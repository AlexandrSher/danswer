import re
from collections.abc import Callable
from collections.abc import Iterator
from typing import cast

from langchain.schema.messages import AIMessage
from langchain.schema.messages import BaseMessage
from langchain.schema.messages import HumanMessage
from langchain.schema.messages import SystemMessage
from sqlalchemy.orm import Session

from danswer.chat.tools import call_tool
from danswer.configs.app_configs import NUM_DOCUMENT_TOKENS_FED_TO_CHAT, DISABLE_GENERATIVE_AI
from danswer.configs.chat_configs import FORCE_TOOL_PROMPT
from danswer.configs.constants import IGNORE_FOR_QA, MessageType
from danswer.configs.model_configs import GEN_AI_MAX_INPUT_TOKENS
from danswer.db.chat import get_or_create_root_message, fetch_chat_message, create_new_chat_message, \
    get_doc_query_identifiers_from_model, fetch_chat_messages_by_session, fetch_chat_session_by_id
from danswer.db.feedback import create_query_event, update_query_event_retrieved_documents
from danswer.db.models import ChatMessage, ChatSession
from danswer.db.models import Persona
from danswer.db.models import User
from danswer.direct_qa.interfaces import DanswerAnswerPiece
from danswer.direct_qa.interfaces import DanswerChatModelOut
from danswer.direct_qa.interfaces import StreamingError
from danswer.direct_qa.qa_utils import get_usable_chunks, get_chunks_for_qa
from danswer.document_index.factory import get_default_document_index
from danswer.indexing.models import InferenceChunk
from danswer.llm.factory import get_default_llm
from danswer.llm.interfaces import LLM
from danswer.llm.utils import get_default_llm_token_encode
from danswer.llm.utils import translate_danswer_msg_to_langchain
from danswer.search.access_filters import build_access_filters_for_user
from danswer.search.models import IndexFilters, BaseFilters, QueryFlow
from danswer.search.models import SearchQuery
from danswer.search.models import SearchType
from danswer.search.request_preprocessing import retrieval_preprocessing
from danswer.search.search_runner import chunks_to_search_docs, inference_documents_from_ids, \
    full_chunk_search_generator, empty_search_generator
from danswer.search.search_runner import full_chunk_search
from danswer.secondary_llm_flows.chat_helpers import check_if_need_search, history_based_query_rephrase
from danswer.server.chat.models import RetrievalDocs, CreateChatMessageRequest, RetrievalDetails, QADocsResponse, \
    LLMRelevanceFilterResponse
from danswer.server.utils import get_json_line
from danswer.utils.logger import setup_logger
from danswer.utils.text_processing import extract_embedded_json
from danswer.utils.text_processing import has_unescaped_quote
from danswer.utils.timing import log_generator_function_time

logger = setup_logger()


LLM_CHAT_FAILURE_MSG = "The large-language-model failed to generate a valid response."


def _create_chat_chain(
    chat_session_id: int,
    db_session: Session,
) -> tuple[ChatMessage, list[ChatMessage]]:
    """Build the linear chain of messages without including the root message"""
    mainline_messages: list[ChatMessage] = []
    all_chat_messages = fetch_chat_messages_by_session(chat_session_id, db_session)
    id_to_msg = {msg.id: msg for msg in all_chat_messages}

    if not all_chat_messages:
        raise ValueError("No messages in Chat Session")

    root_message = all_chat_messages[0]
    if root_message.parent_message is not None:
        raise RuntimeError(
            "Invalid root message, unable to fetch valid chat message sequence"
        )

    current_message: ChatMessage | None = root_message
    while current_message is not None:
        child_msg = current_message.latest_child_message
        if not child_msg:
            break
        current_message = id_to_msg.get(child_msg)

        if current_message is None:
            raise RuntimeError(
                "Invalid message chain,"
                "could not find next message in the same session"
            )

        mainline_messages.append(current_message)

    if not mainline_messages:
        raise RuntimeError("Could not trace chat message history")

    return mainline_messages[-1], mainline_messages[:-1]


def _parse_embedded_json_streamed_response(
    tokens: Iterator[str],
) -> Iterator[DanswerAnswerPiece | DanswerChatModelOut]:
    final_answer = False
    just_start_stream = False
    model_output = ""
    hold = ""
    finding_end = 0
    for token in tokens:
        model_output += token
        hold += token

        if (
            final_answer is False
            and '"action":"finalanswer",' in model_output.lower().replace(" ", "")
        ):
            final_answer = True

        if final_answer and '"actioninput":"' in model_output.lower().replace(
            " ", ""
        ).replace("_", ""):
            if not just_start_stream:
                just_start_stream = True
                hold = ""

            if has_unescaped_quote(hold):
                finding_end += 1
                hold = hold[: hold.find('"')]

            if finding_end <= 1:
                if finding_end == 1:
                    finding_end += 1

                yield DanswerAnswerPiece(answer_piece=hold)
                hold = ""

    model_final = extract_embedded_json(model_output)
    if "action" not in model_final or "action_input" not in model_final:
        raise ValueError("Model did not provide all required action values")

    yield DanswerChatModelOut(
        model_raw=model_output,
        action=model_final["action"],
        action_input=model_final["action_input"],
    )
    return


def _find_last_index(
    lst: list[int], max_prompt_tokens: int = GEN_AI_MAX_INPUT_TOKENS
) -> int:
    """From the back, find the index of the last element to include
    before the list exceeds the maximum"""
    running_sum = 0

    last_ind = 0
    for i in range(len(lst) - 1, -1, -1):
        running_sum += lst[i]
        if running_sum > max_prompt_tokens:
            last_ind = i + 1
            break
    if last_ind >= len(lst):
        raise ValueError("Last message alone is too large!")
    return last_ind


def danswer_chat_retrieval(
    query_message: ChatMessage,
    history: list[ChatMessage],
    llm: LLM,
    filters: IndexFilters,
) -> list[InferenceChunk]:
    if history:
        query_combination_msgs = build_combined_query(query_message, history)
        reworded_query = llm.invoke(query_combination_msgs)
    else:
        reworded_query = query_message.message

    search_query = SearchQuery(
        query=reworded_query,
        search_type=SearchType.HYBRID,
        filters=filters,
        favor_recent=False,
    )

    # Good Debug/Breakpoint
    top_chunks, _ = full_chunk_search(
        query=search_query,
        document_index=get_default_document_index(),
    )

    if not top_chunks:
        return []

    filtered_ranked_chunks = [
        chunk for chunk in top_chunks if not chunk.metadata.get(IGNORE_FOR_QA)
    ]

    # get all chunks that fit into the token limit
    usable_chunks = get_usable_chunks(
        chunks=filtered_ranked_chunks,
        token_limit=NUM_DOCUMENT_TOKENS_FED_TO_CHAT,
    )

    return usable_chunks


def _drop_messages_history_overflow(
    system_msg: BaseMessage | None,
    system_token_count: int,
    history_msgs: list[BaseMessage],
    history_token_counts: list[int],
    final_msg: BaseMessage,
    final_msg_token_count: int,
) -> list[BaseMessage]:
    """As message history grows, messages need to be dropped starting from the furthest in the past.
    The System message should be kept if at all possible and the latest user input which is inserted in the
    prompt template must be included"""

    if len(history_msgs) != len(history_token_counts):
        # This should never happen
        raise ValueError("Need exactly 1 token count per message for tracking overflow")

    prompt: list[BaseMessage] = []

    # Start dropping from the history if necessary
    all_tokens = history_token_counts + [system_token_count, final_msg_token_count]
    ind_prev_msg_start = _find_last_index(all_tokens)

    if system_msg and ind_prev_msg_start <= len(history_msgs):
        prompt.append(system_msg)

    prompt.extend(history_msgs[ind_prev_msg_start:])

    prompt.append(final_msg)

    return prompt


def extract_citations_from_stream(
    tokens: Iterator[str], links: list[str | None]
) -> Iterator[str]:
    if not links:
        yield from tokens
        return

    max_citation_num = len(links) + 1  # LLM is prompted to 1 index these
    curr_segment = ""
    prepend_bracket = False
    for token in tokens:
        # Special case of [1][ where ][ is a single token
        if prepend_bracket:
            curr_segment += "[" + curr_segment
            prepend_bracket = False

        curr_segment += token

        possible_citation_pattern = r"(\[\d*$)"  # [1, [, etc
        possible_citation_found = re.search(possible_citation_pattern, curr_segment)

        citation_pattern = r"\[(\d+)\]"  # [1], [2] etc
        citation_found = re.search(citation_pattern, curr_segment)

        if citation_found:
            numerical_value = int(citation_found.group(1))
            if 1 <= numerical_value <= max_citation_num:
                link = links[numerical_value - 1]
                if link:
                    curr_segment = re.sub(r"\[", "[[", curr_segment, count=1)
                    curr_segment = re.sub("]", f"]]({link})", curr_segment, count=1)

                # In case there's another open bracket like [1][, don't want to match this
            possible_citation_found = None

        # if we see "[", but haven't seen the right side, hold back - this may be a
        # citation that needs to be replaced with a link
        if possible_citation_found:
            continue

        # Special case with back to back citations [1][2]
        if curr_segment and curr_segment[-1] == "[":
            curr_segment = curr_segment[:-1]
            prepend_bracket = True

        yield curr_segment
        curr_segment = ""

    if curr_segment:
        if prepend_bracket:
            yield "[" + curr_segment
        else:
            yield curr_segment


def llm_contextless_chat_answer(
    messages: list[ChatMessage],
    system_text: str | None = None,
    tokenizer: Callable | None = None,
) -> Iterator[DanswerAnswerPiece | StreamingError]:
    try:
        prompt_msgs = [translate_danswer_msg_to_langchain(msg) for msg in messages]

        if system_text:
            tokenizer = tokenizer or get_default_llm_token_encode()
            system_tokens = len(tokenizer(system_text))
            system_msg = SystemMessage(content=system_text)

            message_tokens = [msg.token_count for msg in messages] + [system_tokens]
        else:
            message_tokens = [msg.token_count for msg in messages]

        last_msg_ind = _find_last_index(message_tokens)

        remaining_user_msgs = prompt_msgs[last_msg_ind:]
        if not remaining_user_msgs:
            raise ValueError("Last user message is too long!")

        if system_text:
            all_msgs = [system_msg] + remaining_user_msgs
        else:
            all_msgs = remaining_user_msgs

        for token in get_default_llm().stream(all_msgs):
            yield DanswerAnswerPiece(answer_piece=token)

    except Exception as e:
        logger.exception(f"LLM failed to produce valid chat message, error: {e}")
        yield StreamingError(error=str(e))


def llm_contextual_chat_answer(
    messages: list[ChatMessage],
    persona: Persona,
    user: User | None,
    tokenizer: Callable,
    db_session: Session,
    run_search_system_text: str = REQUIRE_DANSWER_SYSTEM_MSG,
) -> Iterator[DanswerAnswerPiece | RetrievalDocs | StreamingError]:
    last_message = messages[-1]
    final_query_text = last_message.message
    previous_messages = messages[:-1]
    previous_msgs_as_basemessage = [
        translate_danswer_msg_to_langchain(msg) for msg in previous_messages
    ]

    try:
        llm = get_default_llm()

        if not final_query_text:
            raise ValueError("User chat message is empty.")

        # Determine if a search is necessary to answer the user query
        user_req_search_text = form_require_search_text(last_message)
        last_user_msg = HumanMessage(content=user_req_search_text)

        previous_msg_token_counts = [msg.token_count for msg in previous_messages]
        danswer_system_tokens = len(tokenizer(run_search_system_text))
        last_user_msg_tokens = len(tokenizer(user_req_search_text))

        need_search_prompt = _drop_messages_history_overflow(
            system_msg=SystemMessage(content=run_search_system_text),
            system_token_count=danswer_system_tokens,
            history_msgs=previous_msgs_as_basemessage,
            history_token_counts=previous_msg_token_counts,
            final_msg=last_user_msg,
            final_msg_token_count=last_user_msg_tokens,
        )

        # Good Debug/Breakpoint
        model_out = llm.invoke(need_search_prompt)

        # Model will output "Yes Search" if search is useful
        # Be a little forgiving though, if we match yes, it's good enough
        retrieved_chunks: list[InferenceChunk] = []
        if (YES_SEARCH.split()[0] + " ").lower() in model_out.lower():
            user_acl_filters = build_access_filters_for_user(user, db_session)
            doc_set_filter = [doc_set.name for doc_set in persona.document_sets] or None
            final_filters = IndexFilters(
                source_type=None,
                document_set=doc_set_filter,
                time_cutoff=None,
                access_control_list=user_acl_filters,
            )

            retrieved_chunks = danswer_chat_retrieval(
                query_message=last_message,
                history=previous_messages,
                llm=llm,
                filters=final_filters,
            )

            yield RetrievalDocs(top_documents=chunks_to_search_docs(retrieved_chunks))

            tool_result_str = format_danswer_chunks_for_chat(retrieved_chunks)

            last_user_msg_text = form_tool_less_followup_text(
                tool_output=tool_result_str,
                query=last_message.message,
                hint_text=persona.hint_text,
            )
            last_user_msg_tokens = len(tokenizer(last_user_msg_text))
            last_user_msg = HumanMessage(content=last_user_msg_text)

        else:
            last_user_msg_tokens = len(tokenizer(final_query_text))
            last_user_msg = HumanMessage(content=final_query_text)

        system_text = build_system_text_from_persona(persona)
        system_msg = SystemMessage(content=system_text) if system_text else None
        system_tokens = len(tokenizer(system_text)) if system_text else 0

        prompt = _drop_messages_history_overflow(
            system_msg=system_msg,
            system_token_count=system_tokens,
            history_msgs=previous_msgs_as_basemessage,
            history_token_counts=previous_msg_token_counts,
            final_msg=last_user_msg,
            final_msg_token_count=last_user_msg_tokens,
        )

        # Good Debug/Breakpoint
        tokens = llm.stream(prompt)
        links = [
            chunk.source_links[0] if chunk.source_links else None
            for chunk in retrieved_chunks
        ]

        for segment in extract_citations_from_stream(tokens, links):
            yield DanswerAnswerPiece(answer_piece=segment)

    except Exception as e:
        logger.exception(f"LLM failed to produce valid chat message, error: {e}")
        yield StreamingError(error=str(e))


def llm_tools_enabled_chat_answer(
    messages: list[ChatMessage],
    persona: Persona,
    user: User | None,
    tokenizer: Callable,
    db_session: Session,
) -> Iterator[DanswerAnswerPiece | RetrievalDocs | StreamingError]:
    retrieval_enabled = persona.retrieval_enabled
    system_text = build_system_text_from_persona(persona)
    hint_text = persona.hint_text
    tool_text = form_tool_section_text(persona.tools, persona.retrieval_enabled)

    last_message = messages[-1]
    previous_messages = messages[:-1]
    previous_msgs_as_basemessage = [
        translate_danswer_msg_to_langchain(msg) for msg in previous_messages
    ]

    # Failure reasons include:
    # - Invalid LLM output, wrong format or wrong/missing keys
    # - No "Final Answer" from model after tool calling
    # - LLM times out or is otherwise unavailable
    # - Calling invalid tool or tool call fails
    # - Last message has more tokens than model is set to accept
    # - Missing user input
    try:
        if not last_message.message:
            raise ValueError("User chat message is empty.")

        # Build the prompt using the last user message
        user_text = form_user_prompt_text(
            query=last_message.message,
            tool_text=tool_text,
            hint_text=hint_text,
        )
        last_user_msg = HumanMessage(content=user_text)

        # Count tokens once to reuse
        previous_msg_token_counts = [msg.token_count for msg in previous_messages]
        system_tokens = len(tokenizer(system_text)) if system_text else 0
        last_user_msg_tokens = len(tokenizer(user_text))

        prompt = _drop_messages_history_overflow(
            system_msg=SystemMessage(content=system_text) if system_text else None,
            system_token_count=system_tokens,
            history_msgs=previous_msgs_as_basemessage,
            history_token_counts=previous_msg_token_counts,
            final_msg=last_user_msg,
            final_msg_token_count=last_user_msg_tokens,
        )

        llm = get_default_llm()

        # Good Debug/Breakpoint
        tokens = llm.stream(prompt)

        final_result: DanswerChatModelOut | None = None
        final_answer_streamed = False

        for result in _parse_embedded_json_streamed_response(tokens):
            if isinstance(result, DanswerAnswerPiece) and result.answer_piece:
                yield result
                final_answer_streamed = True

            if isinstance(result, DanswerChatModelOut):
                final_result = result
                break

        if final_answer_streamed:
            return

        if final_result is None:
            raise RuntimeError("Model output finished without final output parsing.")

        if (
            retrieval_enabled
            and final_result.action.lower() == DANSWER_TOOL_NAME.lower()
        ):
            user_acl_filters = build_access_filters_for_user(user, db_session)
            doc_set_filter = [doc_set.name for doc_set in persona.document_sets] or None

            final_filters = IndexFilters(
                source_type=None,
                document_set=doc_set_filter,
                time_cutoff=None,
                access_control_list=user_acl_filters,
            )

            retrieved_chunks = danswer_chat_retrieval(
                query_message=last_message,
                history=previous_messages,
                llm=llm,
                filters=final_filters,
            )
            yield RetrievalDocs(top_documents=chunks_to_search_docs(retrieved_chunks))

            tool_result_str = format_danswer_chunks_for_chat(retrieved_chunks)
        else:
            tool_result_str = call_tool(final_result)

        # The AI's tool calling message
        tool_call_msg_text = final_result.model_raw
        tool_call_msg_token_count = len(tokenizer(tool_call_msg_text))

        # Create the new message to use the results of the tool call
        tool_followup_text = form_tool_followup_text(
            tool_output=tool_result_str,
            query=last_message.message,
            hint_text=hint_text,
        )
        tool_followup_msg = HumanMessage(content=tool_followup_text)
        tool_followup_tokens = len(tokenizer(tool_followup_text))

        # Drop previous messages, the drop order goes: previous messages in the history,
        # the last user prompt and generated intermediate messages from this recent prompt,
        # the system message, then finally the tool message that was the last thing generated
        follow_up_prompt = _drop_messages_history_overflow(
            system_msg=SystemMessage(content=system_text) if system_text else None,
            system_token_count=system_tokens,
            history_msgs=previous_msgs_as_basemessage
            + [last_user_msg, AIMessage(content=tool_call_msg_text)],
            history_token_counts=previous_msg_token_counts
            + [last_user_msg_tokens, tool_call_msg_token_count],
            final_msg=tool_followup_msg,
            final_msg_token_count=tool_followup_tokens,
        )

        # Good Debug/Breakpoint
        tokens = llm.stream(follow_up_prompt)

        for result in _parse_embedded_json_streamed_response(tokens):
            if isinstance(result, DanswerAnswerPiece) and result.answer_piece:
                yield result
                final_answer_streamed = True

        if final_answer_streamed is False:
            raise RuntimeError("LLM did not to produce a Final Answer after tool call")
    except Exception as e:
        logger.exception(f"LLM failed to produce valid chat message, error: {e}")
        yield StreamingError(error=str(e))


def generate_message_response(
    messages: list[ChatMessage],
    persona: Persona | None,
    tokenizer: Callable,
    user: User | None,
    db_session: Session,
) -> Iterator[DanswerAnswerPiece | RetrievalDocs | StreamingError]:
    # Common error cases to keep in mind:
    # - User asks question about something long ago, due to context limit, the message is dropped
    # - Tool use gives wrong/irrelevant results, model gets confused by the noise
    # - Model is too weak of an LLM, fails to follow instructions
    # - Bad persona design leads to confusing instructions to the model
    # - Bad configurations, too small token limit, mismatched tokenizer to LLM, etc.

    # No setting/persona available therefore no retrieval and no additional tools
    if persona is None:
        return llm_contextless_chat_answer(messages)

    # Persona is configured but with retrieval off and no tools
    # therefore cannot retrieve any context so contextless
    elif persona.retrieval_enabled is False and not persona.tools:
        return llm_contextless_chat_answer(
            messages, system_text=persona.system_text, tokenizer=tokenizer
        )

    # No additional tools outside of Danswer retrieval, can use a more basic prompt
    # Doesn't require tool calling output format (all LLM outputs are therefore valid)
    elif persona.retrieval_enabled and not persona.tools and not FORCE_TOOL_PROMPT:
        return llm_contextual_chat_answer(
            messages=messages,
            persona=persona,
            tokenizer=tokenizer,
            user=user,
            db_session=db_session,
        )

    # Use most flexible/complex prompt format that allows arbitrary tool calls
    # that are configured in the persona file
    # WARNING: this flow does not work well with weaker LLMs (anything below GPT-4)
    return llm_tools_enabled_chat_answer(
        messages=messages,
        persona=persona,
        tokenizer=tokenizer,
        user=user,
        db_session=db_session,
    )


@log_generator_function_time()
def stream_chat_packets(
    chat_message: CreateChatMessageRequest,
    user: User | None,
    db_session: Session,
    chat_session: ChatSession | None = None,  # Just to avoid fetching twice
    disable_generative_answer: bool = DISABLE_GENERATIVE_AI,
) -> Iterator[str]:
    if chat_session is None:
        chat_session = fetch_chat_session_by_id(chat_message.chat_session_id, db_session)

    chat_session_id = chat_message.chat_session_id
    parent_id = chat_message.parent_message_id
    prompt_id = chat_message.prompt_id
    reference_doc_ids = chat_message.search_doc_ids
    retrieval_options = chat_message.retrieval_options
    message_text = chat_message.message
    user_id = user.id if user is not None else None
    specified_search = chat_message.retrieval_options.search_type if chat_message.retrieval_options is not None else None

    llm_tokenizer = get_default_llm_token_encode()
    document_index = get_default_document_index()

    root_message = get_or_create_root_message(
        chat_session_id=chat_session_id, db_session=db_session
    )

    if parent_id is not None:
        parent_message = fetch_chat_message(
            chat_session_id=chat_session_id,
            chat_message_id=parent_id,
            db_session=db_session,
        )
    else:
        parent_message = root_message

    # create record for this query in Postgres
    query_event_id = create_query_event(
        query=message_text,
        chat_session_id=chat_session_id,
        search_type=specified_search,
        llm_answer=None,
        user_id=user_id,
        db_session=db_session,
    )

    # Create new message at the right place in the tree and update the parent's child pointer
    # Don't commit yet until we verify the chat message chain
    new_message = create_new_chat_message(
        chat_session_id=chat_session_id,
        parent_message=parent_message,
        prompt_id=prompt_id,
        message=message_text,
        token_count=len(llm_tokenizer(message_text)),
        message_type=MessageType.USER,
        db_session=db_session,
        commit=False,
    )

    final_msg, history_msgs = _create_chat_chain(
        chat_session_id,
        db_session,
    )

    if final_msg != new_message.id:
        db_session.rollback()
        raise RuntimeError(
            "The new message was not on the mainline. "
            "Be sure to update the chat pointers before calling this."
        )

    # Save now to save the latest chat message
    db_session.commit()

    llm = get_default_llm()

    # Defaults for no retrieval or preselected docs cases
    predicted_search_type = None
    predicted_flow = None
    time_cutoff = None
    favor_recent = False

    if reference_doc_ids:
        identifier_tuples = get_doc_query_identifiers_from_model(reference_doc_ids)
        documents_generator: Iterator[list[InferenceChunk] | list[bool]] = inference_documents_from_ids(
            doc_identifiers=identifier_tuples,
            document_index=get_default_document_index(),
        )
    else:
        if retrieval_options or check_if_need_search(query_message=final_msg, history=history_msgs, llm=llm):
            rephrased_query = history_based_query_rephrase(
                query_message=final_msg,
                history=history_msgs,
                llm=llm
            )

            if not retrieval_options:
                retrieval_options = RetrievalDetails()

            retrieval_request, predicted_search_type, predicted_flow = retrieval_preprocessing(
                query=rephrased_query,
                retrieval_details=retrieval_options,
                user=user,
                db_session=db_session
            )
            documents_generator = full_chunk_search_generator(
                search_query=retrieval_request,
                document_index=document_index,
            )
            time_cutoff = retrieval_request.filters.time_cutoff
            favor_recent = retrieval_request.favor_recent

        else:
            documents_generator = empty_search_generator()

    # first fetch and return to the UI the top chunks so the user can
    # immediately see some results
    top_chunks = cast(list[InferenceChunk], next(documents_generator))

    top_docs = chunks_to_search_docs(top_chunks)
    initial_response = QADocsResponse(
        top_documents=top_docs,
        # if generative AI is disabled, set flow as search so frontend
        # doesn't ask the user if they want to run QA over more documents
        predicted_flow=QueryFlow.SEARCH
        if disable_generative_answer
        else cast(QueryFlow, predicted_flow),
        predicted_search=cast(SearchType, predicted_search_type),
        time_cutoff=time_cutoff,
        favor_recent=favor_recent
    ).dict()
    yield get_json_line(initial_response)

    if not top_chunks:
        logger.debug("No Documents Found")
        return

    # Update record for this query to include top docs
    update_query_event_retrieved_documents(
        db_session=db_session,
        retrieved_document_ids=[doc.document_id for doc in top_chunks]
        if top_chunks
        else [],
        query_id=query_event_id,
        user_id=user_id,
    )

    # Get the final ordering of chunks for the LLM call
    llm_chunk_selection = cast(list[bool], next(documents_generator))
    llm_chunks_indices = get_chunks_for_qa(
        chunks=top_chunks,
        llm_chunk_selection=llm_chunk_selection,
        token_limit=chat_session.persona.num_chunks,
    )
    
    llm_relevance_filtering_response = LLMRelevanceFilterResponse(
        relevant_chunk_indices=[
            index for index, value in enumerate(llm_chunk_selection) if value
        ]
        if not retrieval_request.skip_llm_chunk_filter
        else []
    ).dict()
    yield get_json_line(llm_relevance_filtering_response)

    if disable_generative_answer:
        logger.debug("Skipping QA because generative AI is disabled")
        return

    response_packets = generate_message_response(
        query_message=final_msg,
        history=history_msgs,
        persona=chat_session.persona,
        tokenizer=llm_tokenizer,
        user=user,
        db_session=db_session,
    )

    llm_output = ""
    fetched_docs: RetrievalDocs | None = None
    for packet in response_packets:
        if isinstance(packet, DanswerAnswerPiece):
            token = packet.answer_piece
            if token:
                llm_output += token
        elif isinstance(packet, RetrievalDocs):
            fetched_docs = packet
        yield get_json_line(packet.dict())

    create_new_chat_message(
        chat_session_id=chat_session_id,
        message_number=message_number + 1,
        parent_edit_number=new_message.edit_number,
        message=llm_output,
        token_count=len(llm_tokenizer(llm_output)),
        message_type=MessageType.ASSISTANT,
        retrieval_docs=fetched_docs.dict() if fetched_docs else None,
        db_session=db_session,
    )
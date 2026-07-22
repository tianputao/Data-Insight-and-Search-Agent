"""
Master Agent implementation using Microsoft Agent Framework.
Follows MAF best practices for multi-turn conversations and threading.
"""

from contextvars import ContextVar, copy_context
from threading import Event
from typing import List, Dict, Any, Optional, Annotated
import json
import time
from pydantic import Field

from .search_agent import SearchAgent
from .data_insight_agent import DataInsightAgent
from .metadata_agent import MetadataAgent
from ..config import AppConfig
from ..prompts import MASTER_AGENT_PROMPT
from ..query_engine import QueryEngineContext
from ..utils import get_logger
from ..utils.activity import (
    agent_activity,
    narration_activity,
    new_activity_id,
    pipeline_activity,
    stage_activity,
    tool_activity,
)
from .maf_runtime import (
    create_agent as create_maf_agent,
    create_session,
    run_agent,
    stream_agent,
)


logger = get_logger(__name__)


class MasterAgent:
    """
    Master Agent using Microsoft Agent Framework 1.11.
    Orchestrates knowledge retrieval and answer generation.
    Manages multi-turn conversations using agent threads.
    """
    
    def __init__(
        self,
        search_agent: SearchAgent,
        data_insight_agent: Optional[DataInsightAgent] = None,
        metadata_agent: Optional[MetadataAgent] = None,
        agent_id: str = "master_agent"
    ):
        """
        Initialize Master Agent.

        Args:
            search_agent: Configured search agent (required).
            data_insight_agent: Optional DataInsightAgent for analytical queries.
            metadata_agent: Optional MetadataAgent for UC schema queries.
            agent_id: Unique identifier.
        """
        self.search_agent = search_agent
        self.data_insight_agent = data_insight_agent
        self.metadata_agent = metadata_agent
        self.agent_id = agent_id
        self._active_turn_context: ContextVar[Optional[QueryEngineContext]] = ContextVar(
            f"{agent_id}_active_turn_context",
            default=None,
        )

        # Create the MAF agent with in-memory session history.
        # In-memory message store is used by default for multi-turn conversations
        self.agent = self._create_agent()

        logger.info(f"MasterAgent '{agent_id}' initialized successfully")

    def _turn_context_var(self) -> ContextVar[Optional[QueryEngineContext]]:
        """Return the request-local context variable, including for lightweight tests."""
        context_var = getattr(self, "_active_turn_context", None)
        if context_var is None:
            context_var = ContextVar(
                f"{getattr(self, 'agent_id', 'master_agent')}_active_turn_context",
                default=None,
            )
            self._active_turn_context = context_var
        return context_var

    def _current_turn(self) -> Optional[QueryEngineContext]:
        return self._turn_context_var().get()

    def _new_turn(
        self,
        message: str,
        *,
        stream_context: Any = None,
        cancel_event: Optional[Event] = None,
    ) -> QueryEngineContext:
        return QueryEngineContext(
            original_question=message,
            max_search_attempts=AppConfig.QUERY_ENGINE_MAX_SEARCH_ATTEMPTS,
            stream_context=stream_context,
            cancel_event=cancel_event,
        )

    def _record_tool_outcome(
        self,
        name: str,
        *,
        success: bool,
        summary: str = "",
        retryable: bool = True,
        metadata: Optional[Dict[str, Any]] = None,
        started_at: Optional[float] = None,
    ) -> None:
        turn = self._current_turn()
        if turn is None:
            return
        duration_ms = None
        if started_at is not None:
            duration_ms = round((time.perf_counter() - started_at) * 1000)
        turn.record_tool(
            name,
            success=success,
            summary=summary,
            retryable=retryable,
            metadata=metadata,
            duration_ms=duration_ms,
        )
    
    def _create_tools(self) -> List:
        """Create function tools for delegating to search agent."""

        def push_stream_event(item_type: str, payload: Any) -> None:
            turn = self._current_turn()
            streaming_ctx = turn.stream_context if turn is not None else None
            if streaming_ctx is None:
                return
            combined_q, main_loop = streaming_ctx
            try:
                main_loop.call_soon_threadsafe(combined_q.put_nowait, (item_type, payload))
            except Exception as exc:
                logger.warning(f"[streaming] queue push failed: {exc}")

        def start_context_thread(target, *, daemon: bool = False):
            """Start a child thread with the active MasterAgent turn context copied in."""
            import threading

            context = copy_context()
            thread = threading.Thread(
                target=lambda: context.run(target),
                daemon=daemon,
            )
            thread.start()
            return thread

        def wait_for_context_thread(thread, timeout: float) -> bool:
            """Wait for a worker while allowing a session stop to end orchestration promptly."""
            deadline = time.monotonic() + timeout
            while thread.is_alive():
                turn = self._current_turn()
                if turn is not None and turn.cancelled:
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                thread.join(timeout=min(0.1, remaining))
            return True

        def start_agent_activity(
            agent: str,
            task: str,
            *,
            parent_id: Optional[str] = None,
        ) -> tuple[str, float]:
            activity_id = new_activity_id(agent.lower())
            push_stream_event(
                "activity",
                agent_activity(
                    activity_id,
                    agent,
                    task,
                    parent_id=parent_id,
                ),
            )
            return activity_id, time.perf_counter()

        def start_search_activity(task: str) -> tuple[str, float, int]:
            turn = self._current_turn()
            turn_state = turn.progress if turn is not None else {}
            activity_id = turn_state.get("search_agent_id")
            started_at = turn_state.get("search_agent_started_at")
            attempt = int(turn_state.get("search_attempts", 0)) + 1
            if not activity_id:
                activity_id = new_activity_id("searchagent")
                started_at = time.perf_counter()
                turn_state["search_agent_id"] = activity_id
                turn_state["search_agent_started_at"] = started_at
            turn_state["search_attempts"] = attempt
            push_stream_event(
                "activity",
                agent_activity(
                    activity_id,
                    "SearchAgent",
                    task,
                    summary=f"Search attempt {attempt}",
                    metrics={"attempts": attempt},
                ),
            )
            return activity_id, float(started_at), attempt

        def finish_agent_activity(
            activity_id: str,
            agent: str,
            task: str,
            started_at: float,
            *,
            error: bool = False,
            summary: Optional[str] = None,
            metrics: Optional[Dict[str, Any]] = None,
        ) -> None:
            push_stream_event(
                "activity",
                agent_activity(
                    activity_id,
                    agent,
                    task,
                    state="error" if error else "completed",
                    duration_ms=round((time.perf_counter() - started_at) * 1000),
                    summary=summary,
                    metrics=metrics,
                ),
            )

        def push_tool_start(
            name: str,
            arguments: str,
            call_id: str,
            agent: str,
            parent_id: str,
        ) -> str:
            try:
                args = json.loads(arguments) if arguments else {}
            except Exception:
                args = {}
            stable_call_id = call_id or new_activity_id("call")
            push_stream_event(
                "activity",
                tool_activity(
                    name,
                    args,
                    stable_call_id,
                    agent=agent,
                    parent_id=parent_id,
                ),
            )
            return stable_call_id

        def push_tool_end(
            call_id: str,
            name: str,
            arguments: str,
            agent: str,
            parent_id: str,
            error: bool = False,
        ) -> None:
            if call_id:
                try:
                    args = json.loads(arguments) if arguments else {}
                except Exception:
                    args = {}
                activity = tool_activity(
                    name,
                    args,
                    call_id,
                    agent=agent,
                    parent_id=parent_id,
                )
                activity["state"] = "error" if error else "completed"
                push_stream_event(
                    "activity",
                    activity,
                )

        async def collect_nested_agent_stream(
            stream,
            *,
            agent: str,
            parent_id: str,
            stream_final_text: bool,
        ) -> str:
            """Forward nested model narration and tool lifecycle without exposing private reasoning."""
            active_text: List[str] = []
            pending_name = ""
            pending_args = ""
            pending_call_id = ""
            started_calls: Dict[str, tuple[str, str, float]] = {}

            def tool_result_failed(name: str, result: Any, exception: Any) -> bool:
                if exception:
                    return True
                if name != "execute_sql":
                    return False
                result_text = str(result or "").lower()
                return any(
                    marker in result_text
                    for marker in (
                        "query execution failed",
                        "configuration error",
                        "only read-only",
                        "timed out",
                        "sql error",
                    )
                )

            def reclassify_text() -> None:
                nonlocal active_text
                raw_text = "".join(active_text)
                active_text = []
                text = raw_text.strip()
                if not text:
                    return
                push_stream_event(
                    "activity",
                    narration_activity(
                        new_activity_id("narration"),
                        text,
                        agent=agent,
                        parent_id=parent_id,
                    ),
                )

            def flush_pending() -> None:
                nonlocal pending_call_id, pending_name, pending_args
                if not pending_name:
                    return
                reclassify_text()
                stable_call_id = push_tool_start(
                    pending_name,
                    pending_args,
                    pending_call_id,
                    agent,
                    parent_id,
                )
                started_calls[stable_call_id] = (
                    pending_name,
                    pending_args,
                    time.perf_counter(),
                )
                pending_name = ""
                pending_args = ""
                pending_call_id = ""

            try:
                async for update in stream:
                    if hasattr(update, "text") and update.text:
                        active_text.append(update.text)

                    if not (hasattr(update, "contents") and update.contents):
                        continue

                    for content in update.contents:
                        content_type = getattr(content, "type", None)
                        if content_type == "text_reasoning":
                            reasoning_text = (getattr(content, "text", "") or "").strip()
                            if reasoning_text:
                                push_stream_event(
                                    "activity",
                                    {
                                        "id": new_activity_id("reasoning"),
                                        "kind": "reasoning",
                                        "category": "reasoning",
                                        "state": "completed",
                                        "agent": agent,
                                        "parent_id": parent_id,
                                        "message": reasoning_text,
                                    },
                                )
                        elif content_type == "function_call":
                            name = getattr(content, "name", "") or ""
                            arguments = getattr(content, "arguments", "") or ""
                            if name and name != pending_name:
                                flush_pending()
                                pending_name = name
                                pending_call_id = getattr(content, "call_id", "") or ""
                                pending_args = arguments
                            else:
                                pending_args += arguments

                            if pending_args:
                                try:
                                    json.loads(pending_args)
                                    flush_pending()
                                except json.JSONDecodeError:
                                    pass
                        elif content_type == "function_result":
                            flush_pending()
                            call_id = getattr(content, "call_id", "") or ""
                            call_info = started_calls.get(call_id)
                            if call_info:
                                name, arguments, tool_started_at = call_info
                                result_payload = getattr(content, "result", None)
                                exception = getattr(content, "exception", None)
                                failed = tool_result_failed(
                                    name,
                                    result_payload,
                                    exception,
                                )
                                push_tool_end(
                                    call_id,
                                    name,
                                    arguments,
                                    agent,
                                    parent_id,
                                    error=failed,
                                )
                                self._record_tool_outcome(
                                    name,
                                    success=not failed,
                                    summary=(
                                        str(exception or result_payload or "")[:1000]
                                    ),
                                    metadata={"agent": agent},
                                    started_at=tool_started_at,
                                )
                flush_pending()
            finally:
                try:
                    await stream.aclose()
                except Exception:
                    pass

            final_text = "".join(active_text)
            if stream_final_text and final_text:
                push_stream_event("text", final_text)
            return final_text.strip()
        
        def decompose_query(
            original_query: Annotated[str, Field(description="The original user question to decompose")],
            num_subqueries: Annotated[int, Field(description="Number of sub-queries to generate (1-5)")] = 3
        ) -> str:
            """
            Decompose a complex query into multiple focused sub-queries.
            Use this when agentic retrieval is disabled and the question is complex.
            Returns a list of sub-queries that can be searched independently.
            """
            logger.info(f"[Tool] decompose_query called for: '{original_query}' (num_subqueries={num_subqueries})")
            tool_started_at = time.perf_counter()
            
            # Use LLM to decompose the query
            decomposition_prompt = f"""Analyze this question and break it down into {num_subqueries} focused, independent sub-questions that together cover all aspects of the original question.

Original Question: {original_query}

Requirements:
1. Each sub-question should be specific and independently searchable
2. Correct ambiguous wording, spelling, abbreviations, and likely terminology errors
3. Enrich each query with relevant synonyms, formal names, and domain terminology
4. Sub-questions should cover different aspects without unnecessary overlap
5. Preserve every constraint and comparison requested by the user
6. Return ONLY the final search-ready sub-questions, numbered 1-{num_subqueries}

Sub-questions:"""
            
            try:
                import asyncio

                decomposition_agent = create_maf_agent(
                    name="QueryDecompositionAgent",
                    instructions="Decompose the supplied question exactly as requested.",
                    tools=[],
                    temperature=0.1,
                )
                response = asyncio.run(run_agent(decomposition_agent, decomposition_prompt))
                subqueries_text = response.text
                
                logger.info(f"[Tool] Query decomposed into:\\n{subqueries_text}")
                self._record_tool_outcome(
                    "decompose_query",
                    success=bool(subqueries_text.strip()),
                    summary=f"Prepared {num_subqueries} search-ready subqueries",
                    metadata={"subquery_count": num_subqueries},
                    started_at=tool_started_at,
                )
                return f"Successfully decomposed query. Sub-queries:\\n{subqueries_text}"
                
            except Exception as e:
                logger.error(f"[Tool] decompose_query failed: {e}", exc_info=True)
                self._record_tool_outcome(
                    "decompose_query",
                    success=False,
                    summary=str(e),
                    started_at=tool_started_at,
                )
                return f"Failed to decompose query: {str(e)}"
        
        def search_multiple_queries(
            queries: Annotated[List[str], Field(description="List of search queries to execute in parallel")]
        ) -> str:
            """
            Execute multiple search queries in parallel and return aggregated results.
            Use this for complex questions that have been decomposed into sub-queries.
            Before calling, emit a user-visible working sentence that explicitly names SearchAgent
            and explains why these sub-queries are being delegated.
            Returns formatted results from all searches with citations.
            """
            logger.info(f"[Tool] search_multiple_queries called with {len(queries)} queries: {queries}")
            tool_started_at = time.perf_counter()
            turn = self._current_turn()
            turn_state = turn.progress if turn is not None else {}
            search_limit = (
                turn.max_search_attempts
                if turn is not None
                else AppConfig.QUERY_ENGINE_MAX_SEARCH_ATTEMPTS
            )
            if int(turn_state.get("search_attempts", 0)) >= search_limit:
                self._record_tool_outcome(
                    "search_multiple_queries",
                    success=False,
                    retryable=False,
                    summary="Search attempt limit reached",
                    metadata={"query_count": len(queries)},
                    started_at=tool_started_at,
                )
                return (
                    "SearchAgent has reached the configured retrieval-attempt limit. "
                    "Use the available evidence and clearly acknowledge any remaining gap."
                )
            agent_activity_id, agent_started_at, search_attempt = start_search_activity(
                "\n".join(queries[:5])
            )
            
            import asyncio
            
            # Container for all results
            all_results = {"results": [], "error": None}

            def report_parallel_progress(progress: Dict[str, Any]) -> None:
                stage = str(progress.get("stage") or "parallel_search")
                metrics = progress.get("metrics") if isinstance(progress.get("metrics"), dict) else {}
                if progress.get("query_index"):
                    metrics = {**metrics, "query_index": progress["query_index"]}
                push_stream_event(
                    "activity",
                    stage_activity(
                        f"{agent_activity_id}-attempt-{search_attempt}-{stage}",
                        agent_activity_id,
                        "SearchAgent",
                        str(progress.get("message") or stage.replace("_", " ").title()),
                        state=str(progress.get("state") or "running"),
                        detail=progress.get("detail"),
                        category=str(progress.get("category") or "search"),
                        metrics=metrics,
                    ),
                )
            
            def run_parallel_searches():
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        # Execute all searches in parallel
                        results_list = loop.run_until_complete(
                            self.search_agent.search_tool.parallel_search(
                                queries,
                                progress_callback=report_parallel_progress,
                            )
                        )
                        all_results["results"] = results_list
                    finally:
                        loop.close()
                except Exception as e:
                    all_results["error"] = e
            
            # Run in separate thread
            thread = start_context_thread(run_parallel_searches, daemon=True)
            thread_finished = wait_for_context_thread(thread, 60)
            
            if not thread_finished:
                turn = self._current_turn()
                if turn is not None and turn.cancelled:
                    return "Search cancelled by user."
                finish_agent_activity(
                    agent_activity_id,
                    "SearchAgent",
                    "\n".join(queries[:5]),
                    agent_started_at,
                    error=True,
                    summary="Parallel search timed out",
                )
                self._record_tool_outcome(
                    "search_multiple_queries",
                    success=False,
                    summary="Parallel search timed out",
                    metadata={"query_count": len(queries)},
                    started_at=tool_started_at,
                )
                return "Search timeout: The parallel search operation took too long."
            
            if all_results["error"]:
                error_msg = str(all_results["error"])
                logger.error(f"[Tool] search_multiple_queries failed: {error_msg}", exc_info=True)
                finish_agent_activity(
                    agent_activity_id,
                    "SearchAgent",
                    "\n".join(queries[:5]),
                    agent_started_at,
                    error=True,
                    summary=error_msg[:160],
                )
                self._record_tool_outcome(
                    "search_multiple_queries",
                    success=False,
                    summary=error_msg,
                    metadata={"query_count": len(queries)},
                    started_at=tool_started_at,
                )
                return f"Parallel search error: {error_msg}"
            
            results_list = all_results["results"]
            
            if not results_list or all(len(r) == 0 for r in results_list):
                logger.warning(f"[Tool] No results found for any query")
                finish_agent_activity(
                    agent_activity_id,
                    "SearchAgent",
                    "\n".join(queries[:5]),
                    agent_started_at,
                    summary="No relevant documents found",
                    metrics={"query_count": len(queries), "selected_count": 0},
                )
                self._record_tool_outcome(
                    "search_multiple_queries",
                    success=False,
                    summary="No relevant documents found for the subqueries",
                    metadata={"query_count": len(queries), "selected_count": 0},
                    started_at=tool_started_at,
                )
                return "No relevant documents found for any of the sub-queries."
            
            # Aggregate and deduplicate results
            aggregation_stage_id = (
                f"{agent_activity_id}-attempt-{search_attempt}-aggregate_results"
            )
            push_stream_event(
                "activity",
                stage_activity(
                    aggregation_stage_id,
                    agent_activity_id,
                    "SearchAgent",
                    "Aggregate unique documents",
                    detail="Merge parallel result sets and remove duplicate document IDs",
                    category="filtering",
                    metrics={
                        "query_count": len(queries),
                        "raw_result_count": sum(len(result_set) for result_set in results_list),
                    },
                ),
            )
            seen_ids = set()
            aggregated_results = []
            citation_counter = 1
            
            for query_idx, query_results in enumerate(results_list):
                logger.info(f"[Tool] Query {query_idx + 1} ('{queries[query_idx][:50]}...') returned {len(query_results)} results")
                
                for result in query_results:
                    result_id = result.get('id')
                    if result_id and result_id not in seen_ids:
                        seen_ids.add(result_id)
                        result['citation_number'] = citation_counter
                        aggregated_results.append(result)
                        citation_counter += 1

            push_stream_event(
                "activity",
                stage_activity(
                    aggregation_stage_id,
                    agent_activity_id,
                    "SearchAgent",
                    "Aggregate unique documents",
                    state="completed",
                    detail="Merge parallel result sets and remove duplicate document IDs",
                    category="filtering",
                    metrics={
                        "query_count": len(queries),
                        "raw_result_count": sum(len(result_set) for result_set in results_list),
                        "selected_count": len(aggregated_results),
                    },
                ),
            )
            
            logger.info(f"[Tool] Aggregated {len(aggregated_results)} unique results from {len(queries)} queries")
            
            # Format aggregated results
            formatted_results = []
            formatted_results.append(f"Found {len(aggregated_results)} unique documents across {len(queries)} search queries:\\n")
            
            for i, result in enumerate(aggregated_results[:20], 1):  # Limit to top 20
                content = result.get("content", "No content available")
                title = result.get("title", "Untitled")
                url = result.get("url") or "Internal Document"
                score = result.get("score", 0)
                reranker_score = result.get("reranker_score")
                
                formatted_results.append(f"\\n[{i}] {title}")
                if reranker_score:
                    formatted_results.append(f"Score: {score:.4f} | Reranker: {reranker_score:.4f}")
                else:
                    formatted_results.append(f"Score: {score:.4f}")
                formatted_results.append(f"Content: {content}")
                formatted_results.append(f"Source: {url}\\n")
            
            result_text = "\\n".join(formatted_results)
            finish_agent_activity(
                agent_activity_id,
                "SearchAgent",
                "\n".join(queries[:5]),
                agent_started_at,
                summary=f"Selected {len(aggregated_results)} unique documents",
                metrics={
                    "query_count": len(queries),
                    "selected_count": len(aggregated_results),
                    "attempts": search_attempt,
                },
            )
            self._record_tool_outcome(
                "search_multiple_queries",
                success=True,
                summary=f"Selected {len(aggregated_results)} unique documents",
                metadata={
                    "query_count": len(queries),
                    "selected_count": len(aggregated_results),
                },
                started_at=tool_started_at,
            )
            logger.info(f"[Tool] search_multiple_queries completed: {len(result_text)} chars")
            return result_text
        
        def search_knowledge(
            query: Annotated[str, Field(description="Query to search the knowledge base. Be specific.")]
        ) -> str:
            """
            Search the enterprise knowledge base for relevant information.
            Use this tool when you need to find information to answer user questions.
            Before calling, emit a user-visible working sentence that explicitly names SearchAgent
            and explains what evidence it should retrieve.
            For simple, focused questions only. For complex questions, use decompose_query first.
            Returns formatted search results with citations and image URLs when available.
            """
            logger.info(f"[Tool] search_knowledge called with query: '{query}'")
            tool_started_at = time.perf_counter()
            turn = self._current_turn()
            turn_state = turn.progress if turn is not None else {}
            search_limit = (
                turn.max_search_attempts
                if turn is not None
                else AppConfig.QUERY_ENGINE_MAX_SEARCH_ATTEMPTS
            )
            if int(turn_state.get("search_attempts", 0)) >= search_limit:
                logger.warning("SearchAgent attempt limit reached; reusing the latest search result.")
                self._record_tool_outcome(
                    "search_knowledge",
                    success=False,
                    retryable=False,
                    summary="Search attempt limit reached",
                    started_at=tool_started_at,
                )
                return turn_state.get("last_search_result") or (
                    "SearchAgent has already completed two retrieval attempts for this turn. "
                    "Use the available evidence and acknowledge any remaining gap."
                )
            agent_activity_id, agent_started_at, search_attempt = start_search_activity(query)

            import asyncio
            import threading

            streaming_ctx = turn.stream_context if turn is not None else None

            def _push_refs(refs: Dict[str, tuple[str, str]]):
                if streaming_ctx is not None and refs:
                    combined_q, ml = streaming_ctx
                    try:
                        ml.call_soon_threadsafe(combined_q.put_nowait, ("refs", refs))
                    except Exception:
                        pass

            result_container: Dict[str, Any] = {"result": None, "error": None}

            def report_search_progress(progress: Dict[str, Any]) -> None:
                stage = str(progress.get("stage") or "search")
                push_stream_event(
                    "activity",
                    stage_activity(
                        f"{agent_activity_id}-attempt-{search_attempt}-{stage}",
                        agent_activity_id,
                        "SearchAgent",
                        str(progress.get("message") or stage.replace("_", " ").title()),
                        state=str(progress.get("state") or "running"),
                        detail=progress.get("detail"),
                        category=str(progress.get("category") or "search"),
                        metrics=progress.get("metrics") if isinstance(progress.get("metrics"), dict) else {},
                    ),
                )

            def run_async_search():
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        results_dict = loop.run_until_complete(
                            self.search_agent.search_knowledge_base(
                                query,
                                progress_callback=report_search_progress,
                            )
                        )
                        result_container["result"] = results_dict
                    finally:
                        loop.close()
                except Exception as e:
                    result_container["error"] = e

            thread = start_context_thread(run_async_search, daemon=True)
            thread_finished = wait_for_context_thread(thread, 30)

            if not thread_finished:
                turn = self._current_turn()
                if turn is not None and turn.cancelled:
                    return "Search cancelled by user."
                finish_agent_activity(
                    agent_activity_id,
                    "SearchAgent",
                    query,
                    agent_started_at,
                    error=True,
                    summary="Knowledge search timed out",
                )
                self._record_tool_outcome(
                    "search_knowledge",
                    success=False,
                    summary="Knowledge search timed out",
                    metadata={"query": query},
                    started_at=tool_started_at,
                )
                return "Search timeout: The search operation took too long to complete."

            if result_container["error"]:
                logger.error(f"[Tool] search_knowledge failed", exc_info=result_container["error"])
                finish_agent_activity(
                    agent_activity_id,
                    "SearchAgent",
                    query,
                    agent_started_at,
                    error=True,
                    summary=str(result_container["error"])[:160],
                )
                self._record_tool_outcome(
                    "search_knowledge",
                    success=False,
                    summary=str(result_container["error"]),
                    metadata={"query": query},
                    started_at=tool_started_at,
                )
                return f"An error occurred while searching: {str(result_container['error'])}"

            results_dict = result_container["result"]

            if not results_dict or not results_dict.get("results"):
                logger.warning(f"[Tool] search_knowledge: No results found")
                finish_agent_activity(
                    agent_activity_id,
                    "SearchAgent",
                    query,
                    agent_started_at,
                    summary="No relevant documents found",
                    metrics={"selected_count": 0},
                )
                self._record_tool_outcome(
                    "search_knowledge",
                    success=False,
                    summary="No relevant documents found",
                    metadata={"query": query, "selected_count": 0},
                    started_at=tool_started_at,
                )
                return "No relevant documents found in the knowledge base for this query."

            results = results_dict["results"]
            logger.info(f"[Tool] Received {len(results)} results from search tool")
            if results:
                for i, res in enumerate(results[:3], 1):
                    reranker_info = (
                        f" | Reranker: {res.get('reranker_score'):.4f}"
                        if res.get("reranker_score") is not None
                        else " | Reranker: N/A"
                    )
                    logger.info(
                        f"[Tool]   [{i}] Score: {res.get('score', 0):.4f}{reranker_info} | "
                        f"Title: {res.get('title', 'N/A')[:60]}"
                    )

            formatted_parts = [f"Found {len(results)} relevant documents:\n"]
            refs_map: Dict[str, tuple[str, str]] = {}
            for result in results:
                content = result.get("content", "No content available")
                title = (result.get("title") or "Untitled")
                url = result.get("url", "Internal Document (No URL)")
                citation_id = result.get("citation_id", "?")
                score = result.get("score", 0)
                reranker_score = result.get("reranker_score")
                image_urls = result.get("image_urls", [])
                formatted_parts.append(f"\n[{citation_id}] {title}")
                if reranker_score:
                    formatted_parts.append(f"Score: {score:.4f} | Reranker: {reranker_score:.4f}")
                else:
                    formatted_parts.append(f"Score: {score:.4f}")
                formatted_parts.append(f"Content: {content}")
                # Append any separately-stored images (from image_mapping field) so the
                # LLM can include them in its response per the IMAGE RULE.
                if image_urls:
                    for img_url in image_urls:
                        formatted_parts.append(f"Image: ![图片]({img_url})")
                formatted_parts.append(f"Source: {url}\n")

                if (
                    isinstance(citation_id, str)
                    and citation_id.isdigit()
                    and url
                    and "Internal Document" not in str(url)
                ):
                    refs_map[citation_id] = (str(title).strip() or f"Reference {citation_id}", str(url).strip())

            result_text = "\n".join(formatted_parts)
            turn_state["last_search_result"] = result_text
            _push_refs(refs_map)
            finish_agent_activity(
                agent_activity_id,
                "SearchAgent",
                query,
                agent_started_at,
                summary=f"Selected {len(results)} relevant documents",
                metrics={
                    "selected_count": len(results),
                    "attempts": search_attempt,
                    "semantic_ranking": self.search_agent.search_tool.enable_semantic_reranker,
                    "agentic_retrieval": self.search_agent.search_tool.enable_agentic_retrieval,
                },
            )
            self._record_tool_outcome(
                "search_knowledge",
                success=True,
                summary=f"Selected {len(results)} relevant documents",
                metadata={"query": query, "selected_count": len(results)},
                started_at=tool_started_at,
            )
            logger.info(f"[Tool] search_knowledge completed: {len(results)} results, {len(result_text)} chars")
            # Return plain result text — thinking steps already pushed in real-time above
            return result_text
        
        # ── New delegation tools ───────────────────────────────────────────────

        def delegate_metadata(
            question: Annotated[
                str,
                Field(description="The schema/metadata question or the user question for which UC metadata is needed"),
            ]
        ) -> str:
            """
            Delegate a metadata/schema question to MetadataAgent.
            Before calling, emit a user-visible working sentence that explicitly names MetadataAgent
            and explains which business concepts, tables, or fields it should establish.
            Use this when:
            - The user asks about table structures, column names, or data descriptions.
            - DataInsightAgent needs schema context before writing a query.
            Returns a YAML/markdown schema summary from Unity Catalog.
            """
            logger.info(f"[Tool] delegate_metadata called: '{question[:80]}'")
            tool_started_at = time.perf_counter()

            if self.metadata_agent is None:
                self._record_tool_outcome(
                    "delegate_metadata",
                    success=False,
                    retryable=False,
                    summary="MetadataAgent is not available",
                    started_at=tool_started_at,
                )
                return "MetadataAgent is not available. Please ensure it is initialised."

            agent_activity_id, agent_started_at = start_agent_activity(
                "MetadataAgent",
                question,
                parent_id=(
                    self._current_turn().progress.get("data_pipeline_activity_id")
                    if self._current_turn() is not None
                    else None
                ),
            )
            result_container: Dict[str, Any] = {"result": None, "error": None}

            def _run():
                import asyncio
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    result_container["result"] = loop.run_until_complete(
                        collect_nested_agent_stream(
                            self.metadata_agent.query_stream(question),
                            agent="MetadataAgent",
                            parent_id=agent_activity_id,
                            stream_final_text=False,
                        )
                    )
                except Exception as exc:
                    result_container["error"] = exc
                finally:
                    pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
                    for task in pending:
                        task.cancel()
                    if pending:
                        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                    loop.run_until_complete(loop.shutdown_asyncgens())
                    loop.close()

            t = start_context_thread(_run, daemon=True)
            thread_finished = wait_for_context_thread(t, 90)

            if not thread_finished:
                turn = self._current_turn()
                if turn is not None and turn.cancelled:
                    return "Metadata lookup cancelled by user."
                finish_agent_activity(
                    agent_activity_id,
                    "MetadataAgent",
                    question,
                    agent_started_at,
                    error=True,
                    summary="Metadata lookup timed out",
                )
                self._record_tool_outcome(
                    "delegate_metadata",
                    success=False,
                    summary="Metadata lookup timed out",
                    started_at=tool_started_at,
                )
                return "Metadata lookup timed out."
            if result_container["error"]:
                finish_agent_activity(
                    agent_activity_id,
                    "MetadataAgent",
                    question,
                    agent_started_at,
                    error=True,
                    summary=str(result_container["error"])[:160],
                )
                self._record_tool_outcome(
                    "delegate_metadata",
                    success=False,
                    summary=str(result_container["error"]),
                    started_at=tool_started_at,
                )
                return f"MetadataAgent error: {result_container['error']}"
            finish_agent_activity(
                agent_activity_id,
                "MetadataAgent",
                question,
                agent_started_at,
                summary="Step 1 completed: schema context prepared",
            )
            metadata_result = result_container["result"] or "No metadata returned."
            self._record_tool_outcome(
                "delegate_metadata",
                success=metadata_result != "No metadata returned.",
                summary=(
                    "Relevant schema context prepared"
                    if metadata_result != "No metadata returned."
                    else metadata_result
                ),
                metadata={"result_chars": len(metadata_result)},
                started_at=tool_started_at,
            )
            return metadata_result

        def _run_data_insight(
            question: Annotated[
                str,
                Field(description="The data analysis question to send to DataInsightAgent"),
            ],
            schema_context: Annotated[
                str,
                Field(description="Optional schema context previously returned by delegate_metadata"),
            ] = "",
        ) -> str:
            """
            Delegate a data analysis question to DataInsightAgent.
            Before calling, emit a user-visible working sentence that explicitly names DataInsightAgent
            and cites the schema evidence that enables the analysis.
            Use this when the user asks for data queries, analytics, KPIs, trends, or statistics
            from Azure Databricks Delta tables.
            For best results, first call delegate_metadata to retrieve relevant schema context
            and pass it as schema_context.
            Returns formatted query results and analytical insights.
            """
            logger.info(f"[Pipeline] DataInsightAgent starting: '{question[:80]}'")
            tool_started_at = time.perf_counter()

            if self.data_insight_agent is None:
                self._record_tool_outcome(
                    "data_insight",
                    success=False,
                    retryable=False,
                    summary="DataInsightAgent is not available",
                    started_at=tool_started_at,
                )
                return "DataInsightAgent is not available. Please ensure it is initialised."

            agent_activity_id, agent_started_at = start_agent_activity(
                "DataInsightAgent",
                question,
                parent_id=(
                    self._current_turn().progress.get("data_pipeline_activity_id")
                    if self._current_turn() is not None
                    else None
                ),
            )
            error_container: Dict[str, Any] = {"error": None}
            result_container: Dict[str, str] = {"result": ""}
            turn = self._current_turn()
            original_user_question = (
                turn.original_question.strip()
                if turn is not None
                else (getattr(self, "_current_user_message", "") or "").strip()
            )

            def _run():
                import asyncio as _asyncio
                loop = _asyncio.new_event_loop()
                _asyncio.set_event_loop(loop)
                try:
                    downstream_question = question
                    if original_user_question:
                        downstream_question = (
                            f"<original_user_question>\n{original_user_question}\n</original_user_question>\n\n"
                            f"<delegated_question>\n{question}\n</delegated_question>"
                        )
                    result_container["result"] = loop.run_until_complete(
                        collect_nested_agent_stream(
                            self.data_insight_agent.query_stream(
                                downstream_question,
                                schema_context=schema_context,
                            ),
                            agent="DataInsightAgent",
                            parent_id=agent_activity_id,
                            stream_final_text=True,
                        )
                    )
                except Exception as exc:
                    error_container["error"] = exc
                    logger.error(f"[data_analysis_pipeline] streaming error: {exc}", exc_info=True)
                finally:
                    # No None sentinel — main loop stays alive until master signals "done"
                    pending = [task for task in _asyncio.all_tasks(loop) if not task.done()]
                    for task in pending:
                        task.cancel()
                    if pending:
                        loop.run_until_complete(_asyncio.gather(*pending, return_exceptions=True))
                    loop.run_until_complete(loop.shutdown_asyncgens())
                    loop.close()

            t = start_context_thread(_run, daemon=True)
            thread_finished = wait_for_context_thread(t, 180)

            if not thread_finished:
                turn = self._current_turn()
                if turn is not None and turn.cancelled:
                    return "DataInsight query cancelled by user."
                finish_agent_activity(
                    agent_activity_id,
                    "DataInsightAgent",
                    question,
                    agent_started_at,
                    error=True,
                    summary="Data analysis timed out",
                )
                self._record_tool_outcome(
                    "data_insight",
                    success=False,
                    summary="Data analysis timed out",
                    started_at=tool_started_at,
                )
                return "DataInsight query timed out (180 s)."
            if error_container["error"]:
                finish_agent_activity(
                    agent_activity_id,
                    "DataInsightAgent",
                    question,
                    agent_started_at,
                    error=True,
                    summary=str(error_container["error"])[:160],
                )
                self._record_tool_outcome(
                    "data_insight",
                    success=False,
                    summary=str(error_container["error"]),
                    started_at=tool_started_at,
                )
                return f"DataInsightAgent error: {error_container['error']}"

            result = result_container["result"].strip()
            if not result:
                finish_agent_activity(
                    agent_activity_id,
                    "DataInsightAgent",
                    question,
                    agent_started_at,
                    error=True,
                    summary="No analysis result returned",
                )
                self._record_tool_outcome(
                    "data_insight",
                    success=False,
                    summary="DataInsightAgent returned no results",
                    started_at=tool_started_at,
                )
                return "DataInsightAgent returned no results."

            finish_agent_activity(
                agent_activity_id,
                "DataInsightAgent",
                question,
                agent_started_at,
                summary="Step 2 completed: SQL analysis completed",
            )
            self._record_tool_outcome(
                "data_insight",
                success=True,
                summary="DataInsightAgent completed the analysis",
                metadata={"result_chars": len(result)},
                started_at=tool_started_at,
            )
            return "[STREAMED] DataInsightAgent has completed analysis and streamed the full result to the user."

        def delegate_data_analysis(
            question: Annotated[
                str,
                Field(
                    description=(
                        "The complete data-analysis question. This deterministic pipeline first "
                        "retrieves schema context with MetadataAgent, then immediately delegates "
                        "the same question and schema context to DataInsightAgent."
                    )
                ),
            ]
        ) -> str:
            """Run the required MetadataAgent → DataInsightAgent pipeline without another MasterAgent turn."""
            logger.info(f"[Tool] delegate_data_analysis called: '{question[:80]}'")
            tool_started_at = time.perf_counter()
            pipeline_id = new_activity_id("data-pipeline")
            push_stream_event("activity", pipeline_activity(pipeline_id, question))
            turn = self._current_turn()
            previous_pipeline_id = None
            if turn is not None:
                previous_pipeline_id = turn.progress.get("data_pipeline_activity_id")
                turn.progress["data_pipeline_activity_id"] = pipeline_id

            try:
                metadata_result = delegate_metadata(question)
                if turn is not None and turn.cancelled:
                    push_stream_event(
                        "activity",
                        pipeline_activity(
                            pipeline_id,
                            question,
                            state="error",
                            duration_ms=round((time.perf_counter() - tool_started_at) * 1000),
                            summary="Stopped by user after MetadataAgent",
                        ),
                    )
                    return "Data-analysis pipeline cancelled by user."
                if metadata_result.startswith("MetadataAgent error:") or metadata_result in {
                    "Metadata lookup timed out.",
                    "MetadataAgent is not available. Please ensure it is initialised.",
                    "No metadata returned.",
                }:
                    self._record_tool_outcome(
                        "delegate_data_analysis",
                        success=False,
                        summary=metadata_result,
                        started_at=tool_started_at,
                    )
                    push_stream_event(
                        "activity",
                        pipeline_activity(
                            pipeline_id,
                            question,
                            state="error",
                            duration_ms=round((time.perf_counter() - tool_started_at) * 1000),
                            summary="Stopped after MetadataAgent failed",
                        ),
                    )
                    return metadata_result

                result = _run_data_insight(question, schema_context=metadata_result)
                success = result.startswith("[STREAMED]")
                self._record_tool_outcome(
                    "delegate_data_analysis",
                    success=success,
                    summary=("Data-analysis pipeline completed" if success else result),
                    started_at=tool_started_at,
                )
                push_stream_event(
                    "activity",
                    pipeline_activity(
                        pipeline_id,
                        question,
                        state="completed" if success else "error",
                        duration_ms=round((time.perf_counter() - tool_started_at) * 1000),
                        summary=(
                            "MetadataAgent → DataInsightAgent completed in sequence"
                            if success
                            else "DataInsightAgent did not complete"
                        ),
                    ),
                )
                return result
            finally:
                if turn is not None:
                    if previous_pipeline_id is None:
                        turn.progress.pop("data_pipeline_activity_id", None)
                    else:
                        turn.progress["data_pipeline_activity_id"] = previous_pipeline_id

        return [
            decompose_query,
            search_multiple_queries,
            search_knowledge,
            delegate_metadata,
            delegate_data_analysis,
        ]

    def _create_agent(self):
        """Create and return the MAF MasterAgent."""
        tools = self._create_tools()
        
        # Add agentic retrieval status to system prompt
        agentic_status = "ENABLED" if self.search_agent.search_tool.enable_agentic_retrieval else "DISABLED"
        
        enhanced_prompt = f"""{MASTER_AGENT_PROMPT}

**CURRENT CONFIGURATION:**
- Agentic Retrieval: {agentic_status}
- Semantic Reranker: {'ENABLED' if self.search_agent.search_tool.enable_semantic_reranker else 'DISABLED'}
- DataInsightAgent: {'AVAILABLE' if self.data_insight_agent else 'NOT CONFIGURED'}
- MetadataAgent: {'AVAILABLE' if self.metadata_agent else 'NOT CONFIGURED'}

Remember: When agentic retrieval is {agentic_status}, follow the corresponding workflow described above."""
        
        agent = create_maf_agent(
            name="MasterAgent",
            instructions=enhanced_prompt,
            tools=tools,
            temperature=0.1,
        )
        logger.info("MasterAgent created with MAF OpenAIChatCompletionClient")
        return agent
    
    def get_new_thread(self):
        """
        Create a new conversation thread.
        MAF automatically provides in-memory storage for thread messages.
        
        Returns:
            New AgentThread instance
        """
        thread = create_session(self.agent)
        logger.info(f"Created new conversation thread")
        return thread
    
    async def chat(
        self,
        message: str,
        thread=None
    ) -> Dict[str, Any]:
        """
        Process a user message and generate a response.
        
        Args:
            message: User message
            thread: Optional conversation thread for multi-turn context
            
        Returns:
            Agent response with text and metadata
        """
        logger.info(f"MasterAgent.chat called with message: '{message}'")
        turn = self._new_turn(message)
        context_var = self._turn_context_var()
        token = context_var.set(turn)

        try:
            result = await run_agent(self.agent, message, session=thread)
        finally:
            context_var.reset(token)

        logger.info(f"MasterAgent.chat completed, response length: {len(result.text)}")

        return {
            "text": result.text,
            "messages": [{"role": msg.role, "content": msg.text} for msg in result.messages],
        }
    
    async def chat_stream(
        self,
        message: str,
        thread=None,
        stream_context: Any = None,
        cancel_event: Optional[Event] = None,
    ):
        """
        Process a user message with streaming response.
        Thread maintains conversation history automatically via in-memory storage.
        
        Args:
            message: User message
            thread: Optional conversation thread for multi-turn context
            
        Yields:
            Response chunks (text or agent events)
        """
        logger.info(f"MasterAgent.chat_stream called with message: '{message}'")
        turn = self._new_turn(
            message,
            stream_context=stream_context,
            cancel_event=cancel_event,
        )
        context_var = self._turn_context_var()
        token = context_var.set(turn)

        try:
            response_stream = stream_agent(self.agent, message, session=thread)
            async for update in response_stream:
                yield update
        finally:
            context_var.reset(token)
    
    def update_config(
        self,
        enable_semantic_reranker: Optional[bool] = None,
        enable_agentic_retrieval: Optional[bool] = None
    ):
        """
        Update search configuration.
        
        Args:
            enable_semantic_reranker: Enable/disable semantic reranker
            enable_agentic_retrieval: Enable/disable agentic retrieval
        """
        self.search_agent.update_config(
            enable_semantic_reranker=enable_semantic_reranker,
            enable_agentic_retrieval=enable_agentic_retrieval
        )
        logger.info("MasterAgent configuration updated")

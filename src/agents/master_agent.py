"""
Master Agent implementation using Microsoft Agent Framework.
Follows MAF best practices for multi-turn conversations and threading.
"""

from typing import List, Dict, Any, Optional, Annotated
import json
import time
from pydantic import Field
from agent_framework.azure import AzureOpenAIChatClient
from azure.identity import DefaultAzureCredential

from .search_agent import SearchAgent
from .data_insight_agent import DataInsightAgent
from .metadata_agent import MetadataAgent
from ..prompts import MASTER_AGENT_PROMPT, MASTER_AGENT_PROMPT_BASE
from ..config import AzureOpenAIConfig
from ..injector import skill_injector
from ..utils import get_logger


logger = get_logger(__name__)


class MasterAgent:
    """
    Master Agent using Microsoft Agent Framework's AzureOpenAIChatClient.
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

        # Create agent using AzureOpenAIChatClient pattern
        # In-memory message store is used by default for multi-turn conversations
        self.agent = self._create_agent()

        logger.info(f"MasterAgent '{agent_id}' initialized successfully")
    
    def _create_tools(self) -> List:
        """Create function tools for delegating to search agent."""
        
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
            
            # Use LLM to decompose the query
            decomposition_prompt = f"""Analyze this question and break it down into {num_subqueries} focused, independent sub-questions that together cover all aspects of the original question.

Original Question: {original_query}

Requirements:
1. Each sub-question should be specific and independently searchable
2. Sub-questions should cover different aspects of the main question  
3. Use clear, concise language
4. Include relevant keywords and technical terms
5. Return ONLY the sub-questions, numbered 1-{num_subqueries}

Sub-questions:"""
            
            try:
                from agent_framework.messages import TextMessage
                
                # Create temporary thread for decomposition
                temp_thread = self.agent.create_thread()
                response = self.agent.send_message(
                    TextMessage(content=decomposition_prompt),
                    thread_id=temp_thread.id
                )
                
                # Extract sub-queries from response
                subqueries_text = ""
                for update in response:
                    if hasattr(update, 'text') and update.text:
                        subqueries_text += update.text
                
                logger.info(f"[Tool] Query decomposed into:\\n{subqueries_text}")
                return f"Successfully decomposed query. Sub-queries:\\n{subqueries_text}"
                
            except Exception as e:
                logger.error(f"[Tool] decompose_query failed: {e}", exc_info=True)
                return f"Failed to decompose query: {str(e)}"
        
        def search_multiple_queries(
            queries: Annotated[List[str], Field(description="List of search queries to execute in parallel")]
        ) -> str:
            """
            Execute multiple search queries in parallel and return aggregated results.
            Use this for complex questions that have been decomposed into sub-queries.
            Returns formatted results from all searches with citations.
            """
            logger.info(f"[Tool] search_multiple_queries called with {len(queries)} queries: {queries}")
            
            import asyncio
            import threading
            
            # Container for all results
            all_results = {"results": [], "error": None}
            
            def run_parallel_searches():
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        # Execute all searches in parallel
                        results_list = loop.run_until_complete(
                            self.search_agent.search_tool.parallel_search(queries)
                        )
                        all_results["results"] = results_list
                    finally:
                        loop.close()
                except Exception as e:
                    all_results["error"] = e
            
            # Run in separate thread
            thread = threading.Thread(target=run_parallel_searches)
            thread.start()
            thread.join(timeout=60)  # 60 second timeout for multiple queries
            
            if thread.is_alive():
                return "Search timeout: The parallel search operation took too long."
            
            if all_results["error"]:
                error_msg = str(all_results["error"])
                logger.error(f"[Tool] search_multiple_queries failed: {error_msg}", exc_info=True)
                return f"Parallel search error: {error_msg}"
            
            results_list = all_results["results"]
            
            if not results_list or all(len(r) == 0 for r in results_list):
                logger.warning(f"[Tool] No results found for any query")
                return "No relevant documents found for any of the sub-queries."
            
            # Aggregate and deduplicate results
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
            logger.info(f"[Tool] search_multiple_queries completed: {len(result_text)} chars")
            return result_text
        
        def search_knowledge(
            query: Annotated[str, Field(description="Query to search the knowledge base. Be specific.")]
        ) -> str:
            """
            Search the enterprise knowledge base for relevant information.
            Use this tool when you need to find information to answer user questions.
            For simple, focused questions only. For complex questions, use decompose_query first.
            Returns formatted search results with citations and image URLs when available.
            """
            logger.info(f"[Tool] search_knowledge called with query: '{query}'")

            import asyncio
            import threading

            # Push thinking steps via the combined streaming queue (same as delegate_data_insight)
            streaming_ctx = getattr(self, "_insight_streaming", None)

            def _push_think(msg: str):
                if streaming_ctx is not None:
                    combined_q, ml = streaming_ctx
                    try:
                        ml.call_soon_threadsafe(combined_q.put_nowait, ("thinking", msg))
                    except Exception:
                        pass

            def _push_refs(refs: Dict[str, tuple[str, str]]):
                if streaming_ctx is not None and refs:
                    combined_q, ml = streaming_ctx
                    try:
                        ml.call_soon_threadsafe(combined_q.put_nowait, ("refs", refs))
                    except Exception:
                        pass

            result_container: Dict[str, Any] = {"result": None, "error": None}

            def run_async_search():
                _push_think(f"🔍 [MasterAgent → SearchAgent] 知识库检索: {query}")
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        results_dict = loop.run_until_complete(
                            self.search_agent.search_knowledge_base(query)
                        )
                        result_container["result"] = results_dict
                        # Push search sub-steps one-by-one with a small delay so the
                        # frontend renders them progressively rather than all at once.
                        for step in results_dict.get("thinking_log", []):
                            _push_think(step)
                            time.sleep(0.35)
                        count = len(results_dict.get("results") or [])
                        if count:
                            time.sleep(0.35)
                            _push_think(f"✅ [SearchAgent] 检索到 {count} 条文档")
                    finally:
                        loop.close()
                except Exception as e:
                    result_container["error"] = e

            thread = threading.Thread(target=run_async_search)
            thread.start()
            thread.join(timeout=30)

            if thread.is_alive():
                return "Search timeout: The search operation took too long to complete."

            if result_container["error"]:
                logger.error(f"[Tool] search_knowledge failed", exc_info=result_container["error"])
                return f"An error occurred while searching: {str(result_container['error'])}"

            results_dict = result_container["result"]

            if not results_dict or not results_dict.get("results"):
                logger.warning(f"[Tool] search_knowledge: No results found")
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
            _push_refs(refs_map)
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
            Use this when:
            - The user asks about table structures, column names, or data descriptions.
            - DataInsightAgent needs schema context before writing a query.
            Returns a YAML/markdown schema summary from Unity Catalog.
            """
            logger.info(f"[Tool] delegate_metadata called: '{question[:80]}'")

            if self.metadata_agent is None:
                return "MetadataAgent is not available. Please ensure it is initialised."

            # Push thinking steps into the combined queue if streaming is active
            streaming_ctx = getattr(self, "_insight_streaming", None)

            def _push_think(msg: str):
                if streaming_ctx is not None:
                    combined_q, ml = streaming_ctx
                    try:
                        ml.call_soon_threadsafe(combined_q.put_nowait, ("thinking", msg))
                    except Exception:
                        pass

            _push_think(f"🗂️ [MetadataAgent] 开始查询数据结构: {question[:60]}")

            result_container: Dict[str, Any] = {"result": None, "error": None}

            def _run():
                import asyncio
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    async def collect() -> str:
                        text_parts: List[str] = []
                        pending_name: str = ""
                        pending_args: str = ""

                        def _flush_pending() -> None:
                            nonlocal pending_name, pending_args
                            if not pending_name:
                                return
                            try:
                                args = json.loads(pending_args) if pending_args else {}
                            except Exception:
                                args = {}

                            if pending_name == "load_skill":
                                _push_think(
                                    f"📖 [MetadataAgent] 加载业务规则: {args.get('skill_name', '')}"
                                )

                            pending_name = ""
                            pending_args = ""

                        stream = self.metadata_agent.query_stream(question)
                        try:
                            async for update in stream:
                                if hasattr(update, "text") and update.text:
                                    text_parts.append(update.text)

                                if not (hasattr(update, "contents") and update.contents):
                                    continue

                                for content in update.contents:
                                    ct = getattr(content, "type", None)

                                    if ct == "function_call":
                                        tname = getattr(content, "name", "") or ""
                                        targs = getattr(content, "arguments", "") or ""

                                        if tname and tname != pending_name:
                                            _flush_pending()
                                            pending_name = tname
                                            pending_args = targs
                                        else:
                                            pending_args += targs

                                        if pending_args:
                                            try:
                                                json.loads(pending_args)
                                                _flush_pending()
                                            except json.JSONDecodeError:
                                                pass

                                    elif ct == "function_result":
                                        _flush_pending()

                            _flush_pending()
                        finally:
                            try:
                                await stream.aclose()
                            except Exception:
                                pass

                        return "".join(text_parts).strip()

                    result_container["result"] = loop.run_until_complete(collect())
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

            import threading as _threading
            t = _threading.Thread(target=_run)
            t.start()
            t.join(timeout=90)

            if t.is_alive():
                return "Metadata lookup timed out."
            if result_container["error"]:
                _push_think(f"❌ [MetadataAgent] 查询失败: {result_container['error']}")
                return f"MetadataAgent error: {result_container['error']}"
            _push_think("✅ [MetadataAgent] 数据结构查询完成")
            return result_container["result"] or "No metadata returned."

        def delegate_data_insight(
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
            Use this when the user asks for data queries, analytics, KPIs, trends, or statistics
            from Azure Databricks Delta tables.
            For best results, first call delegate_metadata to retrieve relevant schema context
            and pass it as schema_context.
            Returns formatted query results and analytical insights.
            """
            logger.info(f"[Tool] delegate_data_insight called: '{question[:80]}'")

            if self.data_insight_agent is None:
                return "DataInsightAgent is not available. Please ensure it is initialised."

            # ── Streaming bridge ─────────────────────────────────────────────
            streaming_ctx = getattr(self, "_insight_streaming", None)
            if streaming_ctx is not None:
                combined_q, main_loop = streaming_ctx

                def push(item_type: str, item_data):
                    """Thread-safe push into the combined asyncio queue on the main event loop."""
                    try:
                        main_loop.call_soon_threadsafe(combined_q.put_nowait, (item_type, item_data))
                    except Exception as exc:
                        logger.warning(f"[delegate_data_insight] queue push failed: {exc}")
            else:
                def push(item_type: str, item_data): pass  # non-streaming fallback

            result_parts: List[str] = []
            error_container: Dict[str, Any] = {"error": None}
            original_user_question = (getattr(self, "_current_user_message", "") or "").strip()

            def _run():
                import asyncio as _asyncio
                loop = _asyncio.new_event_loop()
                _asyncio.set_event_loop(loop)
                try:
                    push("thinking", "🤖 [DataInsightAgent] 开始数据分析…")

                    async def collect():
                        # Track pending function_call to accumulate streaming arg deltas
                        pending_name: str = ""
                        pending_args: str = ""
                        downstream_question = question
                        if original_user_question:
                            downstream_question = (
                                f"<original_user_question>\n{original_user_question}\n</original_user_question>\n\n"
                                f"<delegated_question>\n{question}\n</delegated_question>"
                            )
                        stream = self.data_insight_agent.query_stream(
                            downstream_question, schema_context=schema_context
                        )

                        def _flush_pending() -> None:
                            nonlocal pending_name, pending_args
                            if not pending_name:
                                return
                            try:
                                args = json.loads(pending_args) if pending_args else {}
                            except Exception:
                                args = {}
                            if pending_name == "execute_sql":
                                sql = args.get("sql", "").strip()
                                if sql:
                                    push("thinking", f"⚡ [DataInsightAgent] 执行 SQL:\n```sql\n{sql}\n```")
                                else:
                                    push("thinking", "⚡ [DataInsightAgent] 执行 SQL…")
                            elif pending_name == "get_relevant_tables":
                                q = args.get("question", "")[:80]
                                push("thinking", f"🗂️ [DataInsightAgent → MetadataAgent] 查找相关数据表: {q}")
                            elif pending_name == "load_skill":
                                push("thinking", f"📖 [DataInsightAgent] 加载业务规则: {args.get('skill_name', '')}")
                            pending_name = ""
                            pending_args = ""

                        try:
                            async for update in stream:
                                # Stream text deltas directly to the frontend
                                if hasattr(update, "text") and update.text:
                                    result_parts.append(update.text)
                                    push("text", update.text)

                                if not (hasattr(update, "contents") and update.contents):
                                    continue

                                for content in update.contents:
                                    ct = getattr(content, "type", None)

                                    if ct == "function_call":
                                        tname = getattr(content, "name", "") or ""
                                        targs = getattr(content, "arguments", "") or ""

                                        if tname and tname != pending_name:
                                            # New call started — flush previous
                                            _flush_pending()
                                            pending_name = tname
                                            pending_args = targs
                                        else:
                                            # Same call — accumulate args delta
                                            pending_args += targs

                                        # Try to flush if args are complete JSON
                                        if pending_args:
                                            try:
                                                json.loads(pending_args)
                                                _flush_pending()
                                            except json.JSONDecodeError:
                                                pass  # still streaming

                                    elif ct == "function_result":
                                        # Flush any remaining pending call on result
                                        _flush_pending()

                            # Final flush
                            _flush_pending()
                        finally:
                            try:
                                await stream.aclose()
                            except Exception:
                                pass

                    loop.run_until_complete(collect())
                except Exception as exc:
                    error_container["error"] = exc
                    logger.error(f"[delegate_data_insight] streaming error: {exc}", exc_info=True)
                finally:
                    # No None sentinel — main loop stays alive until master signals "done"
                    pending = [task for task in _asyncio.all_tasks(loop) if not task.done()]
                    for task in pending:
                        task.cancel()
                    if pending:
                        loop.run_until_complete(_asyncio.gather(*pending, return_exceptions=True))
                    loop.run_until_complete(loop.shutdown_asyncgens())
                    loop.close()

            import threading as _threading
            t = _threading.Thread(target=_run, daemon=True)
            t.start()
            t.join(timeout=180)

            if t.is_alive():
                return "DataInsight query timed out (180 s)."
            if error_container["error"]:
                return f"DataInsightAgent error: {error_container['error']}"

            result = "".join(result_parts).strip()
            if not result:
                return "DataInsightAgent returned no results."

            brief = result[:300].replace("\n", " ")
            return f"[STREAMED] DataInsightAgent has completed analysis and streamed results to the user. Brief: {brief}…"

        return [decompose_query, search_multiple_queries, search_knowledge, delegate_metadata, delegate_data_insight]

    def _create_agent(self):
        """Create and return the MAF AzureOpenAIChatClient agent."""
        tools = self._create_tools()

        # Use API key or credential-based authentication
        if AzureOpenAIConfig.use_api_key():
            client = AzureOpenAIChatClient(
                endpoint=AzureOpenAIConfig.ENDPOINT,
                deployment_name=AzureOpenAIConfig.GPT_DEPLOYMENT,
                api_key=AzureOpenAIConfig.API_KEY,
                api_version=AzureOpenAIConfig.API_VERSION
            )
        else:
            client = AzureOpenAIChatClient(
                endpoint=AzureOpenAIConfig.ENDPOINT,
                deployment_name=AzureOpenAIConfig.GPT_DEPLOYMENT,
                credential=DefaultAzureCredential(),
                api_version=AzureOpenAIConfig.API_VERSION
            )
        
        # Add agentic retrieval status to system prompt
        agentic_status = "ENABLED" if self.search_agent.search_tool.enable_agentic_retrieval else "DISABLED"
        
        # Build base prompt: inject skills metadata + agentic config
        base_prompt = skill_injector.inject_skills_metadata(MASTER_AGENT_PROMPT)
        enhanced_prompt = f"""{base_prompt}

**CURRENT CONFIGURATION:**
- Agentic Retrieval: {agentic_status}
- Semantic Reranker: {'ENABLED' if self.search_agent.search_tool.enable_semantic_reranker else 'DISABLED'}
- DataInsightAgent: {'AVAILABLE' if self.data_insight_agent else 'NOT CONFIGURED'}
- MetadataAgent: {'AVAILABLE' if self.metadata_agent else 'NOT CONFIGURED'}

Remember: When agentic retrieval is {agentic_status}, follow the corresponding workflow described above."""
        
        agent = client.as_agent(
            name="MasterAgent",
            instructions=enhanced_prompt,
            tools=tools,
            default_options={
                "temperature": 0.1
            }
        )
        
        logger.info("MasterAgent created with AzureOpenAIChatClient")
        return agent
    
    def get_new_thread(self):
        """
        Create a new conversation thread.
        MAF automatically provides in-memory storage for thread messages.
        
        Returns:
            New AgentThread instance
        """
        thread = self.agent.get_new_thread()
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
        
        # Run the agent
        result = await self.agent.run(message, thread=thread)
        
        logger.info(f"MasterAgent.chat completed, response length: {len(result.text)}")
        
        return {
            "text": result.text,
            "messages": [{"role": msg.role, "content": msg.text} for msg in result.messages]
        }
    
    async def chat_stream(
        self,
        message: str,
        thread=None
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
        self._current_user_message = message
        
        # MAF's run_stream automatically maintains conversation history in the thread
        try:
            async for update in self.agent.run_stream(message, thread=thread):
                yield update
        finally:
            self._current_user_message = ""
    
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

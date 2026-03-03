"""
Search Agent implementation using Microsoft Agent Framework.
Follows MAF best practices with AzureOpenAIChatClient.
"""

from typing import List, Dict, Any, Optional, Annotated
from pydantic import Field
from agent_framework.azure import AzureOpenAIChatClient
from azure.identity import DefaultAzureCredential

from ..tools import AzureAISearchTool
from ..prompts import SEARCH_AGENT_PROMPT
from ..config import AzureOpenAIConfig
from ..utils import get_logger


logger = get_logger(__name__)


class SearchAgent:
    """
    Search Agent using Microsoft Agent Framework's AzureOpenAIChatClient.
    Provides intelligent search capabilities backed by Azure AI Search.
    """
    
    def __init__(
        self,
        search_tool: AzureAISearchTool,
        agent_id: str = "search_agent"
    ):
        """
        Initialize Search Agent.
        
        Args:
            search_tool: Configured Azure AI Search tool
            agent_id: Unique identifier for this agent
        """
        self.search_tool = search_tool
        self.agent_id = agent_id
        
        # Create function tools
        tools = self._create_tools()
        
        # Initialize agent using AzureOpenAIChatClient pattern
        self.agent = self._create_agent(tools)
        
        logger.info(f"SearchAgent '{agent_id}' initialized successfully")
    
    def _create_tools(self) -> List:
        """Create function tools for Azure AI Search operations."""
        
        def search_knowledge_base(
            query: Annotated[str, Field(description="Search query to find relevant documents")],
            top_k: Annotated[int, Field(description="Number of final results to return (recommended 10-15)")] = 10
        ) -> Dict[str, Any]:
            """
            Search the enterprise knowledge base using hybrid search.
            Process: Searches for 20 candidates, reranks, and returns top results.
            """
            logger.info(f"[Tool] search_knowledge_base called with query: '{query}', top_k={top_k}")

            import asyncio
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            results_dict = loop.run_until_complete(self.search_knowledge_base(query, top_k=top_k))
            logger.info(f"[Tool] search_knowledge_base returned {results_dict.get('result_count', 0)} results")
            return results_dict
        
        def parallel_search(
            queries: Annotated[List[str], Field(description="List of queries to search in parallel")]
        ) -> Dict[str, List[Dict[str, Any]]]:
            """
            Execute multiple searches in parallel for improved performance.
            Returns a dictionary mapping each query to its results.
            """
            logger.info(f"[Tool] parallel_search called with {len(queries)} queries")
            results = self.search_tool.parallel_search(queries)
            logger.info(f"[Tool] parallel_search completed")
            return results
        
        return [search_knowledge_base, parallel_search]

    async def search_knowledge_base(self, query: str, top_k: int = 10) -> Dict[str, Any]:
        """
        Search the enterprise knowledge base using hybrid search.
        Process: Searches for 20 candidates, reranks, and returns top results.
        """
        # Always search for up to 20 candidates; if fewer are returned, use what's available
        candidate_k = 20
        logger.info(
            f"[Tool] search_knowledge_base async called with query: '{query}', top_k={top_k} "
            f"(internal candidate_k={candidate_k}, reranker_enabled={self.search_tool.enable_semantic_reranker})"
        )

        # 1. Search for candidates (up to 20)
        results = await self.search_tool.search(query, top_k=candidate_k)
        total_found = len(results)

        # 2. If reranker enabled, keep only effectively relevant items (adaptive cutoff),
        #    then apply top_k. This makes the final count reflect real relevance rather
        #    than always showing the configured top_k.
        if self.search_tool.enable_semantic_reranker:
            reranker_scores = [r.get("reranker_score") for r in results if r.get("reranker_score") is not None]
            if reranker_scores:
                top_score = float(reranker_scores[0])
                # Adaptive threshold: keep reasonably relevant results while allowing
                # count to vary naturally by query difficulty.
                cutoff = max(1.9, top_score * 0.85)

                filtered = [
                    r for r in results
                    if (r.get("reranker_score") is None) or (float(r.get("reranker_score")) >= cutoff)
                ]

                # Deduplicate near-identical chunks so "effective result count" is realistic
                # for users (e.g., not counting repeated near-duplicate chunks as separate hits).
                deduped: List[Dict[str, Any]] = []
                seen_signatures = set()
                for r in filtered:
                    signature = (
                        (r.get("title") or "").strip(),
                        (r.get("content") or "").strip()[:180],
                    )
                    if signature in seen_signatures:
                        continue
                    seen_signatures.add(signature)
                    deduped.append(r)

                final_results_list = deduped[:top_k]
            else:
                final_results_list = results[:top_k]
        else:
            final_results_list = results

        if not final_results_list and results:
            final_results_list = results[:1]

        # Format results for the LLM/UI to easily parse
        processed_results = []
        for i, result in enumerate(final_results_list, 1):
            result['citation_id'] = str(i)
            
            # Clean and enhance content before adding to results
            content = result.get("content", "")
            
            # Clean up markdown image syntax - remove HTML tags
            import re
            from src.config.settings import AzureSearchConfig
            
            content = re.sub(
                r'!\[<figcaption>(.*?)</figcaption>\]',
                r'![\1]',
                content
            )
            content = re.sub(
                r'!\[<figcaption></figcaption>\]',
                r'![]',
                content
            )
            
            # Add SAS token to image URLs if they point to pictureindoc container
            if AzureSearchConfig.IMAGE_SAS_TOKEN:
                def add_sas_to_image(match):
                    alt_text = match.group(1)
                    url = match.group(2)
                    if 'pictureindoc' in url and 'sig=' not in url:
                        joiner = '&' if '?' in url else '?'
                        url_with_sas = f"{url}{joiner}{AzureSearchConfig.IMAGE_SAS_TOKEN}"
                        return f"![{alt_text}]({url_with_sas})"
                    return match.group(0)
                
                content = re.sub(
                    r'!\[(.*?)\]\((https://[^)]+)\)',
                    add_sas_to_image,
                    content
                )
            
            processed_results.append({
                "citation_id": str(i),
                "content": content,  # Use cleaned/enhanced content
                "title": result.get("title"),
                "url": result.get("url") or "Internal Document (No URL)",
                "image_urls": result.get("image_urls", []),
                "metadata": result.get("metadata"),
                "score": result.get("score"),
                "reranker_score": result.get("reranker_score")
            })

        # Create thinking trace
        thinking_log = [
            f"🔍 [SearchAgent] 执行检索: '{query}'",
            f"📥 [SearchAgent] 获取到 {total_found} 条候选文档",
        ]
        if self.search_tool.enable_semantic_reranker:
            thinking_log.append("🏆 [SearchAgent] 应用语义重排序")
            thinking_log.append(f"✅ [SearchAgent] 筛选出 Top {len(processed_results)} 条文档")
        else:
            thinking_log.append(f"✅ [SearchAgent] 返回全部 {len(processed_results)} 条文档（重排序已关闭）")

        return {
            "result_count": len(processed_results),
            "results": processed_results,
            "thinking_log": thinking_log
        }
    
    def _create_agent(self, tools: List):
        """
        Create agent instance using AzureOpenAIChatClient.
        
        Args:
            tools: List of function tools
            
        Returns:
            Configured agent
        """
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
        
        agent = client.as_agent(
            name="SearchAgent",
            instructions=SEARCH_AGENT_PROMPT,
            tools=tools,
            default_options={
                "temperature": 0.6
            }
        )
        
        logger.info("SearchAgent created with AzureOpenAIChatClient")
        return agent
    
    async def search(
        self,
        query: str,
        thread=None
    ) -> Dict[str, Any]:
        """
        Execute search using the agent.
        
        Args:
            query: Search query
            thread: Optional thread for conversation context
            
        Returns:
            Search results with agent response
        """
        logger.info(f"SearchAgent.search called with query: '{query}'")
        
        # Run the agent
        result = await self.agent.run(query, thread=thread)
        
        logger.info(f"SearchAgent.search completed, response length: {len(result.text)}")
        
        return {
            "text": result.text,
            "messages": [{"role": msg.role, "content": msg.text} for msg in result.messages]
        }
    
    def update_config(
        self,
        enable_semantic_reranker: Optional[bool] = None,
        enable_agentic_retrieval: Optional[bool] = None
    ):
        """Update search tool configuration."""
        self.search_tool.update_config(
            enable_semantic_reranker=enable_semantic_reranker,
            enable_agentic_retrieval=enable_agentic_retrieval
        )
        logger.info("SearchAgent configuration updated")

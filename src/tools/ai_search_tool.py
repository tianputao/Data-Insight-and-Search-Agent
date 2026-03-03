"""
Azure AI Search tool for the Agentic RAG system.
Provides hybrid search (vector + keyword), semantic reranking, and agentic retrieval capabilities.
"""

import asyncio
from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, urlsplit, urlunsplit, parse_qsl, urlencode

from azure.search.documents import SearchClient
from azure.search.documents.models import (
    VectorizedQuery,
    QueryType,
    QueryCaptionType,
    QueryAnswerType
)
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import HttpResponseError
from openai import AzureOpenAI

from ..config import AzureSearchConfig, AzureOpenAIConfig, get_search_field_config
from ..utils import get_logger


logger = get_logger(__name__)


class AzureAISearchTool:
    """
    Tool for executing searches against Azure AI Search with support for:
    - Hybrid search (vector + keyword)
    - Semantic reranking
    - Agentic retrieval
    - Parallel multi-query execution
    """
    
    def __init__(
        self,
        enable_semantic_reranker: bool = True,
        enable_agentic_retrieval: bool = True,
        top_k: int = 20
    ):
        """
        Initialize Azure AI Search tool.
        
        Args:
            enable_semantic_reranker: Whether to use semantic reranking
            enable_agentic_retrieval: Whether to use AI Search's agentic retrieval
            top_k: Number of top results to return
        """
        self.enable_semantic_reranker = enable_semantic_reranker
        self.enable_agentic_retrieval = enable_agentic_retrieval
        self.top_k = top_k
        
        # Initialize Azure Search client
        self.search_client = SearchClient(
            endpoint=AzureSearchConfig.ENDPOINT,
            index_name=AzureSearchConfig.INDEX_NAME,
            credential=AzureKeyCredential(AzureSearchConfig.API_KEY)
        )
        
        # Initialize Azure OpenAI client for embeddings
        self.openai_client = AzureOpenAI(
            api_key=AzureOpenAIConfig.API_KEY,
            api_version=AzureOpenAIConfig.API_VERSION,
            azure_endpoint=AzureOpenAIConfig.ENDPOINT
        )

        # Load search field configuration
        self.field_config = get_search_field_config()
        
        logger.info(
            f"AzureAISearchTool initialized - "
            f"Semantic Reranker: {enable_semantic_reranker}, "
            f"Agentic Retrieval: {enable_agentic_retrieval}, "
            f"Top K: {top_k}"
        )
    
    def _generate_embedding(self, text: str) -> List[float]:
        """
        Generate embedding vector for the given text using Azure OpenAI.
        
        Args:
            text: Input text to embed
        
        Returns:
            Embedding vector
        """
        try:
            response = self.openai_client.embeddings.create(
                input=text,
                model=AzureOpenAIConfig.EMBEDDING_DEPLOYMENT
            )
            return response.data[0].embedding
        except Exception as e:
            logger.error(f"Error generating embedding: {e}")
            raise

    def _ensure_blob_sas_url(self, url: Optional[str], is_image: bool = False) -> Optional[str]:
        """
        Ensure Azure Blob URL contains a SAS token when storage account is private.

        - Preserves existing query params and URL fragment
        - Adds only missing SAS keys
        - Uses IMAGE_SAS_TOKEN for image links, SAS_TOKEN for document links
        """
        if not url:
            return url

        clean_url = str(url).strip().replace("<", "").replace(">", "")
        if "blob.core.windows.net" not in clean_url:
            return clean_url

        token = AzureSearchConfig.IMAGE_SAS_TOKEN if is_image else AzureSearchConfig.SAS_TOKEN
        if not token:
            return clean_url

        parsed = urlsplit(clean_url)
        existing_params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        token_params = dict(parse_qsl(token, keep_blank_values=True))

        # If URL already has a SAS signature, keep it as-is
        if "sig" in existing_params:
            return clean_url

        merged = {**existing_params, **token_params}
        new_query = urlencode(merged, doseq=True)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, new_query, parsed.fragment))

    def _extract_page_number(self, result: Dict[str, Any]) -> Optional[int]:
        """Best-effort extract of page number from a search result."""
        page = result.get('page_number') or result.get('page') or result.get('chunk_page_number')
        if page is None:
            return None
        try:
            return int(page)
        except (TypeError, ValueError):
            return None

    def _append_page_anchor(self, url: Optional[str], page: Optional[int]) -> Optional[str]:
        """Append #page=N anchor only when meaningful and absent."""
        if not url or not page:
            return url
        if "#page=" in url:
            return url
        return f"{url}#page={page}"

    def _resolve_result_title(self, result: Dict[str, Any]) -> str:
        """Resolve a stable, non-empty title for citations/references."""
        title = result.get(self.field_config["title_field"])
        if isinstance(title, str) and title.strip():
            return title.strip()

        for key in (
            self.field_config.get("sub_title_field"),
            self.field_config.get("main_title_field"),
            self.field_config.get("h1_field"),
            self.field_config.get("h2_field"),
            self.field_config.get("h3_field"),
            self.field_config.get("document_code_field"),
            self.field_config.get("filepath_field"),
        ):
            if not key:
                continue
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        rid = result.get(self.field_config["id_field"])
        return f"Document {rid}" if rid else "Untitled"
    
    def _process_image_mapping(self, image_mapping: Optional[str]) -> List[str]:
        """
        Process image_mapping field to generate accessible image URLs.
        
        Args:
            image_mapping: JSON string or dict containing image paths/metadata
        
        Returns:
            List of image URLs with SAS tokens
        """
        if not image_mapping:
            return []
        
        try:
            import json
            # Parse if string
            if isinstance(image_mapping, str):
                mapping = json.loads(image_mapping)
            else:
                mapping = image_mapping
            
            image_urls = []
            # Handle different mapping formats
            if isinstance(mapping, list):
                # List of image paths
                for img_path in mapping:
                    if isinstance(img_path, str) and AzureSearchConfig.IMAGE_BASE_URL:
                        base = AzureSearchConfig.IMAGE_BASE_URL.rstrip('/')
                        path = img_path.replace('\\', '/').lstrip('/')
                        encoded_path = quote(path, safe='/')
                        candidate = f"{base}/{encoded_path}"
                        image_urls.append(self._ensure_blob_sas_url(candidate, is_image=True))
            elif isinstance(mapping, dict):
                # Dict with image metadata
                if 'images' in mapping:
                    for img in mapping['images']:
                        img_path = img.get('path') if isinstance(img, dict) else str(img)
                        if img_path and AzureSearchConfig.IMAGE_BASE_URL:
                            base = AzureSearchConfig.IMAGE_BASE_URL.rstrip('/')
                            path = img_path.replace('\\', '/').lstrip('/')
                            encoded_path = quote(path, safe='/')
                            candidate = f"{base}/{encoded_path}"
                            image_urls.append(self._ensure_blob_sas_url(candidate, is_image=True))
            
            return image_urls
        except Exception as e:
            logger.warning(f"Error processing image_mapping: {e}")
            return []
    
    async def search(
        self,
        query: str,
        query_vector: Optional[List[float]] = None,
        top_k: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Perform hybrid search with vector and keyword components.
        Routes to agentic retrieval if enabled.
        
        Args:
            query: Search query text
            query_vector: Optional pre-computed query embedding
            top_k: Optional[int] = None
            
        Returns:
            List of search results with scores and metadata
        """
        from ..config import get_select_fields

        # Retry on transient network errors (e.g. RemoteDisconnected from Azure Search)
        _last_exc: Optional[Exception] = None
        for _attempt in range(3):
            try:
                if self.enable_agentic_retrieval:
                    logger.info(f"[AGENTIC MODE] Using Azure AI Search with enhanced semantic understanding")
                    return await self._search_with_agentic_mode(query, query_vector, top_k)
                else:
                    logger.info(f"[STANDARD MODE] Using standard hybrid search")
                    return await self._search_standard(query, query_vector, top_k)
            except Exception as _exc:
                _last_exc = _exc
                _err = str(_exc)
                _transient = any(k in _err for k in ("RemoteDisconnected", "Connection aborted", "ServiceResponseError", "ConnectionError"))
                if _transient and _attempt < 2:
                    _wait = 1.5 * (_attempt + 1)
                    logger.warning(
                        f"Transient Azure Search connection error (attempt {_attempt + 1}/3): {_exc}. "
                        f"Retrying in {_wait:.1f}s…"
                    )
                    await asyncio.sleep(_wait)
                    continue
                raise
        raise _last_exc  # type: ignore[misc]
    
    async def _search_standard(
        self,
        query: str,
        query_vector: Optional[List[float]] = None,
        top_k: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Standard hybrid search implementation.
        Used when agentic retrieval is disabled.
        """
        from ..config import get_select_fields
        
        logger.info(f"Executing standard search for query: '{query}'")
        
        k = top_k if top_k is not None else self.top_k
        
        # Build search parameters
        search_params = {
            "search_text": query,
            "top": k,
            "select": get_select_fields(),  # Use configured field list
            "query_language": "zh-cn"
        }
        
        # Generate embedding if not provided (hybrid search)
        if query_vector is None:
            try:
                query_vector = self._generate_embedding(query)
            except Exception as e:
                logger.warning(f"Embedding generation failed, falling back to keyword-only search: {e}")
                query_vector = None

        # Add vector search if embedding provided
        if query_vector:
            vector_query = VectorizedQuery(
                vector=query_vector,
                k=50,
                fields=self.field_config["vector_field"]
            )
            search_params["vector_queries"] = [vector_query]
            logger.info(f"Vector search enabled with {len(query_vector)} dimensions")
        
        # Add semantic search if enabled
        if self.enable_semantic_reranker:
            search_params["query_type"] = QueryType.SEMANTIC
            search_params["semantic_configuration_name"] = self.field_config["semantic_config_name"]
            logger.info("Semantic reranking enabled")
        
        # Execute search
        try:
            results = self.search_client.search(**search_params)
            
            # Process results with all available fields
            processed_results = []
            result_count = 0
            for result in results:
                result_count += 1
                
                # Log first result in detail for debugging
                if result_count == 1:
                    logger.info(f"=== FIRST SEARCH RESULT (RAW) ===")
                    logger.info(f"Result keys: {list(result.keys())}")
                    logger.info(f"ID: {result.get(self.field_config['id_field'])}")
                    logger.info(f"Content (first 200 chars): {str(result.get(self.field_config['content_field']))[:200]}")
                    logger.info(f"Title: {result.get(self.field_config['title_field'])}")
                    logger.info(f"URL: {result.get(self.field_config['url_field'])}")
                    logger.info(f"Score: {result.get('@search.score')}")
                    logger.info(f"=== END FIRST RESULT ===")
                
                # Construct citation URL using Base URL and SAS Token if available
                raw_url = result.get(self.field_config["url_field"])
                filepath = result.get(self.field_config["filepath_field"])
                
                page = self._extract_page_number(result)

                # Construct citation URL
                citation_url = self._ensure_blob_sas_url(raw_url, is_image=False)
                if AzureSearchConfig.BASE_URL and filepath:
                    base = AzureSearchConfig.BASE_URL.rstrip('/')
                    path = filepath.replace('\\', '/').lstrip('/')  # Ensure forward slashes for URL
                    encoded_path = quote(path, safe='/')  # URL encode the path
                    citation_url = self._ensure_blob_sas_url(f"{base}/{encoded_path}", is_image=False)
                else:
                    if not filepath:
                        logger.warning(
                            f"[CITATION] Missing filepath for result id={result.get(self.field_config['id_field'])} "
                            f"title={result.get(self.field_config['title_field'])} raw_url={raw_url}"
                        )
                    if not AzureSearchConfig.BASE_URL:
                        logger.warning(
                            f"[CITATION] BASE_URL not set; using raw_url for result id={result.get(self.field_config['id_field'])}"
                        )
                    if not raw_url and not filepath:
                        logger.warning(
                            f"[CITATION] No raw_url or filepath available for result id={result.get(self.field_config['id_field'])}"
                        )

                citation_url = self._append_page_anchor(citation_url, page)

                processed_result = {
                    "id": result.get(self.field_config["id_field"]),
                    "content": result.get(self.field_config["content_field"]),
                    "title": self._resolve_result_title(result),
                    "filepath": filepath,
                    "url": citation_url,
                    "main_title": result.get(self.field_config.get("main_title_field")),
                    "sub_title": result.get(self.field_config.get("sub_title_field")),
                    "publisher": result.get(self.field_config.get("publisher_field")),
                    "document_code": result.get(self.field_config.get("document_code_field")),
                    "document_category": result.get(self.field_config.get("document_category_field")),
                    "description": result.get(self.field_config.get("description_field")),
                    "full_headers": result.get(self.field_config.get("full_headers_field")),
                    "h1": result.get(self.field_config.get("h1_field")),
                    "h2": result.get(self.field_config.get("h2_field")),
                    "h3": result.get(self.field_config.get("h3_field")),
                    "timestamp": result.get(self.field_config.get("timestamp_field")),
                    "publish_date": result.get(self.field_config.get("publish_date_field")),
                    "score": result.get("@search.score", 0.0),
                    "reranker_score": result.get("@search.reranker_score"),
                    "image_urls": self._process_image_mapping(result.get(self.field_config.get("image_mapping_field")))
                }
                
                # Log first processed result
                if result_count == 1:
                    logger.info(f"=== FIRST PROCESSED RESULT ===")
                    logger.info(f"Content: {processed_result['content'][:200] if processed_result['content'] else 'None'}")
                    logger.info(f"Title: {processed_result['title']}")
                    logger.info(f"URL: {processed_result['url']}")
                    logger.info(f"=== END PROCESSED ===")
                
                processed_results.append(processed_result)
            
            logger.info(f"Search completed - Found {len(processed_results)} results")
            
            # Log detailed results with scores and reranker info
            if processed_results:
                logger.info(f"=== SEARCH RESULTS DETAILS ===")
                logger.info(f"Semantic Reranker Enabled: {self.enable_semantic_reranker}")
                logger.info(f"")
                for i, res in enumerate(processed_results[:5], 1):  # First 5 results
                    reranker_info = f" | Reranker: {res.get('reranker_score'):.4f}" if res.get('reranker_score') is not None else " | Reranker: N/A"
                    logger.info(
                        f"  [{i}] Score: {res.get('score', 0):.4f}{reranker_info} | "
                        f"Title: {res.get('title', 'N/A')[:60]}"
                    )
                logger.info(f"")
                logger.info(f"=== END SEARCH RESULTS ===")
            
            return processed_results
            
        except HttpResponseError as e:
            # Do NOT disable semantic search; surface a clear error instead.
            if "Semantic search is not enabled" in str(e) or "SemanticQueriesNotAvailable" in str(e):
                logger.error(
                    "Semantic search is not enabled for this Azure AI Search service. "
                    "Please enable Semantic Search in the service or use a semantic-enabled SKU."
                )
                raise

            logger.error(f"Search error: {str(e)}", exc_info=True)
            raise
        except Exception as e:
            logger.error(f"Search error: {str(e)}", exc_info=True)
            raise
    
    async def _search_with_agentic_mode(
        self,
        query: str,
        query_vector: Optional[List[float]] = None,
        top_k: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Execute search with agentic retrieval mode enabled.
        Uses Azure AI Search's advanced capabilities with enhanced query understanding.
        
        In agentic mode:
        - Optimized for AI-driven query interpretation
        - Respects semantic reranker configuration (enabled/disabled)
        - Single search call with optional semantic ranking
        - Designed for integration with LLM-based agents
        
        Args:
            query: User query
            query_vector: Optional pre-computed embedding
            top_k: Number of results to return
        
        Returns:
            Search results with optional semantic ranking
        """
        from ..config import get_select_fields
        
        logger.info(f"[AGENTIC MODE] Executing search with Azure AI understanding: '{query}'")
        
        k = top_k if top_k is not None else self.top_k
        
        # Build search parameters - respect semantic reranker setting even in agentic mode
        search_params = {
            "search_text": query,
            "top": k,
            "select": get_select_fields(),
            "query_language": "zh-cn"
        }
        
        # Add semantic search if enabled
        if self.enable_semantic_reranker:
            search_params["query_type"] = QueryType.SEMANTIC
            search_params["semantic_configuration_name"] = self.field_config["semantic_config_name"]
            logger.info(f"[AGENTIC MODE] Semantic reranking: ENABLED")
        else:
            logger.info(f"[AGENTIC MODE] Semantic reranking: DISABLED")
        
        # Add vector search if embedding provided or can be generated
        if query_vector is None:
            try:
                query_vector = self._generate_embedding(query)
            except Exception as e:
                logger.warning(f"Embedding generation failed in agentic mode: {e}")
        
        if query_vector:
            vector_query = VectorizedQuery(
                vector=query_vector,
                k=50,
                fields=self.field_config["vector_field"]
            )
            search_params["vector_queries"] = [vector_query]
            logger.info(f"[AGENTIC MODE] Hybrid search enabled (keyword + vector + semantic)")
        else:
            logger.info(f"[AGENTIC MODE] Semantic keyword search (no vector)")
        
        try:
            results = self.search_client.search(**search_params)
            
            processed_results = []
            result_count = 0
            
            for result in results:
                result_count += 1
                
                # Construct citation URL logic
                raw_url = result.get(self.field_config["url_field"])
                filepath = result.get(self.field_config["filepath_field"])
                
                page = self._extract_page_number(result)

                citation_url = self._ensure_blob_sas_url(raw_url, is_image=False)
                if AzureSearchConfig.BASE_URL and filepath:
                    base = AzureSearchConfig.BASE_URL.rstrip('/')
                    path = filepath.replace('\\', '/').lstrip('/')
                    encoded_path = quote(path, safe='/')
                    citation_url = self._ensure_blob_sas_url(f"{base}/{encoded_path}", is_image=False)
                else:
                    if not filepath:
                        logger.warning(
                            f"[CITATION] Missing filepath for result id={result.get(self.field_config['id_field'])} "
                            f"title={result.get(self.field_config['title_field'])} raw_url={raw_url}"
                        )
                    if not AzureSearchConfig.BASE_URL:
                        logger.warning(
                            f"[CITATION] BASE_URL not set; using raw_url for result id={result.get(self.field_config['id_field'])}"
                        )
                    if not raw_url and not filepath:
                        logger.warning(
                            f"[CITATION] No raw_url or filepath available for result id={result.get(self.field_config['id_field'])}"
                        )

                citation_url = self._append_page_anchor(citation_url, page)

                processed_result = {
                    "id": result.get(self.field_config["id_field"]),
                    "content": result.get(self.field_config["content_field"]),
                    "title": self._resolve_result_title(result),
                    "filepath": filepath,
                    "url": citation_url,
                    "main_title": result.get(self.field_config.get("main_title_field")),
                    "sub_title": result.get(self.field_config.get("sub_title_field")),
                    "publisher": result.get(self.field_config.get("publisher_field")),
                    "document_code": result.get(self.field_config.get("document_code_field")),
                    "document_category": result.get(self.field_config.get("document_category_field")),
                    "description": result.get(self.field_config.get("description_field")),
                    "full_headers": result.get(self.field_config.get("full_headers_field")),
                    "h1": result.get(self.field_config.get("h1_field")),
                    "h2": result.get(self.field_config.get("h2_field")),
                    "h3": result.get(self.field_config.get("h3_field")),
                    "timestamp": result.get(self.field_config.get("timestamp_field")),
                    "publish_date": result.get(self.field_config.get("publish_date_field")),
                    "score": result.get("@search.score", 0.0),
                    "reranker_score": result.get("@search.reranker_score"),
                    "image_urls": self._process_image_mapping(result.get(self.field_config.get("image_mapping_field")))
                }
                
                processed_results.append(processed_result)
            
            logger.info(f"[AGENTIC MODE] Search completed - Found {len(processed_results)} results")
            
            # Log detailed results
            if processed_results:
                logger.info(f"=== AGENTIC MODE RESULTS ===")
                semantic_status = "ENABLED" if self.enable_semantic_reranker else "DISABLED"
                logger.info(f"Semantic Reranker: {semantic_status}")
                logger.info(f"")
                for i, res in enumerate(processed_results[:5], 1):
                    reranker_info = f" | Reranker: {res.get('reranker_score'):.4f}" if res.get('reranker_score') is not None else " | Reranker: N/A"
                    logger.info(
                        f"  [{i}] Score: {res.get('score', 0):.4f}{reranker_info} | "
                        f"Title: {res.get('title', 'N/A')[:60]}"
                    )
                logger.info(f"")
                logger.info(f"=== END AGENTIC RESULTS ===")
            
            return processed_results
            
        except Exception as e:
            logger.error(f"[AGENTIC MODE] Search error: {str(e)}", exc_info=True)
            raise
    
    async def parallel_search(
        self,
        queries: List[str],
        top_k: Optional[int] = None
    ) -> List[List[Dict[str, Any]]]:
        """
        Execute multiple searches in parallel.
        
        Args:
            queries: List of query strings to search for
            top_k: Number of results per query
            
        Returns:
            List of search result lists, one per query
        """
        logger.info(f"Executing parallel search for {len(queries)} queries")
        
        import asyncio
        
        # Create search tasks
        tasks = [self.search(query, top_k=top_k) for query in queries]
        
        # Execute all searches in parallel
        all_results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Process results and handle errors
        processed_results = []
        for i, result in enumerate(all_results):
            if isinstance(result, Exception):
                logger.error(f"Query {i+1} failed: {str(result)}")
                processed_results.append([])
            else:
                processed_results.append(result)
                logger.info(f"Query {i+1} returned {len(result)} results")
        
        logger.info(f"Parallel search completed: {len(processed_results)} result sets")
        return processed_results
    
    def parallel_search_sync(
        self,
        queries: List[str],
        max_workers: int = 5
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        Execute multiple search queries in parallel.
        
        Args:
            queries: List of search queries
            max_workers: Maximum number of parallel workers
        
        Returns:
            Dictionary mapping queries to their results
        """
        logger.info(f"Executing {len(queries)} parallel searches")
        
        results_map = {}
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all search tasks
            future_to_query = {
                executor.submit(self.search, query): query
                for query in queries
            }
            
            # Collect results as they complete
            for future in as_completed(future_to_query):
                query = future_to_query[future]
                try:
                    results = future.result()
                    results_map[query] = results
                    logger.info(f"Completed search for: '{query}'")
                except Exception as e:
                    logger.error(f"Error in parallel search for '{query}': {e}")
                    results_map[query] = []
        
        logger.info(f"Parallel search completed for {len(results_map)} queries")
        return results_map
    
    def update_config(
        self,
        enable_semantic_reranker: Optional[bool] = None,
        enable_agentic_retrieval: Optional[bool] = None,
        top_k: Optional[int] = None
    ):
        """
        Update tool configuration dynamically.
        
        Args:
            enable_semantic_reranker: New semantic reranker setting
            enable_agentic_retrieval: New agentic retrieval setting
            top_k: New top K value
        """
        if enable_semantic_reranker is not None:
            self.enable_semantic_reranker = enable_semantic_reranker
            logger.info(f"Semantic reranker set to: {enable_semantic_reranker}")
        
        if enable_agentic_retrieval is not None:
            self.enable_agentic_retrieval = enable_agentic_retrieval
            logger.info(f"Agentic retrieval set to: {enable_agentic_retrieval}")
        
        if top_k is not None:
            self.top_k = top_k
            logger.info(f"Top K set to: {top_k}")


# Tool function for agent framework integration
def create_search_tool(
    enable_semantic_reranker: bool = True,
    enable_agentic_retrieval: bool = True,
    top_k: int = 20
) -> AzureAISearchTool:
    """
    Factory function to create an Azure AI Search tool instance.
    
    Args:
        enable_semantic_reranker: Enable semantic reranking
        enable_agentic_retrieval: Enable agentic retrieval
        top_k: Number of results to return
    
    Returns:
        Configured AzureAISearchTool instance
    """
    return AzureAISearchTool(
        enable_semantic_reranker=enable_semantic_reranker,
        enable_agentic_retrieval=enable_agentic_retrieval,
        top_k=top_k
    )

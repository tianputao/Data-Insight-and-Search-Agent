"""
Configuration module for Agentic RAG application.
Loads environment variables and provides centralized configuration management.
Matches the actual index-dev-figure-01-chunk schema.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file
env_path = Path(__file__).parent.parent.parent / '.env'
load_dotenv(dotenv_path=env_path, override=True)


class AzureOpenAIConfig:
    """Azure OpenAI service configuration."""
    
    ENDPOINT = os.getenv('AZURE_OPENAI_ENDPOINT')
    API_KEY = os.getenv('AZURE_OPENAI_API_KEY')
    AUTH_MODE = os.getenv('AZURE_OPENAI_AUTH_MODE', 'auto').strip().lower()  # auto | key | aad
    API_VERSION = os.getenv('AZURE_OPENAI_API_VERSION', '2025-04-01-preview')
    GPT_DEPLOYMENT = os.getenv('AZURE_OPENAI_GPT_DEPLOYMENT', 'gpt-5.1')
    SMALL_GPT_DEPLOYMENT = (
        os.getenv('AZURE_OPENAI_GPT_SMALL_DEPLOYMENT', GPT_DEPLOYMENT).strip()
        or GPT_DEPLOYMENT
    )

    @classmethod
    def use_api_key(cls) -> bool:
        if cls.AUTH_MODE == 'key':
            return True
        if cls.AUTH_MODE == 'aad':
            return False
        # auto mode: keep backward compatibility
        return bool(cls.API_KEY)
    
    # Embedding configuration - MUST match your index vector dimensions
    EMBEDDING_DEPLOYMENT = os.getenv('AZURE_OPENAI_EMBEDDING_DEPLOYMENT', 'text-embedding-3-large')
    EMBEDDING_MODEL = os.getenv('AZURE_OPENAI_EMBEDDING_MODEL', 'text-embedding-3-large')
    EMBEDDING_DIMENSIONS = int(os.getenv('AZURE_OPENAI_EMBEDDING_DIMENSIONS', '3072'))


_REASONING_EFFORTS = ('none', 'low', 'medium', 'high')


def _read_reasoning_effort(name: str, default: str = 'medium') -> str:
    value = os.getenv(name, default).strip().lower()
    return value if value in _REASONING_EFFORTS else default


class AgentReasoningConfig:
    """Per-agent reasoning effort.

    `gpt-5*` deployments reject `temperature` and `top_p` outright and expose
    `reasoning_effort` instead; `maf_runtime.create_agent` sends this only to those models.
    """

    ALLOWED = _REASONING_EFFORTS

    MASTER = _read_reasoning_effort('MASTER_AGENT_REASONING_EFFORT')
    ONTOLOGY = _read_reasoning_effort('ONTOLOGY_AGENT_REASONING_EFFORT')
    METADATA = _read_reasoning_effort('METADATA_AGENT_REASONING_EFFORT')
    DATA_INSIGHT = _read_reasoning_effort('DATA_INSIGHT_AGENT_REASONING_EFFORT')
    SEARCH = _read_reasoning_effort('SEARCH_AGENT_REASONING_EFFORT')


class AzureSearchConfig:
    """Azure AI Search service configuration - Matches index-dev-figure-01-chunk schema."""
    
    ENDPOINT = os.getenv('AZURE_SEARCH_ENDPOINT')
    API_KEY = os.getenv('AZURE_SEARCH_API_KEY')
    INDEX_NAME = os.getenv('AZURE_SEARCH_INDEX_NAME', 'index-dev-figure-01-chunk')
    
    # Core field names - MUST match your index schema exactly
    ID_FIELD = os.getenv('AZURE_SEARCH_ID_FIELD', 'id')
    SESSION_ID_FIELD = os.getenv('AZURE_SEARCH_SESSION_ID_FIELD', 'session_id')
    CONTENT_FIELD = os.getenv('AZURE_SEARCH_CONTENT_FIELD', 'content')
    TITLE_FIELD = os.getenv('AZURE_SEARCH_TITLE_FIELD', 'title')
    FILEPATH_FIELD = os.getenv('AZURE_SEARCH_FILEPATH_FIELD', 'filepath')
    URL_FIELD = os.getenv('AZURE_SEARCH_URL_FIELD', 'url')
    METADATA_FIELD = os.getenv('AZURE_SEARCH_METADATA_FIELD', 'metadata')
    DOC_METADATA_FIELD = os.getenv('AZURE_SEARCH_DOC_METADATA_FIELD', 'doc_metadata')
    DESCRIPTION_FIELD = os.getenv('AZURE_SEARCH_DESCRIPTION_FIELD', 'description')
    
    # Document access configuration
    # Prefer AZURE_* names, but keep backward compatibility with legacy BASE_URL/SAS_TOKEN.
    BASE_URL = os.getenv('AZURE_BLOB_BASE_URL') or os.getenv('BASE_URL', '')
    SAS_TOKEN = os.getenv('AZURE_BLOB_SAS_TOKEN') or os.getenv('SAS_TOKEN', '')
    
    # Image configuration
    IMAGE_BASE_URL = os.getenv('AZURE_IMAGE_BASE_URL', '')  # Base URL for image storage
    IMAGE_SAS_TOKEN = os.getenv('AZURE_IMAGE_SAS_TOKEN', '')  # SAS token for image access
    
    # Vector fields - dimensions must match embedding model
    VECTOR_FIELD = os.getenv('AZURE_SEARCH_VECTOR_FIELD', 'contentVector')
    METADATA_VECTOR_FIELD = os.getenv('AZURE_SEARCH_METADATA_VECTOR_FIELD', 'full_metadata_vector')

    VECTOR_DIMENSIONS = int(os.getenv('AZURE_SEARCH_VECTOR_DIMENSIONS', '3072'))
    
    # Document metadata fields
    MAIN_TITLE_FIELD = os.getenv('AZURE_SEARCH_MAIN_TITLE_FIELD', 'main_title')
    SUB_TITLE_FIELD = os.getenv('AZURE_SEARCH_SUB_TITLE_FIELD', 'sub_title')
    PUBLISHER_FIELD = os.getenv('AZURE_SEARCH_PUBLISHER_FIELD', 'publisher')
    DOCUMENT_CODE_FIELD = os.getenv('AZURE_SEARCH_DOCUMENT_CODE_FIELD', 'document_code')
    DOCUMENT_CATEGORY_FIELD = os.getenv('AZURE_SEARCH_DOCUMENT_CATEGORY_FIELD', 'document_category')
    DOCUMENT_SCHEMA_FIELD = os.getenv('AZURE_SEARCH_DOCUMENT_SCHEMA_FIELD', 'document_schema')
    
    # Language fields
    PRIMARY_LANGUAGE_FIELD = os.getenv('AZURE_SEARCH_PRIMARY_LANGUAGE_FIELD', 'primary_language')
    SECONDARY_LANGUAGE_FIELD = os.getenv('AZURE_SEARCH_SECONDARY_LANGUAGE_FIELD', 'secondary_language')
    MAIN_TITLE_SEC_LANGUAGE_FIELD = os.getenv('AZURE_SEARCH_MAIN_TITLE_SEC_LANGUAGE_FIELD', 'main_title_sec_language')
    SUB_TITLE_SEC_LANGUAGE_FIELD = os.getenv('AZURE_SEARCH_SUB_TITLE_SEC_LANGUAGE_FIELD', 'sub_title_sec_language')
    
    # Header hierarchy fields
    FULL_HEADERS_FIELD = os.getenv('AZURE_SEARCH_FULL_HEADERS_FIELD', 'full_headers')
    H1_FIELD = os.getenv('AZURE_SEARCH_H1_FIELD', 'h1')
    H2_FIELD = os.getenv('AZURE_SEARCH_H2_FIELD', 'h2')
    H3_FIELD = os.getenv('AZURE_SEARCH_H3_FIELD', 'h3')
    H4_FIELD = os.getenv('AZURE_SEARCH_H4_FIELD', 'h4')
    H5_FIELD = os.getenv('AZURE_SEARCH_H5_FIELD', 'h5')
    H6_FIELD = os.getenv('AZURE_SEARCH_H6_FIELD', 'h6')
    
    # Timestamp fields
    TIMESTAMP_FIELD = os.getenv('AZURE_SEARCH_TIMESTAMP_FIELD', 'timestamp')
    PUBLISH_DATE_FIELD = os.getenv('AZURE_SEARCH_PUBLISH_DATE_FIELD', 'publish_date')
    
    # Image mapping
    IMAGE_MAPPING_FIELD = os.getenv('AZURE_SEARCH_IMAGE_MAPPING_FIELD', 'image_mapping')
    
    # Semantic search configuration (from your index schema)
    SEMANTIC_CONFIG_NAME = os.getenv('AZURE_SEARCH_SEMANTIC_CONFIG', 'default')
    
    # Vector search profile
    VECTOR_SEARCH_PROFILE = os.getenv('AZURE_SEARCH_VECTOR_PROFILE', 'vectorSearchProfile')


class AzureAIFoundryConfig:
    """Azure AI Foundry configuration for monitoring and evaluation."""
    
    CONNECTION_STRING = os.getenv('AZURE_AI_PROJECT_CONNECTION_STRING')


class DatabricksConfig:
    """Azure Databricks Unity Catalog configuration for data insight and metadata agents."""
    
    # Workspace connection
    HOST = os.getenv('DATABRICKS_HOST', '')          # e.g. https://adb-xxx.azuredatabricks.net
    TOKEN = os.getenv('DATABRICKS_TOKEN', '')         # PAT or AAD token
    HTTP_PATH = os.getenv('DATABRICKS_HTTP_PATH', '') # SQL warehouse HTTP path
    
    # Default catalog and schema(s) context
    CATALOG = os.getenv('DATABRICKS_CATALOG', 'main')

    # Comma-separated list of schemas to expose to agents; first item is the default.
    # Supports both new DATABRICKS_SCHEMAS and legacy DATABRICKS_SCHEMA env vars.
    _schemas_raw = os.getenv('DATABRICKS_SCHEMAS', os.getenv('DATABRICKS_SCHEMA', 'default'))
    SCHEMAS = [s.strip() for s in _schemas_raw.split(',') if s.strip()] or ['default']
    SCHEMA = SCHEMAS[0]  # primary / default schema (backward-compatible)
    
    # Maximum rows returned for data insight queries
    MAX_ROWS = int(os.getenv('DATABRICKS_MAX_ROWS', '500'))
    
    # Query timeout in seconds
    QUERY_TIMEOUT = int(os.getenv('DATABRICKS_QUERY_TIMEOUT', '120'))

    # Process-local, object-scoped UC tool cache. 0 means no expiry.
    METADATA_CACHE_TTL_SECONDS = max(
        0,
        int(os.getenv('DATABRICKS_METADATA_CACHE_TTL_SECONDS', '900')),
    )
    METADATA_AGENT_TIMEOUT_SECONDS = max(
        1,
        int(os.getenv('METADATA_AGENT_TIMEOUT_SECONDS', '120')),
    )
    METADATA_AGENT_MAX_MODEL_ROUNDTRIPS = max(
        1,
        int(os.getenv('METADATA_AGENT_MAX_MODEL_ROUNDTRIPS', '5')),
    )
    METADATA_AGENT_MAX_FUNCTION_CALLS = max(
        1,
        int(os.getenv('METADATA_AGENT_MAX_FUNCTION_CALLS', '8')),
    )

    # Candidate recall bounds. The index scan stays server-side; only candidates reach the prompt.
    METADATA_INDEX_MAX_TABLES = max(
        1,
        int(os.getenv('METADATA_INDEX_MAX_TABLES', '500')),
    )
    METADATA_CANDIDATE_MAX_TABLES = max(
        1,
        int(os.getenv('METADATA_CANDIDATE_MAX_TABLES', '12')),
    )
    # Used only when recall finds no candidate at all, so a small schema still resolves.
    METADATA_SNAPSHOT_MAX_TABLES = max(
        1,
        int(os.getenv('METADATA_SNAPSHOT_MAX_TABLES', '40')),
    )
    
    @classmethod
    def is_configured(cls) -> bool:
        """Return True when the minimum required variables are present."""
        return bool(cls.HOST and cls.TOKEN and cls.HTTP_PATH)


class OntologyConfig:
    """Read-only Owlready2 ontology runtime configuration."""

    _project_root = Path(__file__).parent.parent.parent
    _directory_value = Path(
        os.getenv('ONTOLOGY_DIR', str(_project_root / 'Ontology'))
    ).expanduser()
    DIRECTORY = (
        _directory_value
        if _directory_value.is_absolute()
        else (_project_root / _directory_value).resolve()
    )
    FILE_GLOB = os.getenv('ONTOLOGY_FILE_GLOB', '**/*.owl')

    ENABLE_REASONER = os.getenv('ONTOLOGY_ENABLE_REASONER', 'false').lower() == 'true'
    REASONER = os.getenv('ONTOLOGY_REASONER', 'hermit').strip().lower()
    ONLY_LOCAL = os.getenv('ONTOLOGY_ONLY_LOCAL', 'true').lower() == 'true'

    MAX_RESULTS = max(1, int(os.getenv('ONTOLOGY_MAX_RESULTS', '25')))
    MAX_DEPTH = max(1, int(os.getenv('ONTOLOGY_MAX_DEPTH', '5')))
    MAX_PATHS = max(1, int(os.getenv('ONTOLOGY_MAX_PATHS', '10')))
    MAX_NODES = max(10, int(os.getenv('ONTOLOGY_MAX_NODES', '250')))
    FUZZY_THRESHOLD = min(
        1.0,
        max(0.0, float(os.getenv('ONTOLOGY_FUZZY_THRESHOLD', '0.62'))),
    )
    AGENT_TIMEOUT_SECONDS = max(
        1,
        int(os.getenv('ONTOLOGY_AGENT_TIMEOUT_SECONDS', '90')),
    )
    AGENT_MAX_MODEL_ROUNDTRIPS = max(
        1,
        int(os.getenv('ONTOLOGY_AGENT_MAX_MODEL_ROUNDTRIPS', '4')),
    )
    AGENT_MAX_FUNCTION_CALLS = max(
        1,
        int(os.getenv('ONTOLOGY_AGENT_MAX_FUNCTION_CALLS', '6')),
    )
    # Optional safety net for handed-off evidence size. 0 disables any size-based dropping.
    CONTEXT_MAX_CHARS = max(0, int(os.getenv('ONTOLOGY_CONTEXT_MAX_CHARS', '0')))
    # Below this composite confidence the deterministic lookup hands off to the tool-using agent.
    ESCALATION_MIN_CONFIDENCE = min(
        1.0,
        max(0.0, float(os.getenv('ONTOLOGY_ESCALATION_MIN_CONFIDENCE', '0.5'))),
    )


class AppConfig:
    """Application-level configuration."""
    
    # Logging
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
    
    # Feature Defaults
    DEFAULT_ENABLE_SEMANTIC_RERANKER = os.getenv('DEFAULT_ENABLE_SEMANTIC_RERANKER', 'true').lower() == 'true'
    DEFAULT_ENABLE_AGENTIC_RETRIEVAL = os.getenv('DEFAULT_ENABLE_AGENTIC_RETRIEVAL', 'true').lower() == 'true'
    LOG_DIR = Path(__file__).parent.parent.parent / 'logs'
    TMP_DIR = Path(__file__).parent.parent.parent / 'tmp'
    DATA_DIR = Path(__file__).parent.parent.parent / 'data'
    
    # Search configuration
    MAX_SEARCH_RESULTS = int(os.getenv('MAX_SEARCH_RESULTS', '20'))
    # MasterAgent query engine limits. MAF applies these to one function-invocation loop.
    QUERY_ENGINE_MAX_SEARCH_ATTEMPTS = max(1, int(os.getenv('QUERY_ENGINE_MAX_SEARCH_ATTEMPTS', '5')))
    QUERY_ENGINE_MAX_CONSECUTIVE_ERRORS = max(1, int(os.getenv('QUERY_ENGINE_MAX_CONSECUTIVE_ERRORS', '3')))
    QUERY_ENGINE_MAX_MODEL_ROUNDTRIPS = max(
        1,
        int(os.getenv('QUERY_ENGINE_MAX_MODEL_ROUNDTRIPS', '8')),
    )
    QUERY_ENGINE_MAX_FUNCTION_CALLS = max(
        1,
        int(os.getenv('QUERY_ENGINE_MAX_FUNCTION_CALLS', '12')),
    )
    SESSION_RESPONSE_CACHE_ENABLED = os.getenv(
        'SESSION_RESPONSE_CACHE_ENABLED',
        'true',
    ).lower() == 'true'
    SESSION_RESPONSE_CACHE_TTL_SECONDS = max(
        0,
        int(os.getenv('SESSION_RESPONSE_CACHE_TTL_SECONDS', '3600')),
    )
    
    # Feature flags
    DEFAULT_ENABLE_SEMANTIC_RERANKER = os.getenv('DEFAULT_ENABLE_SEMANTIC_RERANKER', 'true').lower() == 'true'
    DEFAULT_ENABLE_AGENTIC_RETRIEVAL = os.getenv('DEFAULT_ENABLE_AGENTIC_RETRIEVAL', 'true').lower() == 'true'
    DEFAULT_ENABLE_ONTOLOGY = os.getenv('DEFAULT_ENABLE_ONTOLOGY', 'true').lower() == 'true'
    
    # Ensure directories exist
    LOG_DIR.mkdir(exist_ok=True)
    TMP_DIR.mkdir(exist_ok=True)
    DATA_DIR.mkdir(exist_ok=True)


def validate_config():
    """Validate that all required configuration values are set."""
    
    required_configs = {
        'AZURE_OPENAI_ENDPOINT': AzureOpenAIConfig.ENDPOINT,
        'AZURE_SEARCH_ENDPOINT': AzureSearchConfig.ENDPOINT,
        'AZURE_SEARCH_API_KEY': AzureSearchConfig.API_KEY,
        'AZURE_SEARCH_INDEX_NAME': AzureSearchConfig.INDEX_NAME,
    }

    if AzureOpenAIConfig.use_api_key() and not AzureOpenAIConfig.API_KEY:
        required_configs['AZURE_OPENAI_API_KEY'] = AzureOpenAIConfig.API_KEY
    
    missing_configs = [key for key, value in required_configs.items() if not value]
    
    if missing_configs:
        raise ValueError(
            f"Missing required configuration values: {', '.join(missing_configs)}. "
            f"Please check your .env file."
        )
    
    return True


def get_search_field_config() -> dict:
    """
    Get search index field configuration as a dictionary.
    Returns all field names matching the index-dev-figure-01-chunk schema.
    
    Returns:
        Dictionary of field names for Azure AI Search
    """
    return {
        # Core fields
        'id_field': AzureSearchConfig.ID_FIELD,
        'session_id_field': AzureSearchConfig.SESSION_ID_FIELD,
        'content_field': AzureSearchConfig.CONTENT_FIELD,
        'title_field': AzureSearchConfig.TITLE_FIELD,
        'filepath_field': AzureSearchConfig.FILEPATH_FIELD,
        'url_field': AzureSearchConfig.URL_FIELD,
        'metadata_field': AzureSearchConfig.METADATA_FIELD,
        'doc_metadata_field': AzureSearchConfig.DOC_METADATA_FIELD,
        'description_field': AzureSearchConfig.DESCRIPTION_FIELD,
        
        # Vector fields
        'vector_field': AzureSearchConfig.VECTOR_FIELD,
        'metadata_vector_field': AzureSearchConfig.METADATA_VECTOR_FIELD,
        'vector_dimensions': AzureSearchConfig.VECTOR_DIMENSIONS,
        'vector_search_profile': AzureSearchConfig.VECTOR_SEARCH_PROFILE,
        
        # Document metadata
        'main_title_field': AzureSearchConfig.MAIN_TITLE_FIELD,
        'sub_title_field': AzureSearchConfig.SUB_TITLE_FIELD,
        'publisher_field': AzureSearchConfig.PUBLISHER_FIELD,
        'document_code_field': AzureSearchConfig.DOCUMENT_CODE_FIELD,
        'document_category_field': AzureSearchConfig.DOCUMENT_CATEGORY_FIELD,
        'document_schema_field': AzureSearchConfig.DOCUMENT_SCHEMA_FIELD,
        
        # Language fields
        'primary_language_field': AzureSearchConfig.PRIMARY_LANGUAGE_FIELD,
        'secondary_language_field': AzureSearchConfig.SECONDARY_LANGUAGE_FIELD,
        'main_title_sec_language_field': AzureSearchConfig.MAIN_TITLE_SEC_LANGUAGE_FIELD,
        'sub_title_sec_language_field': AzureSearchConfig.SUB_TITLE_SEC_LANGUAGE_FIELD,
        
        # Header hierarchy
        'full_headers_field': AzureSearchConfig.FULL_HEADERS_FIELD,
        'h1_field': AzureSearchConfig.H1_FIELD,
        'h2_field': AzureSearchConfig.H2_FIELD,
        'h3_field': AzureSearchConfig.H3_FIELD,
        'h4_field': AzureSearchConfig.H4_FIELD,
        'h5_field': AzureSearchConfig.H5_FIELD,
        'h6_field': AzureSearchConfig.H6_FIELD,
        
        # Timestamps
        'timestamp_field': AzureSearchConfig.TIMESTAMP_FIELD,
        'publish_date_field': AzureSearchConfig.PUBLISH_DATE_FIELD,
        
        # Image mapping
        'image_mapping_field': AzureSearchConfig.IMAGE_MAPPING_FIELD,
        
        # Search configuration
        'semantic_config_name': AzureSearchConfig.SEMANTIC_CONFIG_NAME,
    }


def get_select_fields() -> list:
    """
    Get list of fields to return in search results.
    Includes most important fields for display.
    
    Returns:
        List of field names to select
    """
    return [
        AzureSearchConfig.ID_FIELD,
        AzureSearchConfig.CONTENT_FIELD,
        AzureSearchConfig.TITLE_FIELD,
        AzureSearchConfig.FILEPATH_FIELD,
        AzureSearchConfig.URL_FIELD,
        AzureSearchConfig.MAIN_TITLE_FIELD,
        AzureSearchConfig.SUB_TITLE_FIELD,
        AzureSearchConfig.PUBLISHER_FIELD,
        AzureSearchConfig.DOCUMENT_CODE_FIELD,
        AzureSearchConfig.DOCUMENT_CATEGORY_FIELD,
        AzureSearchConfig.FULL_HEADERS_FIELD,
        AzureSearchConfig.DESCRIPTION_FIELD,
        AzureSearchConfig.TIMESTAMP_FIELD,
        AzureSearchConfig.PUBLISH_DATE_FIELD,
        AzureSearchConfig.H1_FIELD,
        AzureSearchConfig.H2_FIELD,
        AzureSearchConfig.H3_FIELD,
        AzureSearchConfig.IMAGE_MAPPING_FIELD,  # Include image mapping
    ]

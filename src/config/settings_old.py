"""
Configuration module for Agentic RAG application.
Loads environment variables and provides centralized configuration management.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file
env_path = Path(__file__).parent.parent.parent / '.env'
load_dotenv(dotenv_path=env_path)


class AzureOpenAIConfig:
    """Azure OpenAI service configuration."""
    
    ENDPOINT = os.getenv('AZURE_OPENAI_ENDPOINT')
    API_KEY = os.getenv('AZURE_OPENAI_API_KEY')
    API_VERSION = os.getenv('AZURE_OPENAI_API_VERSION', '2024-08-01-preview')
    GPT_DEPLOYMENT = os.getenv('AZURE_OPENAI_GPT_DEPLOYMENT', 'gpt-5.1')
    EMBEDDING_DEPLOYMENT = os.getenv('AZURE_OPENAI_EMBEDDING_DEPLOYMENT', 'text-embedding-3-large')


class AzureSearchConfig:
    """Azure AI Search service configuration."""
    
    ENDPOINT = os.getenv('AZURE_SEARCH_ENDPOINT')
    API_KEY = os.getenv('AZURE_SEARCH_API_KEY')
    INDEX_NAME = os.getenv('AZURE_SEARCH_INDEX_NAME')


class AzureAIFoundryConfig:
    """Azure AI Foundry configuration for monitoring and evaluation."""
    
    CONNECTION_STRING = os.getenv('AZURE_AI_PROJECT_CONNECTION_STRING')


class AppConfig:
    """Application-level configuration."""
    
    # Logging
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
    LOG_DIR = Path(__file__).parent.parent.parent / 'logs'
    TMP_DIR = Path(__file__).parent.parent.parent / 'tmp'
    DATA_DIR = Path(__file__).parent.parent.parent / 'data'
    
    # Search configuration
    MAX_SEARCH_RESULTS = int(os.getenv('MAX_SEARCH_RESULTS', '20'))
    DEFAULT_TOP_K = int(os.getenv('DEFAULT_TOP_K', '5'))
    
    # Feature flags
    DEFAULT_ENABLE_SEMANTIC_RERANKER = os.getenv('DEFAULT_ENABLE_SEMANTIC_RERANKER', 'true').lower() == 'true'
    DEFAULT_ENABLE_AGENTIC_RETRIEVAL = os.getenv('DEFAULT_ENABLE_AGENTIC_RETRIEVAL', 'true').lower() == 'true'
    
    # Ensure directories exist
    LOG_DIR.mkdir(exist_ok=True)
    TMP_DIR.mkdir(exist_ok=True)
    DATA_DIR.mkdir(exist_ok=True)


def validate_config():
    """Validate that all required configuration values are set."""
    
    required_configs = {
        'AZURE_OPENAI_ENDPOINT': AzureOpenAIConfig.ENDPOINT,
        'AZURE_OPENAI_API_KEY': AzureOpenAIConfig.API_KEY,
        'AZURE_SEARCH_ENDPOINT': AzureSearchConfig.ENDPOINT,
        'AZURE_SEARCH_API_KEY': AzureSearchConfig.API_KEY,
        'AZURE_SEARCH_INDEX_NAME': AzureSearchConfig.INDEX_NAME,
    }
    
    missing_configs = [key for key, value in required_configs.items() if not value]
    
    if missing_configs:
        raise ValueError(
            f"Missing required configuration values: {', '.join(missing_configs)}. "
            f"Please check your .env file."
        )
    
    return True

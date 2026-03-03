"""Main package initialization."""

from .config import validate_config

__version__ = "1.0.0"

# Validate configuration on import
try:
    validate_config()
except ValueError as e:
    import warnings
    warnings.warn(f"Configuration validation failed: {e}")

__all__ = ['validate_config']

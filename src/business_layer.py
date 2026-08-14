"""Workspace-level business semantic layer authored by business users.

The document is kept as a data file rather than a Python module or Skill so it is never
imported or executed; swapping the two function bodies moves storage to Blob or a database.
"""

from __future__ import annotations

from pathlib import Path
from threading import Lock

from .utils import get_logger

logger = get_logger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent
BUSINESS_LAYER_PATH = _PROJECT_ROOT / "data" / "business_layer.md"

# The document is injected into every analytical request, so bound how much context it can take.
MAX_BUSINESS_LAYER_CHARS = 20000

_LOCK = Lock()


def _sanitize(content: str) -> str:
    """Normalize newlines, drop control characters, and enforce the length cap."""
    normalized = (content or "").replace("\r\n", "\n").replace("\r", "\n")
    cleaned = "".join(
        char for char in normalized if char in "\n\t" or not (ord(char) < 32 or ord(char) == 127)
    )
    return cleaned[:MAX_BUSINESS_LAYER_CHARS]


def load_business_layer() -> str:
    """Return the saved workspace business layer document, or an empty string."""
    try:
        return BUSINESS_LAYER_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError as exc:
        logger.error("Could not read the business layer document: %s", exc)
        return ""


def save_business_layer(content: str) -> str:
    """Persist the workspace business layer document and return the stored text."""
    sanitized = _sanitize(content)
    with _LOCK:
        BUSINESS_LAYER_PATH.parent.mkdir(parents=True, exist_ok=True)
        BUSINESS_LAYER_PATH.write_text(sanitized, encoding="utf-8")
    logger.info("Business layer document saved (chars=%s)", len(sanitized))
    return sanitized

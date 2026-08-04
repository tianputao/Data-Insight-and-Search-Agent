"""Prompts package.

Each sub-agent's prompt lives in its own module; this package re-exports them
so existing `from src.prompts import XXX_PROMPT` imports keep working.
"""

from .master import MASTER_AGENT_PROMPT
from .search import (
    SEARCH_AGENT_PROMPT,
    QUERY_PLANNING_PROMPT,
    ANSWER_SYNTHESIS_PROMPT,
    CONVERSATION_CONTEXT_PROMPT,
)
from .ontology import ONTOLOGY_AGENT_PROMPT
from .data_insight import DATA_INSIGHT_AGENT_PROMPT
from .metadata import METADATA_AGENT_PROMPT

__all__ = [
    'MASTER_AGENT_PROMPT',
    'SEARCH_AGENT_PROMPT',
    'DATA_INSIGHT_AGENT_PROMPT',
    'METADATA_AGENT_PROMPT',
    'ONTOLOGY_AGENT_PROMPT',
    'QUERY_PLANNING_PROMPT',
    'ANSWER_SYNTHESIS_PROMPT',
    'CONVERSATION_CONTEXT_PROMPT'
]

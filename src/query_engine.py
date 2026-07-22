"""Request-scoped context for the MasterAgent query engine.

The executable agentic loop is the bounded function-invocation loop owned by
the MasterAgent's MAF chat client: model response -> function calls -> function
results -> next model response. This module stores observable per-request state
used by MasterAgent tools and the Activity stream; it does not run a second
model loop or judge completed answers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Event
from typing import Any, Optional


@dataclass(slots=True)
class ToolOutcome:
    """Observable result of one MasterAgent tool or delegated agent call."""

    name: str
    success: bool
    summary: str = ""
    retryable: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)
    duration_ms: Optional[int] = None


@dataclass(slots=True)
class QueryEngineContext:
    """State isolated to one user turn in one MasterAgent session."""

    original_question: str
    max_search_attempts: int
    stream_context: Any = None
    cancel_event: Optional[Event] = None
    tool_outcomes: list[ToolOutcome] = field(default_factory=list)
    progress: dict[str, Any] = field(default_factory=dict)

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set() if self.cancel_event is not None else False

    @property
    def search_attempts(self) -> int:
        return sum(
            1
            for outcome in self.tool_outcomes
            if outcome.name in {"search_knowledge", "search_multiple_queries"}
        )

    def record_tool(
        self,
        name: str,
        *,
        success: bool,
        summary: str = "",
        retryable: bool = True,
        metadata: Optional[dict[str, Any]] = None,
        duration_ms: Optional[int] = None,
    ) -> None:
        self.tool_outcomes.append(
            ToolOutcome(
                name=name,
                success=success,
                summary=summary,
                retryable=retryable,
                metadata=metadata or {},
                duration_ms=duration_ms,
            )
        )

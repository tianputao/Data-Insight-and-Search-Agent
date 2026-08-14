"""Request-scoped context for the MasterAgent query engine.

The executable agentic loop is the bounded function-invocation loop owned by
the MasterAgent's MAF chat client: model response -> function calls -> function
results -> next model response. This module stores observable per-request state
used by MasterAgent tools and the Activity stream; it does not run a second
model loop or judge completed answers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from threading import Event
from typing import Any, Optional
import unicodedata


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
    enable_ontology: bool = True
    business_layer: str = ""
    stream_context: Any = None
    cancel_event: Optional[Event] = None
    tool_outcomes: list[ToolOutcome] = field(default_factory=list)
    progress: dict[str, Any] = field(default_factory=dict)
    search_request_fingerprints: set[str] = field(default_factory=set)
    search_result_fingerprints: set[str] = field(default_factory=set)
    search_stopped_for_no_gain: bool = False

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

    @staticmethod
    def _normalize_search_query(query: str) -> str:
        text = unicodedata.normalize("NFKC", str(query or "")).casefold()
        return " ".join(re.sub(r"[^\w\u3400-\u9fff]+", " ", text).split())

    def register_search_request(self, queries: list[str]) -> bool:
        """Return false when the same normalized query set already ran this turn."""
        normalized = sorted(
            {
                value
                for query in queries
                if (value := self._normalize_search_query(query))
            }
        )
        fingerprint = " || ".join(normalized)
        if not fingerprint or fingerprint in self.search_request_fingerprints:
            return False
        self.search_request_fingerprints.add(fingerprint)
        return True

    @staticmethod
    def _result_fingerprint(result: dict[str, Any]) -> str:
        for key in ("id", "filepath"):
            value = str(result.get(key) or "").strip()
            if value:
                return f"{key}:{value}"
        title = str(result.get("title") or "").strip()
        content = " ".join(str(result.get("content") or "").split())[:240]
        url = str(result.get("url") or "").strip()
        return f"fallback:{title}|{url}|{content}"

    def register_search_results(self, results: list[dict[str, Any]]) -> dict[str, int | bool]:
        """Record cross-attempt evidence gain without judging answer sufficiency."""
        fingerprints = {
            fingerprint
            for result in results
            if isinstance(result, dict)
            and (fingerprint := self._result_fingerprint(result))
        }
        previous = set(self.search_result_fingerprints)
        new = fingerprints - previous
        overlap = fingerprints & previous
        self.search_result_fingerprints.update(fingerprints)
        no_gain = bool(previous and fingerprints and not new)
        if no_gain:
            self.search_stopped_for_no_gain = True
        return {
            "selected_count": len(fingerprints),
            "new_unique_count": len(new),
            "overlap_count": len(overlap),
            "total_unique_count": len(self.search_result_fingerprints),
            "no_new_evidence": no_gain,
        }

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

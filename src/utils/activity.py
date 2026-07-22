"""Structured user-visible activity events for agent streaming."""

from __future__ import annotations

from typing import Any, Optional
from uuid import uuid4

DELEGATION_AGENTS = {
    "search_knowledge": "SearchAgent",
    "search_multiple_queries": "SearchAgent",
    "delegate_metadata": "MetadataAgent",
    "delegate_data_analysis": "DataInsightAgent",
}

_TOOL_LABELS = {
    "decompose_query": "Decompose query",
    "execute_sql": "Execute SQL",
    "list_schemas": "List schemas",
    "list_tables": "List tables",
    "get_table_details": "Inspect table",
    "search_tables": "Search tables",
    "get_relevant_tables": "Find relevant tables",
}

_DETAIL_KEYS = {
    "decompose_query": "original_query",
    "execute_sql": "sql",
    "list_schemas": "catalog",
    "list_tables": "schema",
    "get_table_details": "table_name",
    "search_tables": "keyword",
    "get_relevant_tables": "question",
}

_SEARCH_TOOLS = {"search_tables"}


def new_activity_id(prefix: str) -> str:
    """Return a short, process-local activity identifier."""
    return f"{prefix}-{uuid4().hex[:12]}"


def delegated_agent(tool_name: str) -> Optional[str]:
    """Return the sub-agent represented by a MasterAgent delegation tool."""
    return DELEGATION_AGENTS.get(tool_name)


def tool_activity(
    tool_name: str,
    args: dict[str, Any],
    call_id: str,
    *,
    agent: str,
    parent_id: Optional[str] = None,
) -> dict[str, Any]:
    """Describe a real tool invocation without inventing model narration."""
    if tool_name == "load_skill":
        skill_name = str(args.get("skill_name") or "unknown").strip()
        return {
            "id": f"tool-{call_id}",
            "kind": "skill",
            "category": "skill",
            "state": "running",
            "agent": agent,
            "parent_id": parent_id,
            "message": f"Load skill: {skill_name}",
            "detail": None,
            "metadata": {"skill_name": skill_name},
        }

    if tool_name == "read_skill_resource":
        skill_name = str(args.get("skill_name") or "unknown").strip()
        resource_name = str(args.get("resource_name") or "unknown").strip()
        return {
            "id": f"tool-{call_id}",
            "kind": "skill",
            "category": "skill-resource",
            "state": "running",
            "agent": agent,
            "parent_id": parent_id,
            "message": f"Read skill resource: {resource_name}",
            "detail": skill_name,
            "metadata": {
                "skill_name": skill_name,
                "resource_name": resource_name,
            },
        }

    detail = args.get(_DETAIL_KEYS.get(tool_name, ""))
    if tool_name == "search_multiple_queries":
        detail = "\n".join(str(query) for query in (args.get("queries") or [])[:5])

    return {
        "id": f"tool-{call_id}",
        "kind": "tool",
        "category": "search" if tool_name in _SEARCH_TOOLS else "tool",
        "state": "running",
        "agent": agent,
        "parent_id": parent_id,
        "message": _TOOL_LABELS.get(tool_name, tool_name.replace("_", " ").title()),
        "detail": str(detail).strip()[:4000] if detail else None,
        "metadata": {"tool_name": tool_name},
    }


def agent_activity(
    activity_id: str,
    agent: str,
    task: str,
    *,
    state: str = "running",
    duration_ms: Optional[int] = None,
    summary: Optional[str] = None,
    metrics: Optional[dict[str, Any]] = None,
    parent_id: Optional[str] = None,
) -> dict[str, Any]:
    """Build an agent delegation lifecycle event."""
    return {
        "id": activity_id,
        "kind": "agent",
        "category": "agent",
        "state": state,
        "agent": agent,
        "parent_id": parent_id,
        "message": agent,
        "detail": task,
        "summary": summary,
        "duration_ms": duration_ms,
        "metrics": metrics or {},
    }


def pipeline_activity(
    activity_id: str,
    task: str,
    *,
    state: str = "running",
    duration_ms: Optional[int] = None,
    summary: Optional[str] = None,
) -> dict[str, Any]:
    """Build the parent activity for the deterministic data-analysis pipeline."""
    return {
        "id": activity_id,
        "kind": "agent",
        "category": "pipeline",
        "state": state,
        "agent": "MasterAgent",
        "parent_id": None,
        "message": "Data analysis pipeline",
        "detail": task,
        "summary": summary,
        "duration_ms": duration_ms,
        "metrics": {},
    }


def stage_activity(
    activity_id: str,
    parent_id: str,
    agent: str,
    message: str,
    *,
    state: str = "running",
    detail: Optional[str] = None,
    category: str = "stage",
    metrics: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build a child stage event reported from an executing agent."""
    return {
        "id": activity_id,
        "kind": "stage",
        "category": category,
        "state": state,
        "agent": agent,
        "parent_id": parent_id,
        "message": message,
        "detail": detail,
        "metrics": metrics or {},
    }


def narration_activity(
    activity_id: str,
    message: str,
    *,
    agent: str,
    parent_id: Optional[str] = None,
) -> dict[str, Any]:
    """Build a model-authored, user-visible working narration event."""
    return {
        "id": activity_id,
        "kind": "narration",
        "category": "narration",
        "state": "completed",
        "agent": agent,
        "parent_id": parent_id,
        "message": message,
    }

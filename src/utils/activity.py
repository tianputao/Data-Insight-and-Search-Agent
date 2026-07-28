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
    "search_entities": "Search ontology entities",
    "describe_entity": "Describe ontology entity",
    "expand_neighbors": "Expand semantic neighbors",
    "find_paths": "Find semantic paths",
    "find_related_by_type": "Find related ontology entities",
    "get_schema_mapping": "Inspect schema mappings",
    "get_join_paths": "Find semantic join paths",
    "get_lineage": "Inspect ontology lineage",
    "get_semantic_candidates": "Find semantic properties",
    "get_business_context": "Build ontology business context",
    "recover_metadata_context": "Recover missing schema context",
    "recover_ontology_context": "Recover missing ontology context",
}

_DETAIL_KEYS = {
    "decompose_query": "original_query",
    "execute_sql": "sql",
    "list_schemas": "catalog",
    "list_tables": "schema",
    "get_table_details": "table_name",
    "search_tables": "keyword",
    "get_relevant_tables": "question",
    "search_entities": "query",
    "describe_entity": "entity",
    "expand_neighbors": "entity",
    "find_paths": "source",
    "find_related_by_type": "entity",
    "get_schema_mapping": "entity",
    "get_join_paths": "source",
    "get_lineage": "entity",
    "get_semantic_candidates": "question",
    "get_business_context": "question",
    "recover_metadata_context": "reason",
    "recover_ontology_context": "reason",
}

_SEARCH_TOOLS = {"search_tables"}
_ONTOLOGY_TOOLS = {
    "search_entities",
    "describe_entity",
    "expand_neighbors",
    "find_paths",
    "find_related_by_type",
    "get_schema_mapping",
    "get_join_paths",
    "get_lineage",
    "get_semantic_candidates",
    "get_business_context",
}


def _bounded_activity_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 5:
        return str(value)[:500]
    if isinstance(value, dict):
        return {
            str(key): _bounded_activity_value(item, depth=depth + 1)
            for key, item in list(value.items())[:30]
        }
    if isinstance(value, (list, tuple)):
        return [
            _bounded_activity_value(item, depth=depth + 1)
            for item in list(value)[:12]
        ]
    if isinstance(value, str):
        return value[:1000]
    return value


def ontology_tool_result_fields(
    tool_name: str,
    result: Any,
    *,
    max_detail_chars: int = 12000,
) -> dict[str, Any]:
    """Build a safe, bounded UI projection for an ontology tool result."""
    if tool_name not in _ONTOLOGY_TOOLS:
        return {}
    payload = result
    if isinstance(result, str):
        try:
            import json

            payload = json.loads(result)
        except (TypeError, ValueError):
            return {
                "summary": "Ontology result was not structured JSON",
                "detail": result[:max_detail_chars],
                "metrics": {},
            }
    if not isinstance(payload, dict):
        return {}

    data = payload.get("data")
    matches = payload.get("matches") if isinstance(payload.get("matches"), list) else []
    warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
    unresolved = (
        payload.get("unresolved")
        if isinstance(payload.get("unresolved"), list)
        else []
    )
    root = None
    path_count = 0
    semantic_property_count = 0
    semantic_relationship_count = 0
    if isinstance(data, dict):
        root = data.get("root_entity") or data.get("root") or data.get("entity")
        paths = data.get("join_paths") or data.get("paths") or []
        path_count = len(paths) if isinstance(paths, list) else 0
        semantic_properties = data.get("semantic_properties") or []
        semantic_relationships = data.get("semantic_relationships") or []
        semantic_property_count = (
            len(semantic_properties) if isinstance(semantic_properties, list) else 0
        )
        semantic_relationship_count = (
            len(semantic_relationships)
            if isinstance(semantic_relationships, list)
            else 0
        )
    if isinstance(root, dict):
        root = root.get("name") or root.get("iri")

    projection = {
        "status": payload.get("status"),
        "data": _bounded_activity_value(data),
        "matches": _bounded_activity_value(matches[:8]),
        "evidence": _bounded_activity_value((payload.get("evidence") or [])[:8]),
        "confidence": payload.get("confidence"),
        "warnings": _bounded_activity_value(warnings),
        "unresolved": _bounded_activity_value(unresolved),
    }
    import json

    detail = json.dumps(projection, ensure_ascii=False, indent=2)
    if len(detail) > max_detail_chars:
        detail = detail[:max_detail_chars] + "\n... UI detail truncated; full evidence retained ..."
    status = str(payload.get("status") or "unknown")
    summary_parts = [f"status={status}"]
    if root:
        summary_parts.append(f"root={root}")
    if payload.get("confidence") is not None:
        summary_parts.append(f"confidence={payload.get('confidence')}")
    return {
        "summary": ", ".join(summary_parts),
        "detail": detail,
        "metrics": {
            "match_count": len(matches),
            "path_count": path_count,
            "semantic_property_count": semantic_property_count,
            "semantic_relationship_count": semantic_relationship_count,
            "warning_count": len(warnings),
            "unresolved_count": len(unresolved),
        },
    }


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
    metrics: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build the parent activity for the native-agent data-analysis pipeline."""
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
        "metrics": metrics or {},
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

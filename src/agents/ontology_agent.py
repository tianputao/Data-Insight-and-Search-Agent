"""Ontology Agent backed by the repository's read-only Owlready2 service."""

from __future__ import annotations

from contextvars import ContextVar
import json
import re
from typing import Annotated, Any, List, Optional

from pydantic import Field

from ..config import AzureOpenAIConfig, OntologyConfig
from ..ontology import OntologyService
from ..prompts import ONTOLOGY_AGENT_PROMPT
from ..skills_provider import create_skills_provider
from ..utils import get_logger
from .maf_runtime import (
    create_agent as create_maf_agent,
    create_session,
    run_agent,
    stream_agent,
)

logger = get_logger(__name__)


class OntologyAgent:
    """Query business semantics and multi-hop relationships from loaded OWL files."""

    def __init__(
        self,
        ontology_service: OntologyService,
        agent_id: str = "ontology_agent",
    ) -> None:
        if not ontology_service.loaded:
            raise RuntimeError("OntologyService must be loaded before OntologyAgent")
        self.ontology_service = ontology_service
        self.agent_id = agent_id
        self._tool_result_sink: ContextVar[Optional[list[dict[str, Any]]]] = ContextVar(
            f"{agent_id}_tool_result_sink",
            default=None,
        )
        self.agent = self._create_agent(self._create_tools())
        logger.info("OntologyAgent '%s' initialised successfully.", agent_id)

    @staticmethod
    def _as_json(payload: dict) -> str:
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _tool_json(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        payload: dict[str, Any],
    ) -> str:
        sink = self._tool_result_sink.get()
        if sink is not None:
            sink.append(
                {
                    "tool": tool_name,
                    "arguments": arguments,
                    "result": payload,
                }
            )
        return self._as_json(self._model_tool_view(tool_name, payload))

    @staticmethod
    def _model_tool_view(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Limit duplicate model input; the sink retains the complete payload."""
        if tool_name != "get_business_context":
            return payload
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        return {
            "status": payload.get("status"),
            "data": {
                "root_entity": data.get("root_entity"),
                "root_entity_detail": data.get("root_entity_detail"),
                "filters": data.get("filters", []),
                "semantic_properties": data.get("semantic_properties", []),
                "semantic_relationships": data.get(
                    "semantic_relationships", []
                ),
                "join_paths": [
                    {
                        "semantic_path": path.get("semantic_path", []),
                        "requires_metadata_resolution": path.get(
                            "requires_metadata_resolution", True
                        ),
                    }
                    for path in data.get("join_paths", [])
                    if isinstance(path, dict)
                ],
                "schema_mapping": data.get("schema_mapping"),
                "lineage": data.get("lineage"),
                "entity_candidates": data.get("entity_candidates", []),
            },
            "confidence": payload.get("confidence"),
            "warnings": payload.get("warnings", []),
            "unresolved": payload.get("unresolved", []),
            "note": "Complete evidence is retained by the orchestrator for DataInsightAgent.",
        }

    def _create_tools(self) -> List:
        """Return typed, read-only Owlready2 query tools."""

        def search_entities(
            query: Annotated[
                str,
                Field(description="Entity name, IRI, label, alias, or business phrase"),
            ],
            entity_types: Annotated[
                Optional[List[str]],
                Field(
                    description=(
                        "Optional types: class, individual, property, object_property, "
                        "data_property, annotation_property"
                    )
                ),
            ] = None,
            limit: Annotated[int, Field(description="Maximum ranked matches")] = 10,
        ) -> str:
            """Resolve ontology entities with exact, multilingual, token, and fuzzy search."""
            payload = self.ontology_service.search_entities(
                query,
                entity_types=entity_types or None,
                limit=limit,
            )
            return self._tool_json(
                "search_entities",
                {"query": query, "entity_types": entity_types, "limit": limit},
                payload,
            )

        def describe_entity(
            entity: Annotated[str, Field(description="Entity name or full IRI")],
            entity_types: Annotated[
                Optional[List[str]],
                Field(description="Optional entity type constraints"),
            ] = None,
        ) -> str:
            """Describe labels, annotations, definitions, hierarchy, disjointness, restrictions, properties, and assertions."""
            payload = self.ontology_service.describe_entity(
                entity,
                entity_types=entity_types or None,
            )
            return self._tool_json(
                "describe_entity",
                {"entity": entity, "entity_types": entity_types},
                payload,
            )

        def expand_neighbors(
            entity: Annotated[str, Field(description="Root entity name or IRI")],
            depth: Annotated[int, Field(description="Traversal depth, bounded by configuration")] = 2,
            direction: Annotated[
                str,
                Field(description="Relation direction: both, outgoing, or incoming"),
            ] = "both",
            relation_types: Annotated[
                Optional[List[str]],
                Field(description="Optional relation names to retain"),
            ] = None,
        ) -> str:
            """Expand direct, inverse, hierarchical, restriction, and inferred neighbors."""
            payload = self.ontology_service.expand_neighbors(
                entity,
                depth=depth,
                direction=direction,
                relation_types=relation_types or None,
            )
            return self._tool_json(
                "expand_neighbors",
                {
                    "entity": entity,
                    "depth": depth,
                    "direction": direction,
                    "relation_types": relation_types,
                },
                payload,
            )

        def find_paths(
            source: Annotated[str, Field(description="Path start entity")],
            target: Annotated[str, Field(description="Path target entity")],
            max_depth: Annotated[int, Field(description="Maximum relationship hops")] = 5,
            max_paths: Annotated[int, Field(description="Maximum paths to return")] = 10,
            relation_types: Annotated[
                Optional[List[str]],
                Field(description="Optional allowed relation names"),
            ] = None,
        ) -> str:
            """Find bounded, cycle-free semantic paths between entities."""
            payload = self.ontology_service.find_paths(
                source,
                target,
                max_depth=max_depth,
                max_paths=max_paths,
                relation_types=relation_types or None,
            )
            return self._tool_json(
                "find_paths",
                {
                    "source": source,
                    "target": target,
                    "max_depth": max_depth,
                    "max_paths": max_paths,
                    "relation_types": relation_types,
                },
                payload,
            )

        def find_related_by_type(
            entity: Annotated[str, Field(description="Root entity name or IRI")],
            target_type: Annotated[str, Field(description="Target OWL class name or IRI")],
            max_depth: Annotated[int, Field(description="Maximum relationship hops")] = 5,
            limit: Annotated[int, Field(description="Maximum related entities")] = 25,
        ) -> str:
            """Find reachable classes or individuals matching an OWL class."""
            payload = self.ontology_service.find_related_by_type(
                entity,
                target_type,
                max_depth=max_depth,
                limit=limit,
            )
            return self._tool_json(
                "find_related_by_type",
                {
                    "entity": entity,
                    "target_type": target_type,
                    "max_depth": max_depth,
                    "limit": limit,
                },
                payload,
            )

        def get_schema_mapping(
            entity: Annotated[str, Field(description="Business entity or property")],
        ) -> str:
            """Get explicit physical mappings or provenance-marked naming candidates."""
            payload = self.ontology_service.get_schema_mapping(entity)
            return self._tool_json(
                "get_schema_mapping",
                {"entity": entity},
                payload,
            )

        def get_join_paths(
            source: Annotated[str, Field(description="First business entity")],
            target: Annotated[str, Field(description="Second business entity")],
            max_depth: Annotated[int, Field(description="Maximum semantic hops")] = 5,
            max_paths: Annotated[int, Field(description="Maximum join paths")] = 10,
        ) -> str:
            """Get semantic join paths without inventing physical Databricks joins."""
            payload = self.ontology_service.get_join_paths(
                source,
                target,
                max_depth=max_depth,
                max_paths=max_paths,
            )
            return self._tool_json(
                "get_join_paths",
                {
                    "source": source,
                    "target": target,
                    "max_depth": max_depth,
                    "max_paths": max_paths,
                },
                payload,
            )

        def get_lineage(
            entity: Annotated[str, Field(description="Entity whose lineage is requested")],
            direction: Annotated[
                str,
                Field(description="Lineage direction: both, outgoing, or incoming"),
            ] = "both",
            depth: Annotated[int, Field(description="Maximum lineage depth")] = 2,
        ) -> str:
            """Separate explicit lineage from ordinary semantic dependencies."""
            payload = self.ontology_service.get_lineage(
                entity,
                direction=direction,
                depth=depth,
            )
            return self._tool_json(
                "get_lineage",
                {"entity": entity, "direction": direction, "depth": depth},
                payload,
            )

        def get_semantic_candidates(
            question: Annotated[str, Field(description="Complete analytical question")],
            root_entity: Annotated[
                str,
                Field(description="Optional known root entity name or IRI"),
            ] = "",
            max_depth: Annotated[int, Field(description="Semantic discovery depth")] = 2,
        ) -> str:
            """Return relevant OWL properties and relationships without assigning SQL roles."""
            payload = self.ontology_service.get_semantic_candidates(
                question,
                root_entity=root_entity,
                max_depth=max_depth,
            )
            return self._tool_json(
                "get_semantic_candidates",
                {
                    "question": question,
                    "root_entity": root_entity,
                    "max_depth": max_depth,
                },
                payload,
            )

        def get_business_context(
            question: Annotated[str, Field(description="Complete original user question")],
            root_entity: Annotated[
                str,
                Field(description="Optional known root entity name or IRI"),
            ] = "",
            max_depth: Annotated[int, Field(description="Maximum semantic discovery depth")] = 3,
        ) -> str:
            """Build composite business context for MetadataAgent and DataInsightAgent."""
            payload = self.ontology_service.get_business_context(
                question,
                root_entity=root_entity,
                max_depth=max_depth,
            )
            return self._tool_json(
                "get_business_context",
                {
                    "question": question,
                    "root_entity": root_entity,
                    "max_depth": max_depth,
                },
                payload,
            )

        def list_defined_classes() -> str:
            """List derived/defined OWL classes (equivalentClass business rules) for concept discovery."""
            payload = self.ontology_service.list_defined_classes()
            return self._tool_json("list_defined_classes", {}, payload)

        return [
            search_entities,
            describe_entity,
            expand_neighbors,
            find_paths,
            find_related_by_type,
            get_schema_mapping,
            get_join_paths,
            get_lineage,
            get_semantic_candidates,
            get_business_context,
            list_defined_classes,
        ]

    def _create_agent(self, tools: List):
        health = self.ontology_service.health()
        runtime_context = (
            "\n\n## Ontology Runtime\n"
            f"- Loaded files: {health['file_count']}\n"
            f"- Indexed entities: {health['entity_count']}\n"
            f"- Reasoner: {health['reasoner']} ({health['reasoning_status']})\n"
            "- Queries are read-only and physical mappings require MetadataAgent verification.\n"
        )
        skills_provider = create_skills_provider("OntologyAgent")
        return create_maf_agent(
            name="OntologyAgent",
            instructions=ONTOLOGY_AGENT_PROMPT + runtime_context,
            tools=tools,
            temperature=0.1,
            context_providers=[skills_provider] if skills_provider else None,
            model=AzureOpenAIConfig.SMALL_GPT_DEPLOYMENT,
            max_iterations=OntologyConfig.AGENT_MAX_MODEL_ROUNDTRIPS,
            max_function_calls=OntologyConfig.AGENT_MAX_FUNCTION_CALLS,
        )

    def get_new_thread(self):
        """Create a new MAF conversation session."""
        return create_session(self.agent)

    def get_context(self, question: str, *, schema_context: str = "") -> str:
        """Build complete service context without discarding ontology query fields."""
        payload = self.ontology_service.get_business_context(question)
        if schema_context:
            try:
                schema = json.loads(schema_context)
                payload["verified_schema_reference"] = {
                    "source": schema.get("source"),
                    "catalog": schema.get("catalog"),
                    "schemas": schema.get("schemas", []),
                    "relevant_tables": schema.get("relevant_tables", []),
                }
            except (TypeError, json.JSONDecodeError) as exc:
                payload.setdefault("warnings", []).append(
                    f"Verified schema context could not be attached: {exc}"
                )
        return self._as_json(payload)

    @staticmethod
    def build_collected_context(
        agent_summary: str,
        tool_results: list[dict[str, Any]],
    ) -> str:
        """Preserve every tool payload and add deterministic, deduplicated guidance."""
        summary_text = (agent_summary or "").strip()
        if summary_text.startswith("```"):
            summary_text = re.sub(
                r"^```(?:json)?\s*|\s*```$",
                "",
                summary_text,
                flags=re.IGNORECASE,
            ).strip()
        try:
            route_payload = json.loads(summary_text) if summary_text else {}
        except (TypeError, json.JSONDecodeError):
            route_payload = {}
        if route_payload.get("route") == "governed_skill":
            return json.dumps(
                {
                    "status": "ok",
                    "route": "governed_skill",
                    "governed_skill": {
                        "skill_name": route_payload.get("skill_name"),
                        "resource_name": route_payload.get("resource_name"),
                        "match_reason": route_payload.get("match_reason", {}),
                    },
                    "primary_business_context": None,
                    "additional_tool_results": tool_results,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )

        primary_item = next(
            (
                item
                for item in tool_results
                if item.get("tool") == "get_business_context"
            ),
            None,
        )
        primary = primary_item.get("result") if primary_item else None
        additional_tool_results = [
            item for item in tool_results if item is not primary_item
        ]
        primary_data = (
            primary.get("data")
            if isinstance(primary, dict) and isinstance(primary.get("data"), dict)
            else primary if isinstance(primary, dict) else {}
        )
        compact_properties = [
            {
                "name": item.get("name"),
                "entity": item.get("entity"),
                "type": item.get("type"),
                "relevance": item.get("relevance"),
                "domain": item.get("domain", []),
                "range": item.get("range", []),
            }
            for item in primary_data.get("semantic_properties", [])
            if isinstance(item, dict)
        ]
        semantic_summary = {
            "root_entity": primary_data.get("root_entity"),
            "filters": primary_data.get("filters", []),
            "semantic_properties": compact_properties,
            "semantic_relationships": primary_data.get(
                "semantic_relationships", []
            ),
            "semantic_paths": [
                path.get("semantic_path", [])
                for path in primary_data.get("join_paths", [])
                if isinstance(path, dict)
            ],
            "requires_metadata_resolution": primary_data.get(
                "requires_metadata_resolution", True
            ),
            "confidence": primary.get("confidence") if isinstance(primary, dict) else None,
            "warnings": primary.get("warnings", []) if isinstance(primary, dict) else [],
            "unresolved": primary.get("unresolved", []) if isinstance(primary, dict) else [],
        }
        return json.dumps(
            {
                "status": "ok" if tool_results else "no_tool_results",
                "primary_tool_call": (
                    {
                        "tool": primary_item.get("tool"),
                        "arguments": primary_item.get("arguments", {}),
                    }
                    if primary_item
                    else None
                ),
                "primary_business_context": primary,
                "semantic_summary": semantic_summary,
                "additional_tool_results": additional_tool_results,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _with_schema_context(question: str, schema_context: str) -> str:
        if not schema_context:
            return question
        return (
            f"<verified_schema_context>\n{schema_context}\n</verified_schema_context>"
            f"\n\n{question}"
        )

    async def query(
        self,
        question: str,
        thread=None,
        schema_context: str = "",
        context_sink: Optional[list[dict[str, Any]]] = None,
    ) -> str:
        """Return structured ontology context for an analytical question."""
        sink = context_sink if context_sink is not None else []
        token = self._tool_result_sink.set(sink)
        try:
            result = await run_agent(
                self.agent,
                self._with_schema_context(question, schema_context),
                session=thread,
            )
            return self.build_collected_context(result.text, sink)
        finally:
            self._tool_result_sink.reset(token)

    async def query_stream(
        self,
        question: str,
        thread=None,
        schema_context: str = "",
        context_sink: Optional[list[dict[str, Any]]] = None,
    ):
        """Stream OntologyAgent model and tool updates."""
        sink = context_sink if context_sink is not None else []
        token = self._tool_result_sink.set(sink)
        try:
            async for update in stream_agent(
                self.agent,
                self._with_schema_context(question, schema_context),
                session=thread,
            ):
                yield update
        finally:
            self._tool_result_sink.reset(token)

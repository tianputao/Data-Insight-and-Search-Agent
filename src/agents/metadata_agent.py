"""
Metadata Agent — reads schema and metadata from Azure Databricks Unity Catalog (UC).

Architecture
------------
* Same MAF pattern as SearchAgent and DataInsightAgent.
* Uses the Databricks SDK (`databricks-sdk`) for Unity Catalog REST API access.
* Falls back to `databricks-sql-connector` JDBC queries when the SDK is unavailable.
* MAF SkillsProvider advertises and loads agent-scoped skills on demand.

Tools provided to the LLM
--------------------------
list_schemas       — list schemas in the default catalog (or a specified one)
list_tables        — list tables in a catalog.schema
get_table_details  — full column definitions, types, descriptions, UC tags
search_tables      — fuzzy search by table keyword across the default schema
load_skill         — load a named skill's full instruction body
"""

from __future__ import annotations

from contextvars import ContextVar
import json
from typing import Annotated, Any, Dict, List, Optional

from pydantic import Field

from ..config import AgentReasoningConfig, AzureOpenAIConfig, DatabricksConfig
from ..metadata_catalog import MetadataCatalogService
from ..prompts import METADATA_AGENT_PROMPT
from ..skills_provider import create_skills_provider
from ..utils import get_logger
from .maf_runtime import (
    create_agent as create_maf_agent,
    create_session,
    run_agent,
    stream_agent,
)

logger = get_logger(__name__)


def _jdbc_query_metadata(sql: str) -> List[Dict[str, Any]]:
    """Execute a metadata SQL query via JDBC and return rows as list-of-dicts."""
    if not DatabricksConfig.is_configured():
        raise RuntimeError("Databricks connection not configured.")
    try:
        from databricks import sql as dbsql
    except ImportError as exc:
        raise RuntimeError("databricks-sql-connector not installed.") from exc

    connection = dbsql.connect(
        server_hostname=DatabricksConfig.HOST.replace("https://", ""),
        http_path=DatabricksConfig.HTTP_PATH,
        access_token=DatabricksConfig.TOKEN,
        _socket_timeout=DatabricksConfig.QUERY_TIMEOUT,
    )
    try:
        cursor = connection.cursor()
        cursor.execute(sql)
        columns = [d[0] for d in (cursor.description or [])]
        rows = cursor.fetchall()
        return [dict(zip(columns, row)) for row in rows]
    finally:
        connection.close()


# ─── MetadataAgent ─────────────────────────────────────────────────────────────

class MetadataAgent:
    """
    Metadata Agent using Microsoft Agent Framework 1.11.
    Retrieves and enriches Unity Catalog metadata for the DataInsightAgent.
    """

    def __init__(
        self,
        agent_id: str = "metadata_agent",
        catalog_service: Optional[MetadataCatalogService] = None,
    ) -> None:
        self.agent_id = agent_id
        self.catalog_service = catalog_service or MetadataCatalogService()
        self._tool_result_sink: ContextVar[Optional[list[dict[str, Any]]]] = ContextVar(
            f"{agent_id}_tool_result_sink",
            default=None,
        )
        tools = self._create_tools()
        self.agent = self._create_agent(
            tools,
            name="MetadataAgent",
            enable_skills=True,
        )
        self.verifier_agent = self._create_agent(
            tools,
            name="MetadataVerifierAgent",
            enable_skills=False,
        )
        logger.info(f"MetadataAgent '{agent_id}' initialised successfully.")

    def _tool_json(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        payload: dict[str, Any],
    ) -> str:
        """Return tool JSON and preserve the complete payload for downstream use."""
        sink = self._tool_result_sink.get()
        if sink is not None:
            sink.append(
                {
                    "tool": tool_name,
                    "arguments": arguments,
                    "result": payload,
                }
            )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    # ─────────────────────────────────────────────────────────────────────────
    # Tool definitions
    # ─────────────────────────────────────────────────────────────────────────

    def _create_tools(self) -> List:

        configured_catalog = DatabricksConfig.CATALOG
        configured_schemas = tuple(DatabricksConfig.SCHEMAS)
        schema_lookup = {
            configured_schema.casefold(): configured_schema
            for configured_schema in configured_schemas
        }

        def validate_catalog(catalog: str) -> tuple[Optional[str], Optional[dict[str, Any]]]:
            requested_catalog = (catalog or configured_catalog).strip()
            if requested_catalog.casefold() != configured_catalog.casefold():
                return None, {
                    "status": "out_of_scope",
                    "requested_catalog": requested_catalog,
                    "configured_catalog": configured_catalog,
                    "configured_schemas": list(configured_schemas),
                    "error": "Catalog is not exposed by the runtime Databricks configuration.",
                }
            return configured_catalog, None

        def validate_schema(schema: str) -> tuple[Optional[str], Optional[dict[str, Any]]]:
            requested_schema = (schema or DatabricksConfig.SCHEMA).strip()
            configured_schema = schema_lookup.get(requested_schema.casefold())
            if configured_schema is None:
                return None, {
                    "status": "out_of_scope",
                    "requested_schema": requested_schema,
                    "configured_catalog": configured_catalog,
                    "configured_schemas": list(configured_schemas),
                    "error": "Schema is not exposed by DATABRICKS_SCHEMAS.",
                }
            return configured_schema, None

        def list_schemas(
            catalog: Annotated[
                str,
                Field(description="Catalog name (default: from DATABRICKS_CATALOG env var)"),
            ] = "",
        ) -> str:
            """List configured schemas that exist in the configured catalog."""
            catalog, scope_error = validate_catalog(catalog)
            if scope_error:
                return self._tool_json(
                    "list_schemas",
                    {"catalog": scope_error["requested_catalog"]},
                    scope_error,
                )
            assert catalog is not None
            logger.info(f"[Tool:list_schemas] catalog='{catalog}'")

            try:
                schema_names, cache_hit = self.catalog_service.list_schemas(
                    catalog=catalog
                )
                available_lookup = {
                    str(schema_name).casefold(): str(schema_name)
                    for schema_name in schema_names
                }
                visible_schemas = [
                    available_lookup[schema_name.casefold()]
                    for schema_name in configured_schemas
                    if schema_name.casefold() in available_lookup
                ]
                result = {
                    "status": "ok",
                    "catalog": catalog,
                    "schemas": visible_schemas,
                    "configured_schemas": list(configured_schemas),
                    "count": len(visible_schemas),
                    "source": "unity_catalog_cache",
                    "cache_hit": cache_hit,
                }
                return self._tool_json(
                    "list_schemas",
                    {"catalog": catalog},
                    result,
                )
            except Exception as exc:
                logger.warning(
                    "[Tool:list_schemas] SDK failed, falling back to JDBC: %s",
                    exc,
                )
                try:
                    rows = _jdbc_query_metadata(f"SHOW SCHEMAS IN `{catalog}`")
                    visible_rows = [
                        row
                        for row in rows
                        if str(row[0] if isinstance(row, (list, tuple)) else row).casefold()
                        in schema_lookup
                    ]
                    result = {
                        "status": "ok",
                        "catalog": catalog,
                        "schemas": visible_rows,
                        "configured_schemas": list(configured_schemas),
                        "count": len(visible_rows),
                        "source": "jdbc_fallback",
                        "cache_hit": False,
                    }
                except Exception as fallback_exc:
                    result = {
                        "status": "error",
                        "catalog": catalog,
                        "error": str(fallback_exc),
                    }
                return self._tool_json(
                    "list_schemas",
                    {"catalog": catalog},
                    result,
                )

        def list_tables(
            schema: Annotated[
                str,
                Field(
                    description=(
                        "Schema (database) name. "
                        "Leave empty to list tables from ALL configured schemas. "
                        "A supplied schema must be present in DATABRICKS_SCHEMAS."
                    )
                ),
            ] = "",
            catalog: Annotated[
                str,
                Field(description="Catalog name (default from DATABRICKS_CATALOG)"),
            ] = "",
        ) -> str:
            """List all tables in the given catalog.schema(s).
            When *schema* is empty, returns tables from every configured schema.
            """
            catalog, catalog_error = validate_catalog(catalog)
            if catalog_error:
                return self._tool_json(
                    "list_tables",
                    {"catalog": catalog_error["requested_catalog"], "schema": schema},
                    catalog_error,
                )
            if schema:
                configured_schema, schema_error = validate_schema(schema)
                if schema_error:
                    return self._tool_json(
                        "list_tables",
                        {"catalog": catalog, "schema": schema},
                        schema_error,
                    )
                schemas_to_query = [configured_schema]
            else:
                schemas_to_query = list(configured_schemas)
            assert catalog is not None
            logger.info(f"[Tool:list_tables] {catalog}.{schemas_to_query}")

            all_results: Dict[str, Any] = {"catalog": catalog, "schemas": {}}
            total_count = 0

            for sch in schemas_to_query:
                try:
                    tables, cache_hit = self.catalog_service.list_tables(
                        catalog=catalog,
                        schema=sch,
                    )
                    table_info = [
                        {
                            "name": table["name"],
                            "full_name": table["full_name"],
                            "table_type": table["table_type"],
                            "comment": table["comment"],
                        }
                        for table in tables
                    ]
                    all_results["schemas"][sch] = {
                        "tables": table_info,
                        "count": len(table_info),
                        "cache_hit": cache_hit,
                    }
                    total_count += len(table_info)
                except Exception as exc:
                    logger.warning(
                        "[Tool:list_tables] SDK failed for %s, JDBC fallback: %s",
                        sch,
                        exc,
                    )
                    try:
                        rows = _jdbc_query_metadata(
                            f"SHOW TABLES IN `{catalog}`.`{sch}`"
                        )
                        all_results["schemas"][sch] = {
                            "tables": rows,
                            "count": len(rows),
                            "source": "jdbc_fallback",
                            "cache_hit": False,
                        }
                        total_count += len(rows)
                    except Exception as fallback_exc:
                        all_results["schemas"][sch] = {
                            "error": str(fallback_exc)
                        }

            all_results["status"] = "ok" if total_count else "partial"
            all_results["total_count"] = total_count
            return self._tool_json(
                "list_tables",
                {"catalog": catalog, "schema": schema},
                all_results,
            )

        def get_table_details(
            table_name: Annotated[
                str,
                Field(description="Table name (can be bare name, schema.table, or catalog.schema.table)"),
            ],
            catalog: Annotated[str, Field(description="Catalog (default from env)")] = "",
            schema: Annotated[str, Field(description="Schema (default from env)")] = "",
        ) -> str:
            """
            Return full column definitions, data types, nullable flags, comments, and UC tags
            for the specified table.  This is the primary tool for MetadataAgent.
            """
            requested_catalog = catalog
            requested_schema = schema

            # Normalise table name to three-part
            parts = table_name.split(".")
            if len(parts) == 3:
                requested_catalog, requested_schema, table_name = parts
            elif len(parts) == 2:
                requested_schema, table_name = parts
            # else: bare name, use defaults above

            catalog, catalog_error = validate_catalog(requested_catalog)
            if catalog_error:
                return self._tool_json(
                    "get_table_details",
                    {
                        "table_name": table_name,
                        "catalog": requested_catalog,
                        "schema": requested_schema,
                    },
                    catalog_error,
                )
            schema, schema_error = validate_schema(requested_schema)
            if schema_error:
                return self._tool_json(
                    "get_table_details",
                    {
                        "table_name": table_name,
                        "catalog": catalog,
                        "schema": requested_schema,
                    },
                    schema_error,
                )
            assert catalog is not None and schema is not None

            full_name = f"{catalog}.{schema}.{table_name}"
            logger.info(f"[Tool:get_table_details] full_name='{full_name}'")

            try:
                table, cache_hit = self.catalog_service.get_table(
                    table_name,
                    catalog=catalog,
                    schema=schema,
                )
                if table is None:
                    result = {
                        "status": "no_match",
                        "full_name": full_name,
                        "error": "table not found",
                    }
                else:
                    result = {
                        "status": "ok",
                        **table,
                        "column_count": len(table["columns"]),
                        "source": "unity_catalog_cache",
                        "cache_hit": cache_hit,
                    }
            except Exception as exc:
                logger.warning(
                    "[Tool:get_table_details] SDK failed, JDBC fallback: %s",
                    exc,
                )
                try:
                    rows = _jdbc_query_metadata(
                        f"DESCRIBE TABLE EXTENDED `{catalog}`.`{schema}`.`{table_name}`"
                    )
                    result = {
                        "status": "ok",
                        "full_name": full_name,
                        "describe": rows,
                        "source": "jdbc_fallback",
                        "cache_hit": False,
                    }
                except Exception as fallback_exc:
                    result = {
                        "status": "error",
                        "full_name": full_name,
                        "error": str(fallback_exc),
                    }
            return self._tool_json(
                "get_table_details",
                {
                    "table_name": table_name,
                    "catalog": catalog,
                    "schema": schema,
                },
                result,
            )

        def search_tables(
            keyword: Annotated[
                str,
                Field(description="Keyword to match against table names and descriptions"),
            ],
            catalog: Annotated[str, Field(description="Catalog to search in (default from env)")] = "",
            schema: Annotated[
                str,
                Field(
                    description=(
                        "Schema to search in. "
                        "Leave empty to search across ALL configured schemas. "
                        "A supplied schema must be present in DATABRICKS_SCHEMAS."
                    )
                ),
            ] = "",
        ) -> str:
            """
            Fuzzy-search for table names containing *keyword*.
            When *schema* is empty, searches every configured schema.
            Useful when the user mentions a business concept but not the exact table name.
            """
            catalog, catalog_error = validate_catalog(catalog)
            if catalog_error:
                return self._tool_json(
                    "search_tables",
                    {
                        "keyword": keyword,
                        "catalog": catalog_error["requested_catalog"],
                        "schema": schema,
                    },
                    catalog_error,
                )
            if schema:
                configured_schema, schema_error = validate_schema(schema)
                if schema_error:
                    return self._tool_json(
                        "search_tables",
                        {"keyword": keyword, "catalog": catalog, "schema": schema},
                        schema_error,
                    )
                schemas_to_search = [configured_schema]
            else:
                schemas_to_search = list(configured_schemas)
            assert catalog is not None
            logger.info(f"[Tool:search_tables] keyword='{keyword}' in {catalog}.{schemas_to_search}")

            all_matches: list = []

            for sch in schemas_to_search:
                try:
                    tables, cache_hit = self.catalog_service.search_tables(
                        keyword,
                        catalog=catalog,
                        schema=sch,
                    )
                    matches = [
                        {
                            "name": table["name"],
                            "full_name": table["full_name"],
                            "schema": sch,
                            "comment": table["comment"],
                            "table_type": table["table_type"],
                            "cache_hit": cache_hit,
                        }
                        for table in tables
                    ]
                    all_matches.extend(matches)
                except Exception as exc:
                    logger.warning(f"[Tool:search_tables] Cached search failed for {sch}: {exc}")

            return self._tool_json(
                "search_tables",
                {
                    "keyword": keyword,
                    "catalog": catalog,
                    "schema": schema,
                },
                {
                    "status": "ok" if all_matches else "no_match",
                    "keyword": keyword,
                    "schemas_searched": schemas_to_search,
                    "matches": all_matches,
                    "count": len(all_matches),
                },
            )

        return [list_schemas, list_tables, get_table_details, search_tables]

    # ─────────────────────────────────────────────────────────────────────────
    # Agent creation
    # ─────────────────────────────────────────────────────────────────────────

    def _create_agent(
        self,
        tools: List,
        *,
        name: str,
        enable_skills: bool,
    ):
        """Initialise the MAF MetadataAgent."""
        enriched_prompt = METADATA_AGENT_PROMPT
        db_context = (
            f"\n\n## Databricks Context\n"
            f"- Default catalog: `{DatabricksConfig.CATALOG}`\n"
            f"- Default schema: `{DatabricksConfig.SCHEMA}`\n"
            f"- Exposed schemas (authoritative allowlist): "
            f"{', '.join(f'`{schema}`' for schema in DatabricksConfig.SCHEMAS)}\n"
            f"- Never request or describe a catalog/schema outside this allowlist.\n"
            f"- Configured: {DatabricksConfig.is_configured()}\n"
        )
        enriched_prompt += db_context

        skills_provider = (
            create_skills_provider("MetadataAgent") if enable_skills else None
        )
        agent = create_maf_agent(
            name=name,
            instructions=enriched_prompt,
            tools=tools,
            reasoning_effort=AgentReasoningConfig.METADATA,
            context_providers=[skills_provider] if skills_provider else None,
            model=AzureOpenAIConfig.SMALL_GPT_DEPLOYMENT,
            max_iterations=DatabricksConfig.METADATA_AGENT_MAX_MODEL_ROUNDTRIPS,
            max_function_calls=DatabricksConfig.METADATA_AGENT_MAX_FUNCTION_CALLS,
        )
        logger.info(
            "%s created with MAF OpenAIChatCompletionClient (skills=%s).",
            name,
            enable_skills,
        )
        return agent

    # ─────────────────────────────────────────────────────────────────────────
    # Public interface
    # ─────────────────────────────────────────────────────────────────────────

    def get_new_thread(self):
        """Create a new MAF conversation thread."""
        return create_session(self.agent)

    def rewrite_sql_identifiers(self, sql: str) -> tuple[str, list[dict[str, str]]]:
        """Correct identifiers using only details selected earlier in this process."""
        return self.catalog_service.rewrite_sql_identifiers(sql)

    @staticmethod
    def build_collected_context(
        agent_summary: str,
        tool_results: list[dict[str, Any]],
    ) -> str:
        """Preserve relevant UC tool evidence alongside the agent summary."""
        return json.dumps(
            {
                "status": "ok" if tool_results else "no_tool_results",
                "source": "metadata_agent",
                "agent_summary": agent_summary,
                "all_tool_results": tool_results,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def build_ontology_verification_context(ontology_context: str) -> str:
        """Project canonical ontology evidence into a bounded UC verification request."""
        if not ontology_context.strip():
            return ""
        try:
            collected = json.loads(ontology_context)
        except (TypeError, json.JSONDecodeError):
            return json.dumps(
                {
                    "status": "unstructured",
                    "unresolved": ["Ontology context could not be parsed for UC verification"],
                },
                ensure_ascii=False,
            )

        primary = collected.get("primary_business_context")
        if not isinstance(primary, dict):
            return json.dumps(
                {
                    "status": "missing_primary_context",
                    "unresolved": ["Ontology context has no primary business context"],
                },
                ensure_ascii=False,
            )
        data = primary.get("data") if isinstance(primary.get("data"), dict) else primary
        schema_mapping = (
            data.get("schema_mapping")
            if isinstance(data.get("schema_mapping"), dict)
            else {}
        )
        semantic_properties = [
            item
            for item in data.get("semantic_properties", [])
            if isinstance(item, dict)
        ]
        join_paths = [
            item for item in data.get("join_paths", []) if isinstance(item, dict)
        ]

        root_entity = data.get("root_entity")
        root_name = (
            root_entity.get("name")
            if isinstance(root_entity, dict)
            else root_entity
        )
        required_entities: list[str] = []
        entity_reasons: dict[str, set[str]] = {}

        def require_entity(entity: Any, reason: str) -> None:
            name = str(entity or "").strip()
            if not name:
                return
            if name not in entity_reasons:
                required_entities.append(name)
                entity_reasons[name] = set()
            entity_reasons[name].add(reason)

        require_entity(root_name, "root")
        required_relations: list[dict[str, str]] = []
        seen_relations: set[tuple[str, str, str]] = set()
        for path in join_paths:
            semantic_path = path.get("semantic_path", [])
            if not isinstance(semantic_path, list):
                continue
            entities = semantic_path[0::2]
            for index, entity in enumerate(entities):
                reason = (
                    "path_origin"
                    if index == 0
                    else "path_endpoint"
                    if index == len(entities) - 1
                    else "path_intermediate"
                )
                require_entity(entity, reason)
            for index in range(0, len(semantic_path) - 2, 2):
                relation = {
                    "source": str(semantic_path[index]),
                    "relation": str(semantic_path[index + 1]),
                    "target": str(semantic_path[index + 2]),
                }
                relation_key = (
                    relation["source"],
                    relation["relation"],
                    relation["target"],
                )
                if relation_key not in seen_relations:
                    seen_relations.add(relation_key)
                    required_relations.append(relation)

        for item in data.get("hierarchy_relations", []):
            if not isinstance(item, dict):
                continue
            entity_name = str(item.get("entity") or "").strip()
            relation_name = str(item.get("relation") or "").strip()
            if not entity_name or not relation_name:
                continue
            relation_key = (entity_name, relation_name, entity_name)
            if relation_key in seen_relations:
                continue
            seen_relations.add(relation_key)
            require_entity(entity_name, "recursive_hierarchy")
            required_relations.append(
                {
                    "source": entity_name,
                    "relation": relation_name,
                    "target": entity_name,
                    "recursive": True,
                }
            )

        properties_by_entity: dict[str, list[dict[str, Any]]] = {}
        for item in semantic_properties:
            domain = item.get("domain", [])
            declared_domains = []
            if isinstance(domain, list):
                declared_domains = [
                    value.get("name") if isinstance(value, dict) else value
                    for value in domain
                ]
            property_entities = [
                str(value or "").strip()
                for value in declared_domains or [item.get("entity")]
                if str(value or "").strip()
            ]
            try:
                relevance = float(item.get("relevance") or 0.0)
            except (TypeError, ValueError):
                relevance = 0.0
            for entity_name in property_entities:
                properties_by_entity.setdefault(entity_name, []).append(item)
                if item.get("evidence") == "question_match" or relevance > 0:
                    require_entity(entity_name, "question_match")

        if not required_entities:
            for entity_name in properties_by_entity:
                require_entity(entity_name, "ontology_context")

        semantic_property_groups = [
            {
                "entity": entity_name,
                "reasons": sorted(entity_reasons[entity_name]),
                "properties": properties_by_entity.get(entity_name, []),
            }
            for entity_name in required_entities
        ]
        root_entity_detail = data.get("root_entity_detail")
        if isinstance(root_entity_detail, dict):
            root_entity_detail = {
                key: value
                for key, value in root_entity_detail.items()
                if key != "properties"
            }
        projection = {
            "status": primary.get("status", collected.get("status", "unknown")),
            "root_entity": root_entity,
            "root_entity_detail": root_entity_detail,
            "filters": data.get("filters", [])[:10],
            "semantic_property_groups": semantic_property_groups,
            "required_relations": required_relations,
            "semantic_relationships": data.get(
                "semantic_relationships", []
            )[:12],
            "join_paths": join_paths,
            "candidate_tables": schema_mapping.get("candidate_tables", [])[:12],
            "candidate_columns": schema_mapping.get("candidate_columns", [])[:30],
            "entity_candidates": data.get("entity_candidates", [])[:8],
            "requires_metadata_resolution": data.get(
                "requires_metadata_resolution", True
            ),
            "warnings": primary.get("warnings", [])[:12],
            "unresolved": primary.get("unresolved", [])[:12],
        }
        return json.dumps(projection, ensure_ascii=False, separators=(",", ":"))

    def supplement_schema_snapshot(
        self,
        tool_results: list[dict[str, Any]],
        *,
        max_tables: int = 40,
    ) -> int:
        """Add cached UC details for tables the verifier model failed to inspect."""
        existing = {
            str((item.get("result") or {}).get("full_name") or "").casefold()
            for item in tool_results
            if item.get("tool") == "get_table_details"
            and isinstance(item.get("result"), dict)
        }
        table_summaries: list[dict[str, Any]] = []
        for schema in DatabricksConfig.SCHEMAS:
            tables, _ = self.catalog_service.list_tables(
                catalog=DatabricksConfig.CATALOG,
                schema=schema,
            )
            table_summaries.extend(tables)
        if len(table_summaries) > max_tables:
            logger.info(
                "Skipping deterministic metadata snapshot: %s tables exceeds limit %s",
                len(table_summaries),
                max_tables,
            )
            return 0

        added = 0
        for summary in table_summaries:
            full_name = str(summary.get("full_name") or "")
            if not full_name or full_name.casefold() in existing:
                continue
            schema = str(summary.get("schema") or "")
            table_name = str(summary.get("name") or "")
            table, cache_hit = self.catalog_service.get_table(
                table_name,
                catalog=DatabricksConfig.CATALOG,
                schema=schema,
            )
            if table is None:
                continue
            tool_results.append(
                {
                    "tool": "get_table_details",
                    "arguments": {
                        "table_name": table_name,
                        "catalog": DatabricksConfig.CATALOG,
                        "schema": schema,
                    },
                    "result": {
                        "status": "ok",
                        **table,
                        "column_count": len(table.get("columns") or []),
                        "source": "deterministic_schema_snapshot",
                        "cache_hit": cache_hit,
                    },
                }
            )
            existing.add(full_name.casefold())
            added += 1
        if added:
            logger.info("Deterministic metadata snapshot supplemented %s table(s)", added)
        return added

    @classmethod
    def _with_ontology_verification_context(
        cls,
        question: str,
        ontology_context: str,
    ) -> str:
        verification_context = cls.build_ontology_verification_context(
            ontology_context
        )
        if not verification_context:
            return question
        return (
            "<ontology_verification_context>\n"
            f"{verification_context}\n"
            "</ontology_verification_context>\n\n"
            f"{question}"
        )

    @classmethod
    def _prepare_contextual_question(
        cls,
        question: str,
        *,
        ontology_context: str,
        require_metadata_mapping: bool,
    ) -> str:
        contextual_question = cls._with_ontology_verification_context(
            question,
            ontology_context,
        )
        if require_metadata_mapping and not ontology_context:
            return (
                "<metadata_discovery_mode>\n"
                "required_skill=metadata-mapping\n"
                "Load this Skill progressively before the first Unity Catalog tool call.\n"
                "</metadata_discovery_mode>\n\n"
                f"{contextual_question}"
            )
        return contextual_question

    async def query(
        self,
        question: str,
        thread=None,
        ontology_context: str = "",
        require_metadata_mapping: bool = False,
        context_sink: Optional[list[dict[str, Any]]] = None,
    ) -> str:
        """
        Retrieve schema metadata relevant to *question*.
        Returns a YAML/markdown schema context block.
        """
        logger.info(f"MetadataAgent.query: '{question[:80]}'")
        sink = context_sink if context_sink is not None else []
        token = self._tool_result_sink.set(sink)
        try:
            selected_agent = self.verifier_agent if ontology_context else self.agent
            contextual_question = self._prepare_contextual_question(
                question,
                ontology_context=ontology_context,
                require_metadata_mapping=require_metadata_mapping,
            )
            result = await run_agent(
                selected_agent,
                contextual_question,
                session=thread if selected_agent is self.agent else None,
            )
            logger.info(f"MetadataAgent.query completed, len={len(result.text)}")
            logger.debug(f"MetadataAgent.query result preview:\n{result.text[:600]}")
            return self.build_collected_context(result.text, sink)
        finally:
            self._tool_result_sink.reset(token)

    async def query_stream(
        self,
        question: str,
        thread=None,
        ontology_context: str = "",
        require_metadata_mapping: bool = False,
        context_sink: Optional[list[dict[str, Any]]] = None,
    ):
        """Streaming version of :meth:`query`."""
        logger.info(f"MetadataAgent.query_stream: '{question[:80]}'")
        sink = context_sink if context_sink is not None else []
        token = self._tool_result_sink.set(sink)
        try:
            selected_agent = self.verifier_agent if ontology_context else self.agent
            contextual_question = self._prepare_contextual_question(
                question,
                ontology_context=ontology_context,
                require_metadata_mapping=require_metadata_mapping,
            )
            async for update in stream_agent(
                selected_agent,
                contextual_question,
                session=thread if selected_agent is self.agent else None,
            ):
                yield update
        finally:
            self._tool_result_sink.reset(token)

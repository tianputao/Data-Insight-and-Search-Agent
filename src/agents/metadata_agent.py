"""
Metadata Agent — reads schema and metadata from Azure Databricks Unity Catalog (UC).

Architecture
------------
* Same MAF pattern as SearchAgent and DataInsightAgent.
* Uses the Databricks SDK (`databricks-sdk`) for Unity Catalog REST API access.
* Falls back to `databricks-sql-connector` metadata queries when the SDK is unavailable.
* Deterministically recalls and batch-fetches candidates before one model verification turn.
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
import re
import unicodedata
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

_ID_SUFFIX_MIN = 5
# Shortest entity noun that may link a role-prefixed key to a domain-prefixed table.
_ENTITY_NOUN_MIN = 6
# Shortest term allowed to match inside a run-together identifier.
_COMPACT_TERM_MIN = 5
_RECALL_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "from", "this", "that", "has", "have",
        "sales", "data", "table", "column", "value", "name", "id",
    }
)


def _recall_tokens(value: Any) -> set[str]:
    """Split identifiers, labels and comments into comparable lowercase tokens."""
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    text = text.casefold()
    text = re.sub(r"[_\-/.]+", " ", text)
    text = re.sub(r"[^\w\u3400-\u9fff]+", " ", text, flags=re.UNICODE)
    return {token for token in text.split() if token}


def _compact_hits(terms: set[str], compact: str) -> int:
    """Count terms embedded in a run-together identifier, ignoring short noise."""
    if not compact:
        return 0
    return sum(
        1 for term in terms if len(term) >= _COMPACT_TERM_MIN and term in compact
    )


def _sql_connector_query_metadata(sql: str) -> List[Dict[str, Any]]:
    """Execute a metadata query through the Databricks SQL connector."""
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
                    "[Tool:list_schemas] SDK failed, using SQL connector fallback: %s",
                    exc,
                )
                try:
                    rows = _sql_connector_query_metadata(f"SHOW SCHEMAS IN `{catalog}`")
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
                        "source": "sql_connector_fallback",
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
                        "[Tool:list_tables] SDK failed for %s, SQL connector fallback: %s",
                        sch,
                        exc,
                    )
                    try:
                        rows = _sql_connector_query_metadata(
                            f"SHOW TABLES IN `{catalog}`.`{sch}`"
                        )
                        all_results["schemas"][sch] = {
                            "tables": rows,
                            "count": len(rows),
                            "source": "sql_connector_fallback",
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
                    "[Tool:get_table_details] SDK failed, SQL connector fallback: %s",
                    exc,
                )
                try:
                    rows = _sql_connector_query_metadata(
                        f"DESCRIBE TABLE EXTENDED `{catalog}`.`{schema}`.`{table_name}`"
                    )
                    result = {
                        "status": "ok",
                        "full_name": full_name,
                        "describe": rows,
                        "source": "sql_connector_fallback",
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
    def selected_table_names(agent_summary: str) -> list[str]:
        """Read the tables the verifier actually selected out of the recalled candidates."""
        text = (agent_summary or "").strip()
        if not text:
            return []
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return []
        try:
            payload = json.loads(text[start : end + 1])
        except (TypeError, json.JSONDecodeError):
            return []
        if not isinstance(payload, dict):
            return []
        return [
            str(value)
            for value in payload.get("selected_tables", []) or []
            if str(value or "").strip()
        ]

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

    @staticmethod
    def _table_detail_record(
        table: dict[str, Any],
        *,
        cache_hit: bool,
    ) -> dict[str, Any]:
        return {
            "tool": "get_table_details",
            "origin": "deterministic",
            "arguments": {
                "table_name": table.get("name"),
                "catalog": DatabricksConfig.CATALOG,
                "schema": table.get("schema"),
            },
            "result": {
                "status": "ok",
                **table,
                "column_count": len(table.get("columns") or []),
                "source": "deterministic_schema_snapshot",
                "cache_hit": cache_hit,
            },
        }

    @staticmethod
    def _recalled_table_names(tool_results: list[dict[str, Any]]) -> list[str]:
        """Return the candidate set an earlier recall pass committed to, if any."""
        for item in tool_results:
            if item.get("tool") != "list_tables" or item.get("origin") != "deterministic":
                continue
            result = item.get("result")
            if not isinstance(result, dict):
                continue
            selected = result.get("selected_tables")
            if isinstance(selected, list) and selected:
                return [str(name) for name in selected if name]
        return []

    def _index_summaries(self) -> list[dict[str, Any]]:
        """List every allowlisted table once; no column is fetched here."""
        summaries: list[dict[str, Any]] = []
        for schema in DatabricksConfig.SCHEMAS:
            tables, _ = self.catalog_service.list_tables(
                catalog=DatabricksConfig.CATALOG,
                schema=schema,
            )
            summaries.extend(tables)
            if len(summaries) >= DatabricksConfig.METADATA_INDEX_MAX_TABLES:
                break
        return summaries[: DatabricksConfig.METADATA_INDEX_MAX_TABLES]

    def _recall_terms(self, question: str, ontology_context: str) -> tuple[set[str], set[str]]:
        """Split retrieval terms into ontology-backed terms and plain question terms."""
        ontology_terms: set[str] = set()
        if ontology_context:
            try:
                projection = json.loads(
                    self.build_ontology_verification_context(ontology_context) or "{}"
                )
            except (TypeError, json.JSONDecodeError):
                projection = {}
            # A single-entity question carries its whole meaning on the root, so the
            # root name has to seed recall even when no join path was resolved.
            ontology_terms |= _recall_tokens(projection.get("root_entity"))
            root_detail = projection.get("root_entity_detail")
            if isinstance(root_detail, dict):
                ontology_terms |= _recall_tokens(root_detail.get("name"))
                for prop in root_detail.get("properties", []) or []:
                    if isinstance(prop, dict):
                        ontology_terms |= _recall_tokens(prop.get("name"))
            for group in projection.get("semantic_property_groups", []) or []:
                if not isinstance(group, dict):
                    continue
                ontology_terms |= _recall_tokens(group.get("entity"))
                for prop in group.get("properties", []) or []:
                    if isinstance(prop, dict):
                        ontology_terms |= _recall_tokens(prop.get("name"))
            for relation in projection.get("required_relations", []) or []:
                if isinstance(relation, dict):
                    ontology_terms |= _recall_tokens(relation.get("source"))
                    ontology_terms |= _recall_tokens(relation.get("target"))
            for candidate in projection.get("candidate_tables", []) or []:
                if isinstance(candidate, dict):
                    ontology_terms |= _recall_tokens(candidate.get("name"))
            for candidate in projection.get("candidate_columns", []) or []:
                if not isinstance(candidate, dict):
                    continue
                ontology_terms |= _recall_tokens(candidate.get("property"))
                for value in candidate.get("candidates", []) or []:
                    ontology_terms |= _recall_tokens(value)
        question_terms = {
            token for token in _recall_tokens(question) if len(token) > 2
        }
        return ontology_terms - _RECALL_STOPWORDS, question_terms - _RECALL_STOPWORDS

    def select_candidate_tables(
        self,
        question: str,
        ontology_context: str,
    ) -> dict[str, Any]:
        """Recall the tables this question needs instead of loading the whole schema."""
        summaries = self._index_summaries()
        if not summaries:
            return {"selected": [], "total": 0, "basis": "empty_catalog"}

        cached = {
            str(detail.get("full_name") or "").casefold(): detail
            for detail in self.catalog_service.cached_table_details()
        }
        ontology_terms, question_terms = self._recall_terms(question, ontology_context)

        scored: list[tuple[float, str]] = []
        for summary in summaries:
            full_name = str(summary.get("full_name") or "")
            if not full_name:
                continue
            name_tokens = _recall_tokens(summary.get("name")) | _recall_tokens(
                summary.get("comment")
            )
            column_tokens: set[str] = set()
            detail = cached.get(full_name.casefold())
            if detail:
                for column in detail.get("columns") or []:
                    if isinstance(column, dict):
                        column_tokens |= _recall_tokens(column.get("name"))
                        column_tokens |= _recall_tokens(column.get("comment"))
            # UC names are lower-case run-together words while ontology names are camel
            # case, so token equality alone never matches; compare against the compact form.
            name_compact = "".join(sorted(name_tokens))
            column_compact = "".join(sorted(column_tokens))
            score = 0.0
            score += 3.0 * len(ontology_terms & name_tokens)
            score += 2.0 * len(ontology_terms & column_tokens)
            score += 2.0 * len(question_terms & name_tokens)
            score += 1.0 * len(question_terms & column_tokens)
            score += 2.0 * _compact_hits(ontology_terms, name_compact)
            score += 1.0 * _compact_hits(ontology_terms, column_compact)
            score += 1.0 * _compact_hits(question_terms, name_compact)
            if score > 0:
                scored.append((score, full_name))

        if not scored:
            # No lexical anchor: fall back to the whole small schema rather than guess.
            if len(summaries) <= DatabricksConfig.METADATA_SNAPSHOT_MAX_TABLES:
                return {
                    "selected": [str(item.get("full_name")) for item in summaries],
                    "total": len(summaries),
                    "basis": "no_match_full_schema",
                }
            return {"selected": [], "total": len(summaries), "basis": "no_match"}

        scored.sort(key=lambda item: (-item[0], item[1].casefold()))
        selected = [
            full_name
            for _, full_name in scored[: DatabricksConfig.METADATA_CANDIDATE_MAX_TABLES]
        ]
        return {
            "selected": selected,
            "total": len(summaries),
            "basis": "recall",
            "summaries": summaries,
        }

    @staticmethod
    def _entity_matches_table(stem: str, table_token: str) -> bool:
        """Match a foreign-key stem to a table name across domain and role prefixes."""
        if not stem or not table_token:
            return False
        if table_token == stem or table_token.startswith(stem) or table_token.endswith(stem):
            return True
        # `ShipToAddressID` and `salesaddress` share only the entity noun, so compare
        # the common tail rather than requiring one name to contain the other.
        limit = min(len(stem), len(table_token))
        common = 0
        while common < limit and stem[-1 - common] == table_token[-1 - common]:
            common += 1
        return common >= _ENTITY_NOUN_MIN

    @classmethod
    def _join_closure(
        cls,
        details: list[dict[str, Any]],
        summaries: list[dict[str, Any]],
        selected: set[str],
    ) -> list[str]:
        """Add tables a selected table must join through, keyed by identifier suffix."""
        by_name = {
            str(item.get("full_name") or "").casefold(): str(item.get("full_name") or "")
            for item in summaries
        }
        missing: list[str] = []
        for detail in details:
            for column in detail.get("columns") or []:
                if not isinstance(column, dict):
                    continue
                column_name = str(column.get("name") or "")
                if len(column_name) < _ID_SUFFIX_MIN or not column_name.casefold().endswith("id"):
                    continue
                stem = column_name[:-2].casefold()
                for key, full_name in by_name.items():
                    if full_name.casefold() in selected or full_name in missing:
                        continue
                    if cls._entity_matches_table(stem, key.rsplit(".", 1)[-1]):
                        missing.append(full_name)
                        if len(missing) >= DatabricksConfig.METADATA_CANDIDATE_MAX_TABLES:
                            return missing
        return missing

    def prefetch_candidate_schema(
        self,
        tool_results: list[dict[str, Any]],
        *,
        question: str,
        ontology_context: str,
    ) -> dict[str, Any]:
        """Resolve, fetch and record the candidate tables before the model runs."""
        recall = self.select_candidate_tables(question, ontology_context)
        selected = list(recall.get("selected") or [])
        if not selected:
            return recall

        details, cache_hits = self.catalog_service.get_tables_details(selected)
        summaries = recall.get("summaries") or self._index_summaries()
        closure = self._join_closure(
            details,
            summaries,
            {name.casefold() for name in selected},
        )
        if closure:
            extra, extra_hits = self.catalog_service.get_tables_details(closure)
            details.extend(extra)
            cache_hits += extra_hits
            selected.extend(closure)

        tool_results.append(
            {
                "tool": "list_tables",
                "origin": "deterministic",
                "arguments": {
                    "catalog": DatabricksConfig.CATALOG,
                    "schema": ", ".join(DatabricksConfig.SCHEMAS),
                },
                "result": {
                    "status": "ok",
                    "table_count": recall.get("total", len(summaries)),
                    "tables": [str(item.get("full_name") or "") for item in summaries],
                    "selected_tables": [
                        str(table.get("full_name") or "") for table in details
                    ],
                    "selection_basis": recall.get("basis"),
                    "join_closure_tables": closure,
                    "source": "deterministic_schema_snapshot",
                },
            }
        )
        for table in details:
            tool_results.append(self._table_detail_record(table, cache_hit=True))

        recall["selected"] = [str(table.get("full_name") or "") for table in details]
        recall["cache_hits"] = cache_hits
        logger.info(
            "Metadata recall selected %s of %s table(s) (basis=%s, join closure=%s)",
            len(details),
            recall.get("total"),
            recall.get("basis"),
            len(closure),
        )
        return recall

    def supplement_schema_snapshot(
        self,
        tool_results: list[dict[str, Any]],
        *,
        max_tables: int = 0,
    ) -> int:
        """Add cached UC details for tables the verifier model failed to inspect."""
        limit = max_tables or DatabricksConfig.METADATA_SNAPSHOT_MAX_TABLES
        existing = {
            str((item.get("result") or {}).get("full_name") or "").casefold()
            for item in tool_results
            if item.get("tool") == "get_table_details"
            and isinstance(item.get("result"), dict)
        }
        required = self._recalled_table_names(tool_results)
        if required:
            wanted = [name for name in required if name.casefold() not in existing]
            if not wanted:
                return 0
            details, _ = self.catalog_service.get_tables_details(wanted)
            for table in details:
                tool_results.append(self._table_detail_record(table, cache_hit=True))
            if details:
                logger.info(
                    "Deterministic metadata snapshot supplemented %s recalled table(s)",
                    len(details),
                )
            return len(details)

        table_summaries = self._index_summaries()
        if len(table_summaries) > limit:
            logger.info(
                "Skipping deterministic metadata snapshot: %s tables exceeds limit %s",
                len(table_summaries),
                limit,
            )
            return 0

        wanted = [
            str(summary.get("full_name") or "")
            for summary in table_summaries
            if str(summary.get("full_name") or "").casefold() not in existing
        ]
        details, _ = self.catalog_service.get_tables_details(
            [name for name in wanted if name]
        )
        for table in details:
            tool_results.append(self._table_detail_record(table, cache_hit=True))
        if details:
            logger.info(
                "Deterministic metadata snapshot supplemented %s table(s)",
                len(details),
            )
        return len(details)

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
                "Load this Skill progressively before returning any decision, including when\n"
                "<verified_schema_snapshot> removes the need for a Unity Catalog tool call.\n"
                "</metadata_discovery_mode>\n\n"
                f"{contextual_question}"
            )
        return contextual_question

    @staticmethod
    def _render_schema_snapshot(tool_results: list[dict[str, Any]]) -> str:
        """Render already-fetched UC details compactly enough to prefill the model context."""
        lines: list[str] = []
        for item in tool_results:
            if item.get("tool") != "get_table_details":
                continue
            table = item.get("result")
            if not isinstance(table, dict):
                continue
            full_name = str(table.get("full_name") or "")
            if not full_name:
                continue
            comment = " ".join(str(table.get("comment") or "").split())
            lines.append(f"{full_name}" + (f" — {comment}" if comment else ""))
            for column in table.get("columns") or []:
                if not isinstance(column, dict):
                    continue
                column_type = str(column.get("type") or "").split(".")[-1].lower()
                column_comment = " ".join(str(column.get("comment") or "").split())
                nullable = "" if column.get("nullable", True) else " not null"
                lines.append(
                    f"  {column.get('name')} {column_type}{nullable}"
                    + (f" — {column_comment}" if column_comment else "")
                )
        if not lines:
            return ""
        body = "\n".join(lines)
        return (
            "<verified_schema_snapshot source=\"unity_catalog\" complete=\"true\">\n"
            f"{body}\n"
            "</verified_schema_snapshot>\n\n"
        )

    def _with_schema_snapshot(
        self,
        contextual_question: str,
        sink: list[dict[str, Any]],
        *,
        question: str,
        ontology_context: str,
    ) -> str:
        """Prefetch only the tables this question recalls, so discovery rounds are unnecessary."""
        recall = self.prefetch_candidate_schema(
            sink,
            question=question,
            ontology_context=ontology_context,
        )
        snapshot = self._render_schema_snapshot(sink)
        if not snapshot:
            return contextual_question
        header = (
            f"selected {len(recall.get('selected') or [])} of {recall.get('total', 0)} "
            f"table(s) by {recall.get('basis', 'recall')}"
        )
        return f"<!-- {header} -->\n{snapshot}{contextual_question}"

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
        Returns MetadataAgent's verification decisions plus every raw tool payload.
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
            contextual_question = self._with_schema_snapshot(
                contextual_question,
                sink,
                question=question,
                ontology_context=ontology_context,
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
            contextual_question = self._with_schema_snapshot(
                contextual_question,
                sink,
                question=question,
                ontology_context=ontology_context,
            )
            async for update in stream_agent(
                selected_agent,
                contextual_question,
                session=thread if selected_agent is self.agent else None,
            ):
                yield update
        finally:
            self._tool_result_sink.reset(token)

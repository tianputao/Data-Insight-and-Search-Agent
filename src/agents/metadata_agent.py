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

import json
import threading
from typing import Annotated, Any, Dict, List, Optional

from pydantic import Field

from ..config import DatabricksConfig
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


# ─── Databricks SDK helpers ────────────────────────────────────────────────────

def _get_workspace_client():
    """Return a databricks-sdk WorkspaceClient, or raise RuntimeError if not available."""
    if not DatabricksConfig.is_configured():
        raise RuntimeError(
            "Databricks connection is not configured. "
            "Set DATABRICKS_HOST, DATABRICKS_TOKEN in .env."
        )
    try:
        from databricks.sdk import WorkspaceClient
        return WorkspaceClient(
            host=DatabricksConfig.HOST,
            token=DatabricksConfig.TOKEN,
        )
    except ImportError as exc:
        raise RuntimeError(
            "databricks-sdk is not installed. Run: pip install databricks-sdk"
        ) from exc


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

    def __init__(self, agent_id: str = "metadata_agent") -> None:
        self.agent_id = agent_id
        tools = self._create_tools()
        self.agent = self._create_agent(tools)
        logger.info(f"MetadataAgent '{agent_id}' initialised successfully.")

    # ─────────────────────────────────────────────────────────────────────────
    # Tool definitions
    # ─────────────────────────────────────────────────────────────────────────

    def _create_tools(self) -> List:

        def list_schemas(
            catalog: Annotated[
                str,
                Field(description="Catalog name (default: from DATABRICKS_CATALOG env var)"),
            ] = "",
        ) -> str:
            """List all schemas (databases) in the given catalog."""
            catalog = catalog or DatabricksConfig.CATALOG
            logger.info(f"[Tool:list_schemas] catalog='{catalog}'")

            if not DatabricksConfig.is_configured():
                return "Databricks not configured. Set DATABRICKS_HOST/TOKEN/HTTP_PATH in .env."

            try:
                client = _get_workspace_client()
                schemas = list(client.schemas.list(catalog_name=catalog))
                schema_names = [s.name for s in schemas]
                result = {
                    "catalog": catalog,
                    "schemas": schema_names,
                    "count": len(schema_names),
                }
                return json.dumps(result, ensure_ascii=False, indent=2)
            except RuntimeError as exc:
                return str(exc)
            except Exception as exc:
                # Fallback using JDBC
                logger.warning(f"[Tool:list_schemas] SDK failed, falling back to JDBC: {exc}")
                try:
                    rows = _jdbc_query_metadata(f"SHOW SCHEMAS IN `{catalog}`")
                    return json.dumps({"catalog": catalog, "schemas": rows}, ensure_ascii=False, indent=2)
                except Exception as exc2:
                    return f"Failed to list schemas: {exc2}"

        def list_tables(
            schema: Annotated[
                str,
                Field(
                    description=(
                        "Schema (database) name. "
                        "Leave empty to list tables from ALL configured schemas. "
                        "Pass a specific schema name (e.g. 'silver' or 'gold') to list only that schema."
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
            catalog = catalog or DatabricksConfig.CATALOG
            schemas_to_query = [schema] if schema else DatabricksConfig.SCHEMAS
            logger.info(f"[Tool:list_tables] {catalog}.{schemas_to_query}")

            if not DatabricksConfig.is_configured():
                return "Databricks not configured."

            all_results: Dict[str, Any] = {"catalog": catalog, "schemas": {}}
            total_count = 0

            for sch in schemas_to_query:
                try:
                    client = _get_workspace_client()
                    tables = list(client.tables.list(catalog_name=catalog, schema_name=sch))
                    table_info = [
                        {
                            "name": t.name,
                            "full_name": t.full_name,
                            "table_type": str(t.table_type),
                            "comment": t.comment or "",
                        }
                        for t in tables
                    ]
                    all_results["schemas"][sch] = {"tables": table_info, "count": len(table_info)}
                    total_count += len(table_info)
                except RuntimeError as exc:
                    all_results["schemas"][sch] = {"error": str(exc)}
                except Exception as exc:
                    logger.warning(f"[Tool:list_tables] SDK failed for {sch}, JDBC fallback: {exc}")
                    try:
                        rows = _jdbc_query_metadata(f"SHOW TABLES IN `{catalog}`.`{sch}`")
                        all_results["schemas"][sch] = {"tables": rows, "count": len(rows)}
                        total_count += len(rows)
                    except Exception as exc2:
                        all_results["schemas"][sch] = {"error": str(exc2)}

            all_results["total_count"] = total_count
            return json.dumps(all_results, ensure_ascii=False, indent=2)

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
            catalog = catalog or DatabricksConfig.CATALOG
            schema = schema or DatabricksConfig.SCHEMA

            # Normalise table name to three-part
            parts = table_name.split(".")
            if len(parts) == 3:
                catalog, schema, table_name = parts
            elif len(parts) == 2:
                schema, table_name = parts
            # else: bare name, use defaults above

            full_name = f"{catalog}.{schema}.{table_name}"
            logger.info(f"[Tool:get_table_details] full_name='{full_name}'")

            if not DatabricksConfig.is_configured():
                return "Databricks not configured."

            try:
                client = _get_workspace_client()
                table = client.tables.get(full_name=full_name)
                columns = []
                for col in (table.columns or []):
                    col_info: Dict[str, Any] = {
                        "name": col.name,
                        "type": str(col.type_name),
                        "nullable": getattr(col, "nullable", None),
                        "comment": getattr(col, "comment", "") or "",
                    }
                    col_tags = getattr(col, "tags", None)
                    if col_tags:
                        try:
                            col_info["tags"] = {k: v for k, v in col_tags.items()}
                        except Exception:
                            pass
                    columns.append(col_info)

                table_tags = getattr(table, "tags", None)
                result = {
                    "full_name": full_name,
                    "table_type": str(table.table_type),
                    "comment": getattr(table, "comment", "") or "",
                    "owner": getattr(table, "owner", "") or "",
                    "columns": columns,
                    "column_count": len(columns),
                }
                if table_tags:
                    try:
                        result["table_tags"] = {k: v for k, v in table_tags.items()}
                    except Exception:
                        pass

                return json.dumps(result, ensure_ascii=False, indent=2)

            except RuntimeError as exc:
                return str(exc)
            except Exception as exc:
                logger.warning(f"[Tool:get_table_details] SDK failed, JDBC fallback: {exc}")
                try:
                    rows = _jdbc_query_metadata(f"DESCRIBE TABLE EXTENDED `{catalog}`.`{schema}`.`{table_name}`")
                    return json.dumps({"full_name": full_name, "describe": rows}, ensure_ascii=False, indent=2)
                except Exception as exc2:
                    return f"Failed to get table details: {exc2}"

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
                        "Leave empty to search across ALL configured schemas (e.g. silver, gold). "
                        "Pass a specific schema name to narrow the search."
                    )
                ),
            ] = "",
        ) -> str:
            """
            Fuzzy-search for table names containing *keyword*.
            When *schema* is empty, searches every configured schema.
            Useful when the user mentions a business concept but not the exact table name.
            """
            catalog = catalog or DatabricksConfig.CATALOG
            schemas_to_search = [schema] if schema else DatabricksConfig.SCHEMAS
            keyword_lower = keyword.lower()
            logger.info(f"[Tool:search_tables] keyword='{keyword}' in {catalog}.{schemas_to_search}")

            if not DatabricksConfig.is_configured():
                return "Databricks not configured."

            all_matches: list = []

            for sch in schemas_to_search:
                try:
                    client = _get_workspace_client()
                    tables = list(client.tables.list(catalog_name=catalog, schema_name=sch))
                    matches = [
                        {
                            "name": t.name,
                            "full_name": t.full_name,
                            "schema": sch,
                            "comment": t.comment or "",
                            "table_type": str(t.table_type),
                        }
                        for t in tables
                        if keyword_lower in (t.name or "").lower()
                        or keyword_lower in (t.comment or "").lower()
                    ]
                    all_matches.extend(matches)
                except RuntimeError as exc:
                    logger.warning(f"[Tool:search_tables] RuntimeError for schema '{sch}': {exc}")
                except Exception as exc:
                    logger.warning(f"[Tool:search_tables] SDK failed for {sch}, JDBC fallback: {exc}")
                    try:
                        rows = _jdbc_query_metadata(
                            f"SHOW TABLES IN `{catalog}`.`{sch}` LIKE '*{keyword}*'"
                        )
                        for row in rows:
                            row["schema"] = sch
                        all_matches.extend(rows)
                    except Exception as exc2:
                        logger.warning(f"[Tool:search_tables] JDBC fallback also failed for {sch}: {exc2}")

            return json.dumps(
                {"keyword": keyword, "schemas_searched": schemas_to_search, "matches": all_matches, "count": len(all_matches)},
                ensure_ascii=False,
                indent=2,
            )

        return [list_schemas, list_tables, get_table_details, search_tables]

    # ─────────────────────────────────────────────────────────────────────────
    # Agent creation
    # ─────────────────────────────────────────────────────────────────────────

    def _create_agent(self, tools: List):
        """Initialise the MAF MetadataAgent."""
        enriched_prompt = METADATA_AGENT_PROMPT
        db_context = (
            f"\n\n## Databricks Context\n"
            f"- Default catalog: `{DatabricksConfig.CATALOG}`\n"
            f"- Default schema: `{DatabricksConfig.SCHEMA}`\n"
            f"- Configured: {DatabricksConfig.is_configured()}\n"
        )
        enriched_prompt += db_context

        skills_provider = create_skills_provider("MetadataAgent")
        agent = create_maf_agent(
            name="MetadataAgent",
            instructions=enriched_prompt,
            tools=tools,
            temperature=0.0,
            context_providers=[skills_provider] if skills_provider else None,
        )
        logger.info("MetadataAgent created with MAF OpenAIChatCompletionClient.")
        return agent

    # ─────────────────────────────────────────────────────────────────────────
    # Public interface
    # ─────────────────────────────────────────────────────────────────────────

    def get_new_thread(self):
        """Create a new MAF conversation thread."""
        return create_session(self.agent)

    async def query(self, question: str, thread=None) -> str:
        """
        Retrieve schema metadata relevant to *question*.
        Returns a YAML/markdown schema context block.
        """
        logger.info(f"MetadataAgent.query: '{question[:80]}'")
        result = await run_agent(self.agent, question, session=thread)
        logger.info(f"MetadataAgent.query completed, len={len(result.text)}")
        logger.debug(f"MetadataAgent.query result preview:\n{result.text[:600]}")
        return result.text

    async def query_stream(self, question: str, thread=None):
        """Streaming version of :meth:`query`."""
        logger.info(f"MetadataAgent.query_stream: '{question[:80]}'")
        async for update in stream_agent(self.agent, question, session=thread):
            yield update

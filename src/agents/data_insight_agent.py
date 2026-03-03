"""
Data Insight Agent — executes analytical SQL / SparkSQL against Azure Databricks Delta tables.

Architecture
------------
* Built on the same Microsoft Agent Framework (MAF) pattern as SearchAgent.
* Uses AzureOpenAIChatClient + function tools (no Databricks SDK yet, uses
  databricks-sql-connector for JDBC-style queries).
* Receives schema context from MetadataAgent (injected as part of the question).
* Skill context is injected into the system prompt at initialisation via SkillInjector.

Tools provided to the LLM
--------------------------
get_relevant_tables   — asks MetadataAgent for table metadata relevant to the question
execute_sql           — runs a SQL string against the Databricks SQL warehouse and returns rows
load_skill            — loads the full body of a named skill into the conversation context
"""

from __future__ import annotations

import json
import re
import threading
import hashlib
from typing import Annotated, Any, Dict, List, Optional

from pydantic import Field
from agent_framework.azure import AzureOpenAIChatClient
from azure.identity import DefaultAzureCredential

from ..config import AzureOpenAIConfig, DatabricksConfig
from ..prompts import DATA_INSIGHT_AGENT_PROMPT
from ..injector import skill_injector
from ..utils import get_logger

logger = get_logger(__name__)

# ─── Databricks connection singleton (avoids per-query cold-start) ────────────
# Performance note: the biggest latency contributors are:
#   1. Databricks warehouse cold-start (first connect ~3-10 s, warm ~<1 s)
#   2. MetadataAgent round-trips to Unity Catalog SDK (~1-3 s each)
#   3. Multiple LLM hops: MasterAgent → DataInsightAgent → MetadataAgent
# Reusing the JDBC connection eliminates the cold-start penalty for subsequent queries.
_db_connection: Optional[Any] = None
_db_lock = threading.Lock()


def _get_db_connection():
    """Return a reusable Databricks SQL connection, creating one if needed."""
    global _db_connection

    if not DatabricksConfig.is_configured():
        raise RuntimeError(
            "Databricks connection is not configured. "
            "Set DATABRICKS_HOST, DATABRICKS_TOKEN, and DATABRICKS_HTTP_PATH in .env."
        )

    try:
        from databricks import sql as dbsql
    except ImportError as exc:
        raise RuntimeError(
            "databricks-sql-connector is not installed. "
            "Run: pip install databricks-sql-connector"
        ) from exc

    with _db_lock:
        # Test existing connection with a lightweight ping
        if _db_connection is not None:
            try:
                cur = _db_connection.cursor()
                cur.execute("SELECT 1")
                cur.close()
                return _db_connection
            except Exception:
                logger.warning("Stale Databricks connection, reconnecting…")
                try:
                    _db_connection.close()
                except Exception:
                    pass
                _db_connection = None

        logger.info("Opening new Databricks SQL connection…")
        _db_connection = dbsql.connect(
            server_hostname=DatabricksConfig.HOST.replace("https://", ""),
            http_path=DatabricksConfig.HTTP_PATH,
            access_token=DatabricksConfig.TOKEN,
        )
        return _db_connection


def _run_databricks_query(sql: str, max_rows: int = 500) -> Dict[str, Any]:
    """
    Execute *sql* against the configured Databricks SQL warehouse.
    Reuses a persistent connection to avoid per-call cold-start latency.
    """
    connection = _get_db_connection()
    try:
        cursor = connection.cursor()
        cursor.execute(sql)
        raw_rows = cursor.fetchmany(max_rows)
        columns = [desc[0] for desc in (cursor.description or [])]
        rows = [list(row) for row in raw_rows]
        return {"columns": columns, "rows": rows, "row_count": len(rows), "sql": sql}
    except Exception:
        # Connection may have gone bad — force reconnect next call
        global _db_connection
        with _db_lock:
            _db_connection = None
        raise


class DataInsightAgent:
    """
    Data Insight Agent using Microsoft Agent Framework's AzureOpenAIChatClient.
    Generates and executes SQL queries against Azure Databricks Delta tables.
    """

    def __init__(
        self,
        metadata_agent: Optional[Any] = None,  # MetadataAgent or None
        agent_id: str = "data_insight_agent",
    ) -> None:
        """
        Parameters
        ----------
        metadata_agent:
            Optional MetadataAgent instance.  When provided, the agent can call it
            via the `get_relevant_tables` tool to retrieve schema context.
        agent_id:
            Logical identifier for logging.
        """
        self.metadata_agent = metadata_agent
        self.agent_id = agent_id

        tools = self._create_tools()
        self.agent = self._create_agent(tools)

        logger.info(f"DataInsightAgent '{agent_id}' initialised successfully.")

    # ─────────────────────────────────────────────────────────────────────────
    # Tool definitions
    # ─────────────────────────────────────────────────────────────────────────

    def _create_tools(self) -> List:
        """Return function tools registered with the LLM."""

        def get_relevant_tables(
            question: Annotated[
                str,
                Field(description="The user question or analytical task description"),
            ]
        ) -> str:
            """
            Retrieve schema metadata (tables, columns, descriptions, tags) relevant to
            the question by delegating to MetadataAgent.
            Call this BEFORE writing any SQL so you know the exact table and column names.
            """
            logger.info(f"[Tool:get_relevant_tables] question='{question[:80]}'")

            if self.metadata_agent is None:
                schemas_list = ", ".join(DatabricksConfig.SCHEMAS)
                return (
                    "MetadataAgent not configured. "
                    f"Catalog: {DatabricksConfig.CATALOG}, "
                    f"available schemas: {schemas_list}. "
                    "Please infer table names from the question context."
                )

            # Delegate synchronously to metadata agent
            result_container: Dict[str, Any] = {"result": None, "error": None}

            def _run():
                import asyncio
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    result_container["result"] = loop.run_until_complete(
                        self.metadata_agent.query(question)
                    )
                except Exception as exc:
                    result_container["error"] = exc
                finally:
                    loop.close()

            t = threading.Thread(target=_run)
            t.start()
            t.join(timeout=60)

            if t.is_alive():
                return "Metadata lookup timed out after 60 s."
            if result_container["error"]:
                logger.error(f"[Tool:get_relevant_tables] MetadataAgent error: {result_container['error']}")
                return f"Metadata error: {result_container['error']}"
            response = result_container["result"] or "No metadata returned."
            logger.debug(f"[Tool:get_relevant_tables] MetadataAgent returned:\n{response[:500]}")
            return response

        def execute_sql(
            sql: Annotated[
                str,
                Field(description="Fully-qualified SparkSQL / Delta SQL query to execute"),
            ],
            max_rows: Annotated[
                int,
                Field(description="Maximum number of rows to return (default 100, max 500)"),
            ] = 100,
        ) -> str:
            """
            Execute the provided SQL query against Azure Databricks and return the results.
            Only SELECT statements are permitted. Always use fully-qualified table names
            (catalog.schema.table).
            """
            logger.info(f"[Tool:execute_sql] Executing SQL (max_rows={max_rows}):\n{sql}")

            # Safety: block data-modification statements
            sql_upper = sql.strip().upper()
            forbidden = ("INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE", "ALTER", "CREATE")
            for kw in forbidden:
                if sql_upper.startswith(kw) or f" {kw} " in sql_upper:
                    return f"BLOCKED: '{kw}' statements are not permitted. Only SELECT is allowed."

            max_rows = min(max(1, max_rows), DatabricksConfig.MAX_ROWS)

            def _rewrite_invalid_qualify(original_sql: str) -> Optional[str]:
                """
                Databricks-specific recovery for patterns like:
                QUALIFY ROW_NUMBER() OVER (ORDER BY SUM(...) DESC)=1
                which can fail with aggregate resolution errors.
                """
                sql_text = original_sql.strip().rstrip(";")
                upper = sql_text.upper()
                if "QUALIFY" not in upper or "ROW_NUMBER" not in upper:
                    return None

                # Remove QUALIFY clause while preserving ORDER BY/LIMIT if present.
                rewritten = re.sub(
                    r"(?is)\s+QUALIFY\s+.+?(?=(\s+ORDER\s+BY|\s+LIMIT|$))",
                    "",
                    sql_text,
                ).strip()

                if rewritten == sql_text:
                    return None

                alias_match = re.search(r"(?is)SUM\([^\)]+\)\s+AS\s+([A-Za-z_][A-Za-z0-9_]*)", rewritten)
                if alias_match and not re.search(r"(?is)\bORDER\s+BY\b", rewritten):
                    rewritten += f" ORDER BY {alias_match.group(1)} DESC"
                if not re.search(r"(?is)\bLIMIT\b", rewritten):
                    rewritten += " LIMIT 1"

                return rewritten

            try:
                active_sql = sql
                try:
                    result = _run_databricks_query(active_sql, max_rows=max_rows)
                except Exception as first_exc:
                    msg = str(first_exc)
                    if "Cannot resolve QUALIFY" in msg and "aggregate functions" in msg and "QUALIFY" in active_sql.upper():
                        rewritten = _rewrite_invalid_qualify(active_sql)
                        if rewritten:
                            logger.warning(
                                "[Tool:execute_sql] Retrying after rewriting unsupported QUALIFY aggregate pattern."
                            )
                            logger.info(f"[Tool:execute_sql] Rewritten SQL:\n{rewritten}")
                            result = _run_databricks_query(rewritten, max_rows=max_rows)
                        else:
                            raise
                    else:
                        raise

                columns = result["columns"]
                rows = result["rows"]
                row_count = result["row_count"]

                if not rows:
                    return f"Query returned 0 rows."

                # Build markdown table for small results
                if row_count <= 20:
                    header = "| " + " | ".join(str(c) for c in columns) + " |"
                    separator = "|" + "|".join("---" for _ in columns) + "|"
                    body_lines = [
                        "| " + " | ".join(str(v) for v in row) + " |" for row in rows
                    ]
                    table = "\n".join([header, separator] + body_lines)
                    # Note: SQL is intentionally excluded here — it is already shown
                    # in the thinking panel via the execute_sql thinking event.
                    return f"Query returned {row_count} row(s).\n\n{table}"
                else:
                    # Summarise large results as JSON (no SQL block — shown in thinking)
                    summary = json.dumps(
                        {"columns": columns, "rows": rows[:5], "total_rows": row_count},
                        ensure_ascii=False,
                        indent=2,
                    )
                    return (
                        f"Query returned {row_count} row(s) (showing first 5 of {row_count}).\n\n"
                        f"```json\n{summary}\n```"
                    )

            except RuntimeError as exc:
                logger.error(f"[Tool:execute_sql] RuntimeError: {exc}")
                return f"Configuration error: {exc}"
            except Exception as exc:
                logger.error(f"[Tool:execute_sql] Unexpected error: {exc}", exc_info=True)
                return f"Query execution failed: {exc}"

        def load_skill(
            skill_name: Annotated[
                str,
                Field(description="The exact name of the skill to load (from the available_skills list)"),
            ]
        ) -> str:
            """
            Load the full instruction body of a named skill into the current context.
            Use this when you need detailed domain knowledge (e.g. column mappings, KPI formulas)
            before generating a query.
            """
            logger.info(f"[Tool:load_skill] Loading skill '{skill_name}'")
            body = skill_injector.load_skill_full_body(skill_name)
            if body is None:
                return f"Skill '{skill_name}' not found. Available skills: {skill_injector.build_skill_selection_info('')}"
            body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
            logger.info(
                f"[Tool:load_skill] Skill '{skill_name}' context returned to DataInsightAgent "
                f"(chars={len(body)}, sha256_12={body_hash})."
            )
            return body

        return [get_relevant_tables, execute_sql, load_skill]

    # ─────────────────────────────────────────────────────────────────────────
    # Agent creation
    # ─────────────────────────────────────────────────────────────────────────

    def _create_agent(self, tools: List):
        """Initialise the AzureOpenAIChatClient agent."""
        if AzureOpenAIConfig.use_api_key():
            client = AzureOpenAIChatClient(
                endpoint=AzureOpenAIConfig.ENDPOINT,
                deployment_name=AzureOpenAIConfig.GPT_DEPLOYMENT,
                api_key=AzureOpenAIConfig.API_KEY,
                api_version=AzureOpenAIConfig.API_VERSION,
            )
        else:
            client = AzureOpenAIChatClient(
                endpoint=AzureOpenAIConfig.ENDPOINT,
                deployment_name=AzureOpenAIConfig.GPT_DEPLOYMENT,
                credential=DefaultAzureCredential(),
                api_version=AzureOpenAIConfig.API_VERSION,
            )

        # Inject skill metadata into the system prompt
        enriched_prompt = skill_injector.inject_skills_metadata(DATA_INSIGHT_AGENT_PROMPT)
        # Add Databricks config context (list all available schemas)
        schemas_list = ", ".join(f"`{s}`" for s in DatabricksConfig.SCHEMAS)
        db_context = (
            f"\n\n## Databricks Context\n"
            f"- Catalog: `{DatabricksConfig.CATALOG}`\n"
            f"- Available schemas: {schemas_list}\n"
            f"- Default schema (when unspecified): `{DatabricksConfig.SCHEMA}`\n"
            f"- Always use fully-qualified names: `{DatabricksConfig.CATALOG}.<schema>.<table>`\n"
            f"- Max rows per query: {DatabricksConfig.MAX_ROWS}\n"
            f"- Configured: {DatabricksConfig.is_configured()}\n"
        )
        enriched_prompt += db_context

        agent = client.as_agent(
            name="DataInsightAgent",
            instructions=enriched_prompt,
            tools=tools,
            default_options={"temperature": 0.6},  # deterministic for SQL generation
        )
        logger.info("DataInsightAgent created with AzureOpenAIChatClient.")
        return agent

    # ─────────────────────────────────────────────────────────────────────────
    # Public interface
    # ─────────────────────────────────────────────────────────────────────────

    def get_new_thread(self):
        """Create a new MAF conversation thread."""
        return self.agent.get_new_thread()

    async def query(
        self,
        question: str,
        thread=None,
        schema_context: str = "",
    ) -> str:
        """
        Ask a data-related question.  The agent generates SQL, executes it, and
        returns a formatted analytical answer.

        Parameters
        ----------
        question:
            The user's analytical question in natural language.
        thread:
            Optional MAF thread for multi-turn context.
        schema_context:
            Optional pre-fetched metadata from MetadataAgent to prepend.
        """
        logger.info(f"DataInsightAgent.query: '{question[:80]}'")

        full_question = question
        if schema_context:
            full_question = (
                f"<schema_context>\n{schema_context}\n</schema_context>\n\n{question}"
            )

        result = await self.agent.run(full_question, thread=thread)
        logger.info(f"DataInsightAgent.query completed, len={len(result.text)}")
        return result.text

    async def query_stream(self, question: str, thread=None, schema_context: str = ""):
        """
        Streaming version of :meth:`query`.  Yields MAF update objects.
        Used by the FastAPI SSE endpoint.
        """
        logger.info(f"DataInsightAgent.query_stream: '{question[:80]}'")

        full_question = question
        if schema_context:
            full_question = (
                f"<schema_context>\n{schema_context}\n</schema_context>\n\n{question}"
            )

        async for update in self.agent.run_stream(full_question, thread=thread):
            yield update

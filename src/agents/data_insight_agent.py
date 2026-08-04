"""
Data Insight Agent — executes analytical SQL / SparkSQL against Azure Databricks Delta tables.

Architecture
------------
* Built on the same Microsoft Agent Framework (MAF) pattern as SearchAgent.
* Uses MAF OpenAIChatCompletionClient + function tools (no Databricks SDK yet, uses
  databricks-sql-connector for JDBC-style queries).
* Receives schema context from MetadataAgent (injected as part of the question).
* MAF SkillsProvider advertises and loads agent-scoped skills on demand.

Tools provided to the LLM
--------------------------
execute_sql           — runs a SQL string against the Databricks SQL warehouse and returns rows
recover_metadata_context — re-runs MetadataAgent only when upstream schema context is missing/incomplete
recover_ontology_context — re-runs OntologyAgent only when enabled context is unexpectedly missing
load_skill            — loads the full body of a named skill into the conversation context
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
import json
import re
import threading
from typing import Annotated, Any, Dict, List, Optional

from pydantic import Field
from sqlglot import exp, parse
from sqlglot.errors import ParseError

from ..config import DatabricksConfig, OntologyConfig
from ..prompts import DATA_INSIGHT_AGENT_PROMPT
from ..skills_provider import (
    begin_skill_usage_tracking,
    create_skills_provider,
    reset_skill_usage_tracking,
    skill_resource_was_read,
    skill_was_loaded,
)
from ..utils import get_logger
from .maf_runtime import (
    create_agent as create_maf_agent,
    create_session,
    run_agent,
    stream_agent,
)

logger = get_logger(__name__)

# ─── Databricks connection singleton (avoids per-query cold-start) ────────────
# Performance note: the biggest latency contributors are:
#   1. Databricks warehouse cold-start (first connect ~3-10 s, warm ~<1 s)
#   2. Native MetadataAgent and OntologyAgent model/tool iterations
#   3. DataInsightAgent SQL generation and result interpretation
# Reusing the JDBC connection eliminates the cold-start penalty for subsequent queries.
_db_connection: Optional[Any] = None
_db_lock = threading.Lock()


def _validate_sql_scope(sql: str) -> Optional[str]:
    """Return a blocking error when SQL references an unexposed UC object."""
    try:
        statements = [statement for statement in parse(sql, read="databricks") if statement]
    except ParseError as exc:
        return f"BLOCKED: SQL could not be parsed for catalog/schema validation: {exc}"
    if len(statements) != 1:
        return "BLOCKED: Exactly one SQL statement is permitted."

    statement = statements[0]
    cte_names = {
        cte.alias_or_name.casefold()
        for cte in statement.find_all(exp.CTE)
        if cte.alias_or_name
    }
    configured_catalog = DatabricksConfig.CATALOG
    configured_schemas = {
        schema.casefold(): schema for schema in DatabricksConfig.SCHEMAS
    }

    for table in statement.find_all(exp.Table):
        table_name = table.name
        if (
            not table.catalog
            and not table.db
            and table_name.casefold() in cte_names
        ):
            continue
        if not table.catalog or not table.db:
            return (
                f"BLOCKED: Physical table '{table.sql(dialect='databricks')}' must use a "
                "fully-qualified configured catalog.schema.table name."
            )
        if table.catalog.casefold() != configured_catalog.casefold():
            return (
                f"BLOCKED: Catalog '{table.catalog}' is outside the configured catalog "
                f"'{configured_catalog}'."
            )
        if table.db.casefold() not in configured_schemas:
            return (
                f"BLOCKED: Schema '{table.db}' is outside DATABRICKS_SCHEMAS "
                f"({', '.join(DatabricksConfig.SCHEMAS)})."
            )
    return None


@dataclass
class _RecoveryState:
    question: str
    schema_context: str
    ontology_context: str
    ontology_enabled: bool
    ontology_fallback: str
    governed_skill_context: str = ""
    metadata_attempts: int = 0
    ontology_attempts: int = 0


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
            _socket_timeout=DatabricksConfig.QUERY_TIMEOUT,
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
    Data Insight Agent using Microsoft Agent Framework 1.11.
    Generates and executes SQL queries against Azure Databricks Delta tables.
    """

    def __init__(
        self,
        metadata_agent: Optional[Any] = None,  # MetadataAgent or None
        ontology_agent: Optional[Any] = None,  # OntologyAgent or None
        agent_id: str = "data_insight_agent",
    ) -> None:
        """
        Parameters
        ----------
        metadata_agent:
            Optional MetadataAgent instance used only for grounded SQL identifier
            correction and bounded recovery when upstream schema is missing.
        ontology_agent:
            Optional OntologyAgent instance used only for bounded recovery when
            ontology was enabled but its context is unexpectedly missing.
        agent_id:
            Logical identifier for logging.
        """
        self.metadata_agent = metadata_agent
        self.ontology_agent = ontology_agent
        self.agent_id = agent_id
        self._recovery_state: ContextVar[Optional[_RecoveryState]] = ContextVar(
            f"{agent_id}_recovery_state",
            default=None,
        )

        tools = self._create_tools()
        self.agent = self._create_agent(tools)

        logger.info(f"DataInsightAgent '{agent_id}' initialised successfully.")

    @staticmethod
    def _recovery_result(
        status: str,
        source: str,
        *,
        message: str = "",
        reason: str = "",
        context: str = "",
    ) -> str:
        parsed_context: Any = context
        if context:
            try:
                parsed_context = json.loads(context)
            except (TypeError, json.JSONDecodeError):
                pass
        return json.dumps(
            {
                "status": status,
                "source": source,
                "reason": reason,
                "message": message,
                "context": parsed_context if context else None,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _original_question(question: str) -> str:
        match = re.search(
            r"(?is)<original_user_question>\s*(.*?)\s*</original_user_question>",
            question,
        )
        return match.group(1).strip() if match else question.strip()

    @staticmethod
    def _schema_context_status(schema_context: str) -> str:
        if not schema_context.strip():
            return "missing"
        try:
            parsed = json.loads(schema_context)
        except (TypeError, json.JSONDecodeError):
            return "present_unstructured"
        tool_results = parsed.get("all_tool_results", [])
        has_details = any(
            item.get("tool") == "get_table_details"
            and isinstance(item.get("result"), dict)
            and item["result"].get("status") == "ok"
            and bool(item["result"].get("columns"))
            for item in tool_results
            if isinstance(item, dict)
        )
        return "ready" if has_details else "incomplete"

    @staticmethod
    def _ontology_context_status(
        ontology_context: str,
        *,
        ontology_enabled: bool,
        ontology_fallback: str,
    ) -> str:
        if not ontology_enabled:
            return "disabled"
        if ontology_fallback:
            return "upstream_failed"
        if not ontology_context.strip():
            return "missing"
        try:
            parsed = json.loads(ontology_context)
        except (TypeError, json.JSONDecodeError):
            return "present_unstructured"
        return (
            "ready"
            if isinstance(parsed.get("primary_business_context"), dict)
            else "incomplete"
        )

    def _prepare_contextual_question(
        self,
        question: str,
        *,
        schema_context: str,
        ontology_context: str,
        ontology_fallback: str,
        ontology_enabled: bool,
        governed_skill_context: str = "",
        business_layer: str = "",
    ) -> tuple[_RecoveryState, str]:
        state = _RecoveryState(
            question=self._original_question(question),
            schema_context=schema_context,
            ontology_context=ontology_context,
            ontology_enabled=ontology_enabled,
            ontology_fallback=ontology_fallback,
            governed_skill_context=governed_skill_context,
        )
        if governed_skill_context:
            schema_status = "governed_skill"
            ontology_status = "skipped_by_skill"
        else:
            schema_status = self._schema_context_status(schema_context)
            ontology_status = self._ontology_context_status(
                ontology_context,
                ontology_enabled=ontology_enabled,
                ontology_fallback=ontology_fallback,
            )
        required_actions = []
        if schema_status in {"missing", "incomplete"}:
            required_actions.append("recover_metadata_context before SQL")
        if ontology_status in {"missing", "incomplete"}:
            required_actions.append("recover_ontology_context before SQL")
        context_blocks = [
            (
                "<context_recovery_status>\n"
                f"schema={schema_status}\n"
                f"ontology={ontology_status}\n"
                "required_actions="
                f"{'; '.join(required_actions) if required_actions else 'none'}\n"
                "A successful recovery tool result supersedes these initial statuses.\n"
                "</context_recovery_status>"
            )
        ]
        if ontology_context:
            context_blocks.append(
                f"<ontology_context>\n{ontology_context}\n</ontology_context>"
            )
        if business_layer:
            context_blocks.append(
                f"<business_layer_context>\n{business_layer}\n</business_layer_context>"
            )
        if schema_context:
            context_blocks.append(
                f"<schema_context>\n{schema_context}\n</schema_context>"
            )
        if ontology_fallback:
            context_blocks.append(
                f"<ontology_fallback>\n{ontology_fallback}\n</ontology_fallback>"
            )
        if governed_skill_context:
            context_blocks.append(
                "<governed_skill_context>\n"
                f"{governed_skill_context}\n"
                "</governed_skill_context>"
            )
        return state, "\n\n".join([*context_blocks, question])

    # ─────────────────────────────────────────────────────────────────────────
    # Tool definitions
    # ─────────────────────────────────────────────────────────────────────────

    def _create_tools(self) -> List:
        """Return function tools registered with the LLM."""

        async def recover_metadata_context(
            reason: Annotated[
                str,
                Field(
                    description=(
                        "Concrete missing table, column, join, or ambiguity that "
                        "prevents grounded SQL generation"
                    )
                ),
            ],
        ) -> str:
            """Recover schema context only when the upstream handoff is unusable."""
            state = self._recovery_state.get()
            if state is None:
                return self._recovery_result(
                    "error",
                    "MetadataAgent",
                    message="No active DataInsight recovery scope.",
                )
            if state.governed_skill_context:
                return self._recovery_result(
                    "not_required",
                    "MetadataAgent",
                    message="A governed Skill resource supplies the physical SQL contract.",
                )
            if state.metadata_attempts >= 1:
                return self._recovery_result(
                    "exhausted",
                    "MetadataAgent",
                    message="The one allowed metadata recovery attempt was already used.",
                )
            if self.metadata_agent is None:
                return self._recovery_result(
                    "unavailable",
                    "MetadataAgent",
                    message="MetadataAgent is not configured.",
                )

            state.metadata_attempts += 1
            logger.warning(
                "[Tool:recover_metadata_context] reason='%s'",
                reason[:240],
            )
            try:
                recovered = await asyncio.wait_for(
                    self.metadata_agent.query(
                        state.question,
                        ontology_context=state.ontology_context,
                        require_metadata_mapping=not bool(state.ontology_context),
                    ),
                    timeout=DatabricksConfig.METADATA_AGENT_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                return self._recovery_result(
                    "timeout",
                    "MetadataAgent",
                    message=(
                        "Metadata recovery timed out after "
                        f"{DatabricksConfig.METADATA_AGENT_TIMEOUT_SECONDS} seconds."
                    ),
                )
            except Exception as exc:
                return self._recovery_result(
                    "error",
                    "MetadataAgent",
                    message=str(exc),
                )

            state.schema_context = recovered
            return self._recovery_result(
                "ok",
                "MetadataAgent",
                reason=reason,
                context=recovered,
            )

        async def recover_ontology_context(
            reason: Annotated[
                str,
                Field(
                    description=(
                        "Concrete missing business meaning, relationship, hierarchy, "
                        "factor, or lineage needed for deeper analysis"
                    )
                ),
            ],
        ) -> str:
            """Recover ontology context only when that session requested ontology."""
            state = self._recovery_state.get()
            if state is None:
                return self._recovery_result(
                    "error",
                    "OntologyAgent",
                    message="No active DataInsight recovery scope.",
                )
            if state.governed_skill_context:
                return self._recovery_result(
                    "not_required",
                    "OntologyAgent",
                    message="Ontology discovery was intentionally skipped by a governed Skill route.",
                )
            if not state.ontology_enabled:
                return self._recovery_result(
                    "disabled",
                    "OntologyAgent",
                    message="Ontology is disabled for this request.",
                )
            if state.ontology_fallback:
                return self._recovery_result(
                    "upstream_failed",
                    "OntologyAgent",
                    message=(
                        "The upstream OntologyAgent already failed; it will not be "
                        "retried inside DataInsightAgent."
                    ),
                )
            if state.ontology_attempts >= 1:
                return self._recovery_result(
                    "exhausted",
                    "OntologyAgent",
                    message="The one allowed ontology recovery attempt was already used.",
                )
            if self.ontology_agent is None:
                return self._recovery_result(
                    "unavailable",
                    "OntologyAgent",
                    message="OntologyAgent is not configured.",
                )
            if not state.schema_context.strip():
                return self._recovery_result(
                    "requires_metadata",
                    "OntologyAgent",
                    message=(
                        "Recover MetadataAgent context first so ontology semantics can "
                        "be checked against physical schema."
                    ),
                )

            state.ontology_attempts += 1
            logger.warning(
                "[Tool:recover_ontology_context] reason='%s'",
                reason[:240],
            )
            try:
                recovered = await asyncio.wait_for(
                    self.ontology_agent.query(
                        state.question,
                        schema_context=state.schema_context,
                    ),
                    timeout=OntologyConfig.AGENT_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                return self._recovery_result(
                    "timeout",
                    "OntologyAgent",
                    message=(
                        "Ontology recovery timed out after "
                        f"{OntologyConfig.AGENT_TIMEOUT_SECONDS} seconds."
                    ),
                )
            except Exception as exc:
                return self._recovery_result(
                    "error",
                    "OntologyAgent",
                    message=str(exc),
                )

            state.ontology_context = recovered
            return self._recovery_result(
                "ok",
                "OntologyAgent",
                reason=reason,
                context=recovered,
            )

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

            state = self._recovery_state.get()
            required_skill = "ontology-sql-planning"
            required_resource = ""
            if state is not None and state.governed_skill_context:
                try:
                    governed = json.loads(state.governed_skill_context)
                except (TypeError, json.JSONDecodeError):
                    governed = {}
                required_skill = str(governed.get("skill_name") or "")
                required_resource = str(governed.get("resource_name") or "")
                if not required_skill or not required_resource:
                    return (
                        "BLOCKED: Governed Skill context is invalid. A validated Skill and "
                        "indexed resource are required before SQL execution."
                    )

            if state is not None and not skill_was_loaded(required_skill):
                return (
                    f"BLOCKED: Required Skill '{required_skill}' has not been loaded in this "
                    "request. Call load_skill before execute_sql."
                )
            if (
                state is not None
                and required_resource
                and not skill_resource_was_read(required_skill, required_resource)
            ):
                return (
                    f"BLOCKED: Required Skill resource '{required_resource}' has not been read "
                    "in this request. Call read_skill_resource before execute_sql."
                )

            # Safety: block data-modification statements
            sql_upper = sql.strip().upper()
            forbidden = ("INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE", "ALTER", "CREATE")
            for kw in forbidden:
                if sql_upper.startswith(kw) or f" {kw} " in sql_upper:
                    return f"BLOCKED: '{kw}' statements are not permitted. Only SELECT is allowed."

            scope_error = _validate_sql_scope(sql)
            if scope_error:
                return scope_error

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
                corrections: list[dict[str, str]] = []
                if self.metadata_agent is not None and hasattr(
                    self.metadata_agent,
                    "rewrite_sql_identifiers",
                ):
                    active_sql, corrections = self.metadata_agent.rewrite_sql_identifiers(
                        active_sql
                    )
                    if corrections:
                        logger.info(
                            "[Tool:execute_sql] Corrected SQL identifiers from UC metadata: %s",
                            corrections,
                        )
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

        return [
            recover_metadata_context,
            recover_ontology_context,
            execute_sql,
        ]

    # ─────────────────────────────────────────────────────────────────────────
    # Agent creation
    # ─────────────────────────────────────────────────────────────────────────

    def _create_agent(self, tools: List):
        """Initialise the MAF DataInsightAgent."""
        enriched_prompt = DATA_INSIGHT_AGENT_PROMPT
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

        skills_provider = create_skills_provider("DataInsightAgent")
        agent = create_maf_agent(
            name="DataInsightAgent",
            instructions=enriched_prompt,
            tools=tools,
            temperature=0.6,
            context_providers=[skills_provider] if skills_provider else None,
        )
        logger.info("DataInsightAgent created with MAF OpenAIChatCompletionClient.")
        return agent

    # ─────────────────────────────────────────────────────────────────────────
    # Public interface
    # ─────────────────────────────────────────────────────────────────────────

    def get_new_thread(self):
        """Create a new MAF conversation thread."""
        return create_session(self.agent)

    async def query(
        self,
        question: str,
        thread=None,
        schema_context: str = "",
        ontology_context: str = "",
        ontology_fallback: str = "",
        ontology_enabled: bool = False,
        governed_skill_context: str = "",
        business_layer: str = "",
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
        state, full_question = self._prepare_contextual_question(
            question,
            schema_context=schema_context,
            ontology_context=ontology_context,
            ontology_fallback=ontology_fallback,
            ontology_enabled=ontology_enabled,
            governed_skill_context=governed_skill_context,
            business_layer=business_layer,
        )
        token = self._recovery_state.set(state)
        skill_token = begin_skill_usage_tracking()
        try:
            result = await run_agent(self.agent, full_question, session=thread)
            logger.info(f"DataInsightAgent.query completed, len={len(result.text)}")
            return result.text
        finally:
            reset_skill_usage_tracking(skill_token)
            self._recovery_state.reset(token)

    async def query_stream(
        self,
        question: str,
        thread=None,
        schema_context: str = "",
        ontology_context: str = "",
        ontology_fallback: str = "",
        ontology_enabled: bool = False,
        governed_skill_context: str = "",
        business_layer: str = "",
    ):
        """
        Streaming version of :meth:`query`.  Yields MAF update objects.
        Used by the FastAPI SSE endpoint.
        """
        logger.info(f"DataInsightAgent.query_stream: '{question[:80]}'")
        state, full_question = self._prepare_contextual_question(
            question,
            schema_context=schema_context,
            ontology_context=ontology_context,
            ontology_fallback=ontology_fallback,
            ontology_enabled=ontology_enabled,
            governed_skill_context=governed_skill_context,
            business_layer=business_layer,
        )
        token = self._recovery_state.set(state)
        skill_token = begin_skill_usage_tracking()
        try:
            async for update in stream_agent(
                self.agent,
                full_question,
                session=thread,
            ):
                yield update
        finally:
            reset_skill_usage_tracking(skill_token)
            self._recovery_state.reset(token)

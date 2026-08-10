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
from datetime import date, datetime, time as _time
from decimal import Decimal
import json
import re
import threading
from typing import Annotated, Any, Dict, List, Optional

from pydantic import Field
from sqlglot import exp, parse
from sqlglot.errors import ParseError

from ..config import AgentReasoningConfig, DatabricksConfig, OntologyConfig
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
# Re-entrant so a query can hold it across acquisition and execution.
_db_lock = threading.RLock()


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


def _extract_sql_measures(sql: str) -> dict[str, Any]:
    """Report what a query actually aggregated, grouped, and filtered, straight from its AST."""
    try:
        statements = [statement for statement in parse(sql, read="databricks") if statement]
    except ParseError:
        return {}
    if not statements:
        return {}
    statement = statements[0]

    cte_names = {
        cte.alias_or_name.casefold()
        for cte in statement.find_all(exp.CTE)
        if cte.alias_or_name
    }
    tables = sorted(
        {
            f"{table.db}.{table.name}" if table.db else table.name
            for table in statement.find_all(exp.Table)
            if table.name and table.name.casefold() not in cte_names
        }
    )

    alias_bases: dict[str, set[str]] = {}
    for alias in statement.find_all(exp.Alias):
        name = alias.alias
        if not name:
            continue
        sources = {column.name for column in alias.find_all(exp.Column) if column.name}
        alias_bases.setdefault(name, set()).update(sources - {name})

    # Resolve alias-of-alias chains so a derived name points at real columns.
    for _ in range(len(alias_bases) + 1):
        changed = False
        for name, sources in list(alias_bases.items()):
            expanded = set()
            for source in sources:
                expanded |= alias_bases.get(source, {source}) if source != name else {source}
            expanded.discard(name)
            if expanded != sources:
                alias_bases[name] = expanded
                changed = True
        if not changed:
            break

    aggregates: set[str] = set()
    aggregate_columns: set[str] = set()
    for function in statement.find_all(exp.AggFunc):
        columns = sorted({column.name for column in function.find_all(exp.Column) if column.name})
        aggregate_columns.update(columns)
        distinct = "DISTINCT " if any(function.find_all(exp.Distinct)) else ""
        aggregates.add(f"{function.sql_name()}({distinct}{', '.join(columns) or '*'})")

    def column_names(node_type: Any) -> list[str]:
        return sorted(
            {
                column.name
                for node in statement.find_all(node_type)
                for column in node.find_all(exp.Column)
                if column.name
            }
        )

    group_by = column_names(exp.Group)
    filter_columns = column_names(exp.Where)
    referenced = set(group_by) | set(filter_columns) | aggregate_columns
    derived = {
        name: sorted(sources)
        for name, sources in alias_bases.items()
        if name in referenced and sources
    }

    return {
        "tables": tables,
        "aggregates": sorted(aggregates),
        "group_by": group_by,
        "filter_columns": filter_columns,
        "derived_names": derived,
    }


_COMPARISON_COLUMN_PATTERN = re.compile(
    r"(?:difference|diff|delta|variance|gap|change|uplift|差异|差额|变化|增减)",
    re.IGNORECASE,
)
_IDENTIFIER_COLUMN_PATTERN = re.compile(
    r"(?:^|_)(?:id|key|code|rank|index|number)(?:$|_)|"
    r"(?:id|key|code|rank|index|number)$|(?:编号|代码|排名)$",
    re.IGNORECASE,
)
_MULTI_OBSERVATION_PATTERN = re.compile(
    r"(?:compare|comparison|difference|trend|distribution|breakdown|across|"
    r"\bby\b|monthly|daily|weekly|yearly|top\s*\d+|analy[sz]e|"
    r"比较|差异|趋势|分布|拆分|前\s*\d+|分别|各(?:个|类|地区|月|年)|按[^\s]{0,12}(?:统计|汇总|地区|月|年|类别|产品))",
    re.IGNORECASE,
)


def _profile_query_result(
    columns: list[str],
    rows: list[list[Any]],
    *,
    question: str = "",
    minimum_sample_rows: int = 3,
) -> dict[str, Any]:
    """Detect result shapes that cannot support a confident analytical conclusion."""

    row_count = len(rows)
    expects_multiple = bool(_MULTI_OBSERVATION_PATTERN.search(question or ""))
    signals: list[dict[str, Any]] = []
    if row_count == 0:
        signals.append({"code": "empty_result", "row_count": 0})

    def is_number(value: Any) -> bool:
        return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)

    numeric_values: dict[str, list[Any]] = {}
    for index, column in enumerate(columns):
        values = [
            row[index]
            for row in rows
            if index < len(row) and row[index] is not None and is_number(row[index])
        ]
        if values and not _IDENTIFIER_COLUMN_PATTERN.search(str(column)):
            numeric_values[str(column)] = values

    comparison_columns = [
        column
        for column in numeric_values
        if _COMPARISON_COLUMN_PATTERN.search(column)
    ]
    all_comparison_values_zero = bool(comparison_columns) and all(
        all(value == 0 for value in numeric_values[column])
        for column in comparison_columns
    )
    if all_comparison_values_zero:
        signals.append(
            {
                "code": "all_comparison_values_zero",
                "columns": comparison_columns,
            }
        )

    non_comparison_columns = [
        column for column in numeric_values if column not in comparison_columns
    ]
    identical_column_pairs: list[list[str]] = []
    column_indexes = {str(column): index for index, column in enumerate(columns)}
    for left_index, left in enumerate(non_comparison_columns):
        for right in non_comparison_columns[left_index + 1 :]:
            left_position = column_indexes[left]
            right_position = column_indexes[right]
            comparable = [
                (row[left_position], row[right_position])
                for row in rows
                if left_position < len(row)
                and right_position < len(row)
                and row[left_position] is not None
                and row[right_position] is not None
                and is_number(row[left_position])
                and is_number(row[right_position])
            ]
            if len(comparable) >= 2 and all(
                left_value == right_value
                for left_value, right_value in comparable
            ):
                identical_column_pairs.append([left, right])
    if identical_column_pairs:
        signals.append(
            {
                "code": "identical_measure_columns",
                "pairs": identical_column_pairs,
            }
        )

    all_numeric_values_zero = bool(numeric_values) and all(
        all(value == 0 for value in values)
        for values in numeric_values.values()
    )
    if all_numeric_values_zero:
        signals.append(
            {
                "code": "all_numeric_values_zero",
                "columns": list(numeric_values),
            }
        )

    constant_measure_columns = [
        column
        for column, values in numeric_values.items()
        if row_count > 1 and len(set(values)) == 1
    ]
    if expects_multiple and constant_measure_columns:
        signals.append(
            {
                "code": "constant_measure_columns",
                "columns": constant_measure_columns,
            }
        )

    low_sample = 0 < row_count < max(1, minimum_sample_rows)
    if expects_multiple and low_sample:
        signals.append(
            {
                "code": "low_sample",
                "row_count": row_count,
                "expected_minimum": minimum_sample_rows,
            }
        )

    hard_signal_codes = {
        "empty_result",
        "all_comparison_values_zero",
        "identical_measure_columns",
        "all_numeric_values_zero",
    }
    requires_follow_up = any(
        signal["code"] in hard_signal_codes for signal in signals
    ) or (
        expects_multiple
        and any(
            signal["code"] in {"constant_measure_columns", "low_sample"}
            for signal in signals
        )
    )
    return {
        "row_count": row_count,
        "expects_multiple_observations": expects_multiple,
        "numeric_measure_columns": list(numeric_values),
        "comparison_columns": comparison_columns,
        "identical_column_pairs": identical_column_pairs,
        "signals": signals,
        "requires_follow_up": requires_follow_up,
    }


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
    diagnostic_attempts: int = 0
    pending_diagnostic: Optional[dict[str, Any]] = None


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


def _json_default(value: Any) -> str:
    """Databricks returns date, datetime and Decimal objects that `json` cannot encode."""
    if isinstance(value, (date, datetime, _time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _is_connection_level_error(exc: BaseException) -> bool:
    """A server-side SQL error leaves the connection usable; anything else may not."""
    try:
        from databricks.sql import exc as dbsql_exc
    except ImportError:
        return True
    return not isinstance(exc, dbsql_exc.ServerOperationError)


def _run_databricks_query(sql: str, max_rows: int = 500) -> Dict[str, Any]:
    """
    Execute *sql* against the configured Databricks SQL warehouse.
    Reuses a persistent connection to avoid per-call cold-start latency.
    """
    global _db_connection

    # One process-wide connection is shared by every session thread, and a DB-API
    # connection is not safe for concurrent cursors, so queries run one at a time.
    with _db_lock:
        connection = _get_db_connection()
        cursor = None
        try:
            cursor = connection.cursor()
            cursor.execute(sql)
            raw_rows = cursor.fetchmany(max_rows)
            columns = [desc[0] for desc in (cursor.description or [])]
            rows = [list(row) for row in raw_rows]
            return {"columns": columns, "rows": rows, "row_count": len(rows), "sql": sql}
        except Exception as exc:
            if _is_connection_level_error(exc):
                _db_connection = None
            raise
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass


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
    def _diagnostic_follow_up_message(state: _RecoveryState) -> str:
        return (
            "<mandatory_result_diagnostic>\n"
            "The previous SQL result was analytically degenerate and the first response "
            "must not be finalized. Using the ontology and verified schema already in this "
            "session, execute exactly one focused source-level SQL query with "
            "purpose=\"diagnostic\". Distinguish source equality, aggregation/grain "
            "collapse, null or mapping gaps, low cardinality, and period/sample coverage. "
            "Then provide one revised final answer grounded in both query results.\n"
            f"signals={json.dumps(state.pending_diagnostic or {}, ensure_ascii=False, default=_json_default)}\n"
            "</mandatory_result_diagnostic>"
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
            try:
                governed_name = str(
                    json.loads(governed_skill_context).get("skill_name") or ""
                ).strip()
            except (TypeError, json.JSONDecodeError, AttributeError):
                governed_name = ""
            definition_source = (
                "skill_contract\n"
                "Definitions come from the governed Skill resource.\n"
                "allowed_source_labels=用户指定 / User-stated | "
                f"Skill: {governed_name or 'governed template'} | "
                "系统默认 / System default | 推断 / Inferred"
            )
        elif ontology_enabled:
            definition_source = (
                "available\n"
                "Business meaning comes from the active ontology. MetadataAgent ran in verification "
                "mode without the metadata-mapping glossary, so that Skill is not a valid source "
                "here.\n"
                "allowed_source_labels=用户指定 / User-stated | 本体 / Ontology | "
                "Skill: sql-planning | 系统默认 / System default | 推断 / Inferred"
            )
        else:
            definition_source = (
                "unavailable\n"
                "No ontology is active, so business meaning came from the loaded Skills, the "
                "deterministic rules in your instructions, or your own choice.\n"
                "allowed_source_labels=用户指定 / User-stated | Skill: metadata-mapping | "
                "Skill: sql-planning | 系统默认 / System default | 推断 / Inferred"
            )
        context_blocks.append(
            "<definition_provenance>\n"
            f"governed_definitions={definition_source}\n"
            "</definition_provenance>"
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
            purpose: Annotated[
                str,
                Field(
                    description=(
                        "Use 'analysis' for the requested result or 'diagnostic' for the one "
                        "required follow-up after a degenerate result"
                    )
                ),
            ] = "analysis",
        ) -> str:
            """
            Execute the provided SQL query against Azure Databricks and return the results.
            Only SELECT statements are permitted. Always use fully-qualified table names
            (catalog.schema.table).
            """
            logger.info(
                "[Tool:execute_sql] Executing SQL (purpose=%s, max_rows=%s):\n%s",
                purpose,
                max_rows,
                sql,
            )

            state = self._recovery_state.get()
            normalized_purpose = str(purpose or "analysis").strip().lower()
            if normalized_purpose not in {"analysis", "diagnostic"}:
                return "BLOCKED: purpose must be either 'analysis' or 'diagnostic'."
            if (
                state is not None
                and state.pending_diagnostic is not None
                and normalized_purpose != "diagnostic"
            ):
                return (
                    "BLOCKED: The previous result was analytically degenerate. Execute one "
                    "focused source-level query with purpose='diagnostic' before finalizing."
                )
            if (
                state is not None
                and normalized_purpose == "diagnostic"
                and state.diagnostic_attempts >= 1
            ):
                return "BLOCKED: The one allowed diagnostic SQL query was already executed."
            if (
                state is not None
                and normalized_purpose == "diagnostic"
                and state.pending_diagnostic is None
            ):
                return "BLOCKED: No degenerate result is awaiting a diagnostic query."
            required_skill = "sql-planning"
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
                    executed_sql = active_sql
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
                            executed_sql = rewritten
                            result = _run_databricks_query(rewritten, max_rows=max_rows)
                        else:
                            raise
                    else:
                        raise

                columns = result["columns"]
                rows = result["rows"]
                row_count = result["row_count"]
                diagnostics = _profile_query_result(
                    [str(column) for column in columns],
                    rows,
                    question=state.question if state is not None else "",
                )
                if state is not None and normalized_purpose == "diagnostic":
                    state.diagnostic_attempts += 1
                    state.pending_diagnostic = None
                    diagnostics["requires_follow_up"] = False
                    diagnostics["diagnostic_completed"] = True
                elif diagnostics["requires_follow_up"]:
                    diagnostics["diagnostic_completed"] = False
                    if state is not None and state.diagnostic_attempts < 1:
                        state.pending_diagnostic = diagnostics
                    elif state is not None:
                        diagnostics["requires_follow_up"] = False
                        diagnostics["diagnostic_limit_reached"] = True
                diagnostics["next_action"] = (
                    "Execute exactly one focused source-level SQL query with "
                    "purpose='diagnostic' before answering. Distinguish source-grain "
                    "equality, aggregation/grain collapse, null or coverage gaps, and "
                    "insufficient sample coverage using the verified schema."
                    if diagnostics["requires_follow_up"]
                    else "Interpret the measured result without inventing unsupported causes."
                )
                diagnostic_block = (
                    "\n\n<result_diagnostics>\n"
                    + json.dumps(
                        diagnostics,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        default=_json_default,
                    )
                    + "\n</result_diagnostics>"
                )
                measures = _extract_sql_measures(executed_sql)
                if measures:
                    measures["note"] = (
                        "Extracted from the executed SQL. Any statement about which columns, "
                        "grain, or filters produced these numbers must match this block."
                    )
                    diagnostic_block += (
                        "\n\n<measures_used>\n"
                        + json.dumps(measures, ensure_ascii=False, separators=(",", ":"))
                        + "\n</measures_used>"
                    )

                if not rows:
                    return f"Query returned 0 rows.{diagnostic_block}"

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
                    return f"Query returned {row_count} row(s).\n\n{table}{diagnostic_block}"
                else:
                    # Summarise large results as JSON (no SQL block — shown in thinking)
                    summary = json.dumps(
                        {"columns": columns, "rows": rows[:5], "total_rows": row_count},
                        ensure_ascii=False,
                        indent=2,
                        default=_json_default,
                    )
                    return (
                        f"Query returned {row_count} row(s) (showing first 5 of {row_count}).\n\n"
                        f"```json\n{summary}\n```{diagnostic_block}"
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
            # This agent chooses the metric, grain, dimension level, and time window; sampling
            # variance here makes the same question return a different 口径 on every run.
            reasoning_effort=AgentReasoningConfig.DATA_INSIGHT,
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
            active_session = thread or self.get_new_thread()
            result = await run_agent(
                self.agent,
                full_question,
                session=active_session,
            )
            if state.pending_diagnostic is not None:
                result = await run_agent(
                    self.agent,
                    self._diagnostic_follow_up_message(state),
                    session=active_session,
                )
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
            active_session = thread or self.get_new_thread()
            async for update in stream_agent(
                self.agent,
                full_question,
                session=active_session,
            ):
                yield update
            if state.pending_diagnostic is not None:
                async for update in stream_agent(
                    self.agent,
                    self._diagnostic_follow_up_message(state),
                    session=active_session,
                ):
                    yield update
        finally:
            reset_skill_usage_tracking(skill_token)
            self._recovery_state.reset(token)

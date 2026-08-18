"""Native agent-loop data-analysis pipeline tests."""

from contextvars import ContextVar
import json
from threading import Event
from types import SimpleNamespace

import pytest
from agent_framework import SkillsSourceContext

from src.agents.data_insight_agent import (
    DataInsightAgent,
    _extract_sql_measures,
    _profile_query_result,
)
from src.agents.master_agent import MasterAgent
from src.agents.metadata_agent import MetadataAgent
from src.agents.ontology_agent import OntologyAgent
from src.config import AzureOpenAIConfig, DatabricksConfig
from src.prompts import (
    DATA_INSIGHT_AGENT_PROMPT,
    MASTER_AGENT_PROMPT,
    METADATA_AGENT_PROMPT,
    ONTOLOGY_AGENT_PROMPT,
)
from src.skills_provider import (
    begin_skill_usage_tracking,
    reset_skill_usage_tracking,
)
from src.utils.activity import ontology_tool_result_fields


class _MetadataAgent:
    def __init__(self, calls: list[dict] | None = None) -> None:
        self.calls = calls

    async def query_stream(
        self,
        question,
        ontology_context="",
        require_metadata_mapping=False,
        context_sink=None,
    ):
        if self.calls is not None:
            self.calls.append(
                {
                    "agent": "metadata",
                    "question": question,
                    "ontology_context": ontology_context,
                    "require_metadata_mapping": require_metadata_mapping,
                }
            )
        if context_sink is not None:
            context_sink.append(
                {
                    "tool": "get_table_details",
                    "arguments": {"table_name": "salesorderheader"},
                    "result": {
                        "status": "ok",
                        "full_name": "ai_data_insight.silver.salesorderheader",
                        "columns": [
                            {"name": "OrderDate"},
                            {"name": "TotalDue"},
                        ],
                        "cache_hit": True,
                    },
                }
            )
        yield SimpleNamespace(
            text="tables: salesorderheader; fields: OrderDate, TotalDue",
            contents=[],
        )
    @staticmethod
    def build_collected_context(agent_summary, tool_results):
        return json.dumps(
            {
                "status": "ok" if tool_results else "no_tool_results",
                "source": "metadata_agent",
                "agent_summary": agent_summary,
                "all_tool_results": tool_results,
            }
        )


class _DataInsightAgent:
    def __init__(self, calls: list) -> None:
        self.calls = calls

    async def query_stream(
        self,
        question,
        schema_context="",
        ontology_context="",
        ontology_fallback="",
        ontology_enabled=False,
        governed_skill_context="",
        business_layer="",
    ):
        self.calls.append(
            {
                "agent": "data",
                "question": question,
                "schema_context": schema_context,
                "ontology_context": ontology_context,
                "ontology_fallback": ontology_fallback,
                "ontology_enabled": ontology_enabled,
                "governed_skill_context": governed_skill_context,
                "business_layer": business_layer,
            }
        )
        yield SimpleNamespace(text="analysis result", contents=[])


class _OntologyAgent:
    def __init__(self, calls: list[dict], result: str) -> None:
        self.calls = calls
        self.result = result

    async def query_stream(
        self,
        question,
        schema_context="",
        context_sink=None,
    ):
        self.calls.append(
            {
                "agent": "ontology",
                "question": question,
                "schema_context": schema_context,
            }
        )
        try:
            payload = __import__("json").loads(self.result)
        except Exception:
            payload = None
        if context_sink is not None and payload is not None:
            context_sink.append(
                {
                    "tool": "get_business_context",
                    "arguments": {"question": question},
                    "result": payload,
                }
            )
        yield SimpleNamespace(text="ontology summary", contents=[])

    @staticmethod
    def build_collected_context(agent_summary, tool_results):
        return OntologyAgent.build_collected_context(agent_summary, tool_results)


class _RaisingOntologyAgent:
    async def query_stream(
        self,
        question,
        schema_context="",
        context_sink=None,
    ):
        if False:
            yield None
        raise RuntimeError("ontology query exploded")


class _SkillRoutingOntologyAgent:
    async def query_stream(
        self,
        question,
        schema_context="",
        context_sink=None,
    ):
        yield SimpleNamespace(
            text="",
            contents=[
                SimpleNamespace(
                    type="function_call",
                    name="load_skill",
                    arguments='{"skill_name":"analytics-spec"}',
                    call_id="ontology-skill-call",
                )
            ],
        )
        yield SimpleNamespace(
            text="",
            contents=[
                SimpleNamespace(
                    type="function_result",
                    call_id="ontology-skill-call",
                    result="analytics-spec loaded",
                    exception=None,
                )
            ],
        )
        yield SimpleNamespace(
            text=json.dumps(
                {
                    "route": "governed_skill",
                    "skill_name": "analytics-spec",
                    "resource_name": "references/highest-spending-customer.sql",
                    "match_reason": {
                        "metric": "total customer spending",
                        "grain": "customer",
                        "cardinality": "top 1",
                        "period": "2023",
                    },
                }
            ),
            contents=[],
        )

    @staticmethod
    def build_collected_context(agent_summary, tool_results):
        return OntologyAgent.build_collected_context(agent_summary, tool_results)


class _RaisingMetadataAgent:
    def __init__(self, calls: list[dict]) -> None:
        self.calls = calls

    async def query_stream(
        self,
        question,
        ontology_context="",
        require_metadata_mapping=False,
        context_sink=None,
    ):
        self.calls.append(
            {
                "agent": "metadata",
                "question": question,
                "ontology_context": ontology_context,
                "require_metadata_mapping": require_metadata_mapping,
            }
        )
        if False:
            yield None
        raise RuntimeError("unity catalog verification exploded")


class _CancellingOntologyAgent:
    def __init__(self, cancel_event: Event) -> None:
        self.cancel_event = cancel_event

    async def query_stream(
        self,
        question,
        schema_context="",
        context_sink=None,
    ):
        self.cancel_event.set()
        yield SimpleNamespace(text="", contents=[])

    @staticmethod
    def build_collected_context(agent_summary, tool_results):
        return '{"status":"no_tool_results","primary_business_context":null,"all_tool_results":[]}'


class _EventQueue:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict]] = []

    def put_nowait(self, item) -> None:
        self.items.append(item)


class _ImmediateLoop:
    def call_soon_threadsafe(self, callback, *args) -> None:
        callback(*args)


def test_data_insight_exposes_bounded_recovery_and_sql_tools() -> None:
    agent = DataInsightAgent.__new__(DataInsightAgent)
    agent.metadata_agent = None
    agent.ontology_agent = None
    agent._recovery_state = ContextVar("data-recovery-tools-test", default=None)

    assert [tool.__name__ for tool in agent._create_tools()] == [
        "recover_metadata_context",
        "recover_ontology_context",
        "execute_sql",
    ]


def test_master_runtime_context_matches_session_ontology_mode() -> None:
    disabled = MasterAgent._with_runtime_context(
        "monthly sales",
        enable_ontology=False,
        max_search_attempts=5,
    )
    enabled = MasterAgent._with_runtime_context(
        "monthly sales",
        enable_ontology=True,
    )

    assert "ontology_enabled=false" in disabled
    assert "pipeline_handoff=MetadataAgent -> DataInsightAgent" in disabled
    assert "OntologyAgent" not in disabled
    assert "search_attempt_limit=5" in disabled
    assert "search_attempt_limit_is_ceiling_not_target=true" in disabled
    assert "ontology_enabled=true" in enabled
    assert "OntologyAgent -> conditional MetadataAgent -> DataInsightAgent" in enabled
    assert "ontology_enabled=false" in MASTER_AGENT_PROMPT
    assert "MUST NOT mention OntologyAgent" in MASTER_AGENT_PROMPT
    assert "hard ceiling, never as a target" in MASTER_AGENT_PROMPT
    assert "stop calling search tools and answer now" in MASTER_AGENT_PROMPT
    assert "completed two retrieval attempts" not in MASTER_AGENT_PROMPT


def test_master_prompt_repairs_queries_without_inventing_constraints() -> None:
    assert "Never add a qualifier the user did not state" in MASTER_AGENT_PROMPT
    assert "are constraints, not enrichment" in MASTER_AGENT_PROMPT
    assert "repaired only where the original wording is defective" in MASTER_AGENT_PROMPT
    assert "corrected and enriched query" not in MASTER_AGENT_PROMPT
    assert "correcting and enriching terminology" not in MASTER_AGENT_PROMPT
    # A self-introduced version qualifier must not suppress an otherwise supported answer.
    assert "never withhold or downgrade the answer" in MASTER_AGENT_PROMPT
    # The data path keeps the same protection without changing its own agents.
    assert "do not add filters, time windows, or qualifiers the user did not state" in (
        MASTER_AGENT_PROMPT
    )


def test_ontology_agent_uses_primary_deployment(monkeypatch) -> None:
    captured: dict = {}
    ontology_agent = OntologyAgent.__new__(OntologyAgent)
    ontology_agent.ontology_service = SimpleNamespace(
        health=lambda: {
            "file_count": 1,
            "entity_count": 1,
            "reasoner": "disabled",
            "reasoning_status": "disabled",
        }
    )

    monkeypatch.setattr(
        "src.agents.ontology_agent.create_skills_provider",
        lambda _agent_name: None,
    )
    monkeypatch.setattr(
        "src.agents.ontology_agent.create_maf_agent",
        lambda **kwargs: captured.update(kwargs) or object(),
    )

    ontology_agent._create_agent([])

    assert captured["model"] == AzureOpenAIConfig.GPT_DEPLOYMENT


def test_result_diagnostics_detects_degenerate_comparison_without_domain_rules() -> None:
    diagnostics = _profile_query_result(
        ["group_name", "scenario_a", "scenario_b", "difference"],
        [
            ["A", 120.0, 120.0, 0.0],
            ["B", 80.0, 80.0, 0.0],
            ["C", 25.0, 25.0, 0.0],
        ],
        question="Compare the two scenarios and return the largest differences",
    )
    signal_codes = {signal["code"] for signal in diagnostics["signals"]}

    assert diagnostics["requires_follow_up"] is True
    assert "all_comparison_values_zero" in signal_codes
    assert "identical_measure_columns" in signal_codes
    assert diagnostics["identical_column_pairs"] == [
        ["scenario_a", "scenario_b"]
    ]


def test_result_diagnostics_flags_low_sample_only_for_multi_observation_intent() -> None:
    comparison = _profile_query_result(
        ["region", "sales"],
        [["A", 10.0]],
        question="Compare sales by region",
    )
    single_winner = _profile_query_result(
        ["customer", "sales"],
        [["A", 10.0]],
        question="Which customer has the highest sales?",
    )

    assert comparison["requires_follow_up"] is True
    assert any(
        signal["code"] == "low_sample" for signal in comparison["signals"]
    )
    assert single_winner["requires_follow_up"] is False

    one_row_equal_measures = _profile_query_result(
        ["CustomerID", "customer", "measure_a", "measure_b"],
        [[10, "A", 10.0, 10.0]],
        question="Which customer has the highest sales?",
    )
    assert "CustomerID" not in one_row_equal_measures["numeric_measure_columns"]
    assert one_row_equal_measures["identical_column_pairs"] == []
    assert one_row_equal_measures["requires_follow_up"] is False


@pytest.mark.asyncio
async def test_data_insight_automatically_continues_pending_diagnostic_in_same_session(
    monkeypatch,
) -> None:
    agent = DataInsightAgent.__new__(DataInsightAgent)
    agent.agent = SimpleNamespace()
    agent._recovery_state = ContextVar("automatic-diagnostic-test", default=None)
    session = object()
    calls: list[tuple[str, object]] = []

    async def fake_run_agent(_agent, message, *, session=None):
        calls.append((message, session))
        state = agent._recovery_state.get()
        assert state is not None
        if len(calls) == 1:
            state.pending_diagnostic = {
                "requires_follow_up": True,
                "signals": [{"code": "all_comparison_values_zero"}],
            }
            return SimpleNamespace(text="premature answer")
        state.pending_diagnostic = None
        state.diagnostic_attempts = 1
        return SimpleNamespace(text="diagnostic-grounded answer")

    monkeypatch.setattr(
        "src.agents.data_insight_agent.run_agent",
        fake_run_agent,
    )

    result = await agent.query(
        "Compare sales by region",
        thread=session,
        schema_context="verified schema",
        ontology_enabled=False,
    )

    assert result == "diagnostic-grounded answer"
    assert len(calls) == 2
    assert calls[0][1] is session and calls[1][1] is session
    assert "<mandatory_result_diagnostic>" in calls[1][0]
    assert "all_comparison_values_zero" in calls[1][0]


@pytest.mark.asyncio
async def test_data_insight_stream_automatically_continues_pending_diagnostic(
    monkeypatch,
) -> None:
    agent = DataInsightAgent.__new__(DataInsightAgent)
    agent.agent = SimpleNamespace()
    agent._recovery_state = ContextVar("stream-diagnostic-test", default=None)
    session = object()
    calls: list[tuple[str, object]] = []

    def fake_stream_agent(_agent, message, *, session=None):
        calls.append((message, session))
        call_number = len(calls)

        async def updates():
            state = agent._recovery_state.get()
            assert state is not None
            if call_number == 1:
                state.pending_diagnostic = {
                    "requires_follow_up": True,
                    "signals": [{"code": "low_sample"}],
                }
                yield SimpleNamespace(text="premature stream", contents=[])
            else:
                state.pending_diagnostic = None
                state.diagnostic_attempts = 1
                yield SimpleNamespace(text="diagnostic stream", contents=[])

        return updates()

    monkeypatch.setattr(
        "src.agents.data_insight_agent.stream_agent",
        fake_stream_agent,
    )

    texts = [
        update.text
        async for update in agent.query_stream(
            "Compare sales by region",
            thread=session,
            schema_context="verified schema",
            ontology_enabled=False,
        )
    ]

    assert texts == ["premature stream", "diagnostic stream"]
    assert len(calls) == 2
    assert calls[0][1] is session and calls[1][1] is session
    assert "<mandatory_result_diagnostic>" in calls[1][0]


@pytest.mark.asyncio
async def test_data_insight_blocks_sql_until_dynamic_planning_skill_is_loaded(
    monkeypatch,
) -> None:
    monkeypatch.setattr(DatabricksConfig, "CATALOG", "catalog")
    monkeypatch.setattr(DatabricksConfig, "SCHEMAS", ["silver"])
    monkeypatch.setattr(DatabricksConfig, "SCHEMA", "silver")
    database_calls: list[tuple[str, int]] = []

    def run_query(sql: str, max_rows: int = 500):
        database_calls.append((sql, max_rows))
        return {
            "columns": ["value"],
            "rows": [[1]],
            "row_count": 1,
            "sql": sql,
        }

    monkeypatch.setattr(
        "src.agents.data_insight_agent._run_databricks_query",
        run_query,
    )
    agent = DataInsightAgent()
    execute_sql = {
        tool.__name__: tool for tool in agent._create_tools()
    }["execute_sql"]
    state, _ = agent._prepare_contextual_question(
        "monthly sales",
        schema_context="verified schema",
        ontology_context="",
        ontology_fallback="",
        ontology_enabled=False,
    )
    recovery_token = agent._recovery_state.set(state)
    usage_token = begin_skill_usage_tracking()
    try:
        blocked = execute_sql("SELECT 1")
        assert "Required Skill 'sql-planning'" in blocked
        assert database_calls == []

        provider = agent.agent.context_providers[0]
        _, _, skill_tools = await provider._create_context(
            SkillsSourceContext(
                agent=agent.agent,
                session=agent.agent.create_session(),
            )
        )
        load_skill = next(tool for tool in skill_tools if tool.name == "load_skill")
        await load_skill.invoke(
            arguments={"skill_name": "sql-planning"}
        )

        blocked_scope = execute_sql(
            "SELECT * FROM catalog.gold.factsales"
        )
        assert "Schema 'gold' is outside DATABRICKS_SCHEMAS (silver)" in blocked_scope
        assert database_calls == []

        result = execute_sql("SELECT 1")
        assert "Query returned 1 row(s)" in result
        assert '"requires_follow_up":true' in result

        blocked_diagnostic = execute_sql("SELECT 2")
        assert "purpose='diagnostic'" in blocked_diagnostic

        diagnostic = execute_sql("SELECT 2", purpose="diagnostic")
        assert '"diagnostic_completed":true' in diagnostic
        assert '"requires_follow_up":false' in diagnostic
        assert database_calls == [("SELECT 1", 100), ("SELECT 2", 100)]
    finally:
        reset_skill_usage_tracking(usage_token)
        agent._recovery_state.reset(recovery_token)


def test_sql_scope_validation_allows_ctes_but_rejects_unconfigured_tables(
    monkeypatch,
) -> None:
    from src.agents.data_insight_agent import _validate_sql_scope

    monkeypatch.setattr(DatabricksConfig, "CATALOG", "catalog")
    monkeypatch.setattr(DatabricksConfig, "SCHEMAS", ["silver"])

    valid_sql = (
        "WITH base AS (SELECT * FROM catalog.silver.salesorderheader) "
        "SELECT * FROM base"
    )

    assert _validate_sql_scope(valid_sql) is None
    assert "Schema 'gold'" in (
        _validate_sql_scope("SELECT * FROM catalog.gold.factsales") or ""
    )
    assert "outside the configured catalog" in (
        _validate_sql_scope("SELECT * FROM other.silver.orders") or ""
    )
    assert "fully-qualified" in (
        _validate_sql_scope("SELECT * FROM salesorderheader") or ""
    )


@pytest.mark.asyncio
async def test_governed_sql_requires_its_indexed_skill_resource(monkeypatch) -> None:
    database_calls: list[str] = []

    def run_query(sql: str, max_rows: int = 500):
        database_calls.append(sql)
        return {
            "columns": ["value"],
            "rows": [[1]],
            "row_count": 1,
            "sql": sql,
        }

    monkeypatch.setattr(
        "src.agents.data_insight_agent._run_databricks_query",
        run_query,
    )
    agent = DataInsightAgent()
    execute_sql = {
        tool.__name__: tool for tool in agent._create_tools()
    }["execute_sql"]
    governed = json.dumps(
        {
            "skill_name": "analytics-spec",
            "resource_name": "references/highest-spending-customer.sql",
        }
    )
    state, _ = agent._prepare_contextual_question(
        "top customer in 2023",
        schema_context="",
        ontology_context="",
        ontology_fallback="",
        ontology_enabled=True,
        governed_skill_context=governed,
    )
    recovery_token = agent._recovery_state.set(state)
    usage_token = begin_skill_usage_tracking()
    try:
        provider = agent.agent.context_providers[0]
        _, _, skill_tools = await provider._create_context(
            SkillsSourceContext(
                agent=agent.agent,
                session=agent.agent.create_session(),
            )
        )
        load_skill = next(tool for tool in skill_tools if tool.name == "load_skill")
        read_resource = next(
            tool for tool in skill_tools if tool.name == "read_skill_resource"
        )
        await load_skill.invoke(arguments={"skill_name": "analytics-spec"})

        blocked = execute_sql("SELECT 1")
        assert "Required Skill resource" in blocked
        assert database_calls == []

        await read_resource.invoke(
            arguments={
                "skill_name": "analytics-spec",
                "resource_name": "references/highest-spending-customer.sql",
            }
        )
        result = execute_sql("SELECT 1")
        assert "Query returned 1 row(s)" in result
        assert database_calls == ["SELECT 1"]
    finally:
        reset_skill_usage_tracking(usage_token)
        agent._recovery_state.reset(recovery_token)


def test_ontology_first_prompt_contracts_are_mandatory() -> None:
    ontology_prompt = " ".join(ONTOLOGY_AGENT_PROMPT.split())
    metadata_prompt = " ".join(METADATA_AGENT_PROMPT.split())
    insight_prompt = " ".join(DATA_INSIGHT_AGENT_PROMPT.split())

    assert "run before MetadataAgent" in ontology_prompt
    assert "does not require physical schema context" in ontology_prompt
    assert "Ontology Verification Mode" in metadata_prompt
    assert "only responsibility in this mode is physical verification" in metadata_prompt
    assert "Mandatory Ontology Review Before SQL" in insight_prompt
    assert "before every SQL draft and every retry" in insight_prompt
    assert "sql-planning" in insight_prompt
    assert "all_tool_results" in insight_prompt
    assert "formal-definition gap" in insight_prompt
    assert "operational-choice ambiguity" in insight_prompt
    assert "Choose the best-supported candidate" in insight_prompt
    assert "Do not refuse merely because multiple verified columns" in insight_prompt
    assert "A normalized name match is verified, not unresolved" in metadata_prompt
    assert "runner-up region" not in insight_prompt
    assert "average order value over detail-line rows" not in insight_prompt
    assert "Avoid SELECT *" not in insight_prompt
    assert "Aggregate order-header measures" not in insight_prompt


def test_ontology_tool_result_is_visible_as_bounded_activity_detail() -> None:
    fields = ontology_tool_result_fields(
        "get_business_context",
        json.dumps(
            {
                "status": "partial",
                "data": {
                    "root_entity": "SalesOrder",
                        "semantic_properties": [
                            {"name": "totalDue"},
                            {"name": "orderDate"},
                        ],
                        "semantic_relationships": [
                            {"relation": "placedBy", "entity": "Customer"}
                        ],
                    "join_paths": [{"semantic_path": ["Customer", "SalesOrder"]}],
                },
                "confidence": 0.91,
                "warnings": ["physical mapping requires UC verification"],
                "unresolved": ["join keys"],
            }
        ),
        max_detail_chars=2000,
    )

    assert fields["summary"] == "status=partial, root=SalesOrder, confidence=0.91"
    assert '"totalDue"' in fields["detail"]
    assert fields["metrics"] == {
        "match_count": 0,
        "path_count": 1,
            "semantic_property_count": 2,
            "semantic_relationship_count": 1,
        "warning_count": 1,
        "unresolved_count": 1,
    }


def test_deterministic_ontology_lookups_remain_visible_in_activity() -> None:
    calls: list[dict] = []

    class DeterministicOntologyAgent:
        async def query_stream(
            self,
            question,
            schema_context="",
            context_sink=None,
        ):
            business_context = {
                "status": "ok",
                "data": {
                    "root_entity": "ProductCategory",
                    "semantic_properties": [{"name": "categoryName"}],
                    "semantic_relationships": [
                        {"relation": "hasParentCategory"}
                    ],
                    "join_paths": [],
                    "schema_mapping": {},
                },
                "confidence": 0.91,
                "warnings": [],
                "unresolved": [],
            }
            defined_classes = {
                "status": "ok",
                "data": {
                    "defined_classes": [
                        {"name": "HighValueOrder"},
                        {"name": "DiscountedOrderLine"},
                    ]
                },
                "confidence": 1.0,
                "warnings": [],
                "unresolved": [],
            }
            if context_sink is not None:
                context_sink.extend(
                    [
                        {
                            "tool": "get_business_context",
                            "origin": "deterministic",
                            "arguments": {"question": question},
                            "result": business_context,
                        },
                        {
                            "tool": "list_defined_classes",
                            "origin": "deterministic",
                            "arguments": {},
                            "result": defined_classes,
                        },
                    ]
                )
            yield SimpleNamespace(text="", contents=[])

        @staticmethod
        def build_collected_context(agent_summary, tool_results):
            return OntologyAgent.build_collected_context(agent_summary, tool_results)

    master = MasterAgent.__new__(MasterAgent)
    master.search_agent = SimpleNamespace()
    master.agent_id = "deterministic-ontology-activity-test"
    master.ontology_agent = DeterministicOntologyAgent()
    master.metadata_agent = _MetadataAgent(calls)
    master.data_insight_agent = _DataInsightAgent(calls)
    event_queue = _EventQueue()
    turn = master._new_turn(
        "2023 category sales",
        stream_context=(event_queue, _ImmediateLoop()),
        enable_ontology=True,
    )
    context_var = master._turn_context_var()
    token = context_var.set(turn)
    pipeline = next(
        tool for tool in master._create_tools()
        if tool.__name__ == "delegate_data_analysis"
    )
    try:
        assert pipeline("2023 category sales").startswith("[STREAMED]")
    finally:
        context_var.reset(token)

    completed = [
        payload
        for event_type, payload in event_queue.items
        if event_type == "activity"
        and payload.get("agent") == "OntologyAgent"
        and payload.get("kind") == "tool"
        and payload.get("state") == "completed"
    ]
    by_name = {item["message"]: item for item in completed}
    assert set(by_name) == {
        "Build ontology business context",
        "List defined business classes",
    }
    assert "ProductCategory" in by_name["Build ontology business context"]["detail"]
    assert "HighValueOrder" in by_name["List defined business classes"]["detail"]
    assert by_name["Build ontology business context"]["metrics"][
        "semantic_property_count"
    ] == 1


@pytest.mark.asyncio
async def test_data_insight_recovers_missing_context_inside_its_loop() -> None:
    calls: list[dict] = []

    async def metadata_query(
        question,
        ontology_context="",
        require_metadata_mapping=False,
    ):
        calls.append(
            {
                "agent": "metadata",
                "question": question,
                "ontology_context": ontology_context,
                "require_metadata_mapping": require_metadata_mapping,
            }
        )
        return json.dumps(
            {
                "status": "ok",
                "all_tool_results": [
                    {
                        "tool": "get_table_details",
                        "result": {
                            "status": "ok",
                            "full_name": "catalog.silver.orders",
                            "columns": [{"name": "TotalDue"}],
                        },
                    }
                ],
            }
        )

    async def ontology_query(question, schema_context=""):
        calls.append(
            {
                "agent": "ontology",
                "question": question,
                "schema_context": schema_context,
            }
        )
        return json.dumps(
            {
                "status": "ok",
                "primary_business_context": {"root_entity": "SalesOrder"},
                "all_tool_results": [],
            }
        )

    agent = DataInsightAgent.__new__(DataInsightAgent)
    agent.metadata_agent = SimpleNamespace(query=metadata_query)
    agent.ontology_agent = SimpleNamespace(query=ontology_query)
    agent._recovery_state = ContextVar("data-recovery-test", default=None)
    tools = {tool.__name__: tool for tool in agent._create_tools()}
    state, full_question = agent._prepare_contextual_question(
        "monthly sales",
        schema_context="",
        ontology_context="",
        ontology_fallback="",
        ontology_enabled=True,
    )
    token = agent._recovery_state.set(state)
    try:
        metadata_result = json.loads(
            await tools["recover_metadata_context"](
                reason="Missing order table columns"
            )
        )
        ontology_result = json.loads(
            await tools["recover_ontology_context"](
                reason="Missing sales measure semantics"
            )
        )
        repeated_metadata = json.loads(
            await tools["recover_metadata_context"](
                reason="Try the same recovery again"
            )
        )
    finally:
        agent._recovery_state.reset(token)

    assert metadata_result["status"] == "ok"
    assert ontology_result["status"] == "ok"
    assert repeated_metadata["status"] == "exhausted"
    assert [call["agent"] for call in calls] == ["metadata", "ontology"]
    assert calls[0]["require_metadata_mapping"] is True
    assert "catalog.silver.orders" in calls[1]["schema_context"]
    assert "recover_metadata_context before SQL" in full_question
    assert "recover_ontology_context before SQL" in full_question


@pytest.mark.asyncio
async def test_data_insight_metadata_recovery_preserves_ready_ontology() -> None:
    calls: list[dict] = []
    ontology_context = json.dumps(
        {
            "status": "ok",
            "primary_business_context": {"root_entity": "SalesOrder"},
            "all_tool_results": [],
        }
    )

    async def metadata_query(
        question,
        ontology_context="",
        require_metadata_mapping=False,
    ):
        calls.append(
            {
                "question": question,
                "ontology_context": ontology_context,
                "require_metadata_mapping": require_metadata_mapping,
            }
        )
        return json.dumps(
            {
                "status": "ok",
                "all_tool_results": [
                    {
                        "tool": "get_table_details",
                        "result": {
                            "status": "ok",
                            "full_name": "catalog.silver.orders",
                            "columns": [{"name": "TotalDue"}],
                        },
                    }
                ],
            }
        )

    agent = DataInsightAgent.__new__(DataInsightAgent)
    agent.metadata_agent = SimpleNamespace(query=metadata_query)
    agent.ontology_agent = None
    agent._recovery_state = ContextVar("ontology-aware-metadata-recovery", default=None)
    tools = {tool.__name__: tool for tool in agent._create_tools()}
    state, _ = agent._prepare_contextual_question(
        "monthly sales",
        schema_context="",
        ontology_context=ontology_context,
        ontology_fallback="",
        ontology_enabled=True,
    )
    token = agent._recovery_state.set(state)
    try:
        result = json.loads(
            await tools["recover_metadata_context"](
                reason="Missing verified physical order table"
            )
        )
    finally:
        agent._recovery_state.reset(token)

    assert result["status"] == "ok"
    assert calls == [
        {
            "question": "monthly sales",
            "ontology_context": ontology_context,
            "require_metadata_mapping": False,
        }
    ]


@pytest.mark.asyncio
async def test_data_insight_cannot_recover_disabled_ontology() -> None:
    calls: list[str] = []

    async def ontology_query(question, schema_context=""):
        calls.append(question)
        return "unexpected"

    agent = DataInsightAgent.__new__(DataInsightAgent)
    agent.metadata_agent = None
    agent.ontology_agent = SimpleNamespace(query=ontology_query)
    agent._recovery_state = ContextVar("disabled-ontology-test", default=None)
    tools = {tool.__name__: tool for tool in agent._create_tools()}
    state, full_question = agent._prepare_contextual_question(
        "monthly sales",
        schema_context="schema details",
        ontology_context="",
        ontology_fallback="",
        ontology_enabled=False,
    )
    token = agent._recovery_state.set(state)
    try:
        result = json.loads(
            await tools["recover_ontology_context"](
                reason="No ontology context"
            )
        )
    finally:
        agent._recovery_state.reset(token)

    assert result["status"] == "disabled"
    assert calls == []
    assert "ontology=disabled" in full_question


def test_definition_provenance_is_declared_symmetrically() -> None:
    agent = DataInsightAgent.__new__(DataInsightAgent)
    agent._recovery_state = ContextVar("provenance-test", default=None)

    def build(**overrides: object) -> str:
        kwargs: dict[str, object] = {
            "schema_context": "schema details",
            "ontology_context": "",
            "ontology_fallback": "",
            "ontology_enabled": False,
        }
        kwargs.update(overrides)
        _, full_question = agent._prepare_contextual_question("monthly sales", **kwargs)
        return full_question

    # Both modes disclose provenance; only the source label differs.
    ontology_off = build()
    assert "governed_definitions=unavailable" in ontology_off
    assert "Skill: metadata-mapping" in ontology_off
    assert "本体 / Ontology" not in ontology_off

    ontology_on = build(
        ontology_enabled=True,
        ontology_context='{"primary_business_context": {}}',
    )
    assert "governed_definitions=available" in ontology_on
    assert "本体 / Ontology" in ontology_on
    # The verifier runs without the glossary, so it is not a claimable source.
    assert "Skill: metadata-mapping" not in ontology_on

    assert "governed_definitions=available" in build(
        ontology_enabled=True,
        ontology_fallback="OntologyAgent timed out",
    )
    governed = build(governed_skill_context='{"skill_name": "analytics-spec"}')
    assert "governed_definitions=skill_contract" in governed
    assert "Skill: analytics-spec" in governed
    assert "Skill: governed template" in build(governed_skill_context="not json")

    for rendered in (ontology_off, ontology_on, governed):
        assert "allowed_source_labels=" in rendered
        assert "Skill: sql-planning" in rendered or "Skill: analytics-spec" in rendered
        assert "推断 / Inferred" in rendered
        assert "用户指定 / User-stated" in rendered

    prompt = " ".join(DATA_INSIGHT_AGENT_PROMPT.split())
    assert "计算口径 / Definitions Used" in prompt
    assert "业务词 / 采用字段 / 粒度 / 来源" in prompt
    assert "copied verbatim from `allowed_source_labels`" in prompt
    # A paired label must survive a Chinese answer intact.
    assert "including both sides of a paired label" in prompt
    assert "the answer language governs the prose, never these labels" in prompt
    # Rules in the prompt are system defaults, never ontology or Skill evidence.
    assert "must never be attributed to the ontology or to a Skill" in prompt
    # A self-supplied threshold is neither user-stated nor a system default.
    assert (
        "applies only when the value, threshold, or definition appears in "
        "the user's own literal wording" in prompt
    )
    assert "is not 系统默认 either" in prompt
    # One term per row, with role/level choices split out.
    assert "Give each business term exactly one row" in prompt
    assert "an over-claimed source is worse than an honest 推断" in prompt
    # A procedure that says how to decide is not a source for what was decided.
    assert "A source counts only when it names the choice itself" in prompt
    assert "tells you how to decide, not what to decide" in prompt
    assert "MetadataAgent verified every physical column name in Unity Catalog" in prompt
    assert "the defensible alternative you did not use" in prompt
    # The provenance block must not turn into a warning that discredits the answer.
    assert "never a reason to refuse, to ask instead of executing, to hedge the numbers" in prompt
    assert "without a generic reliability warning" in prompt
    # Definitions are audited after the conclusions, not before them.
    assert prompt.index("洞察 / Insights") < prompt.index("计算口径 / Definitions Used")
    assert prompt.index("计算口径 / Definitions Used") < prompt.index(
        "建议与下一步 / Recommendations & Next Steps"
    )
    # Sections must be separated and hierarchical rather than one dense block.
    assert "`###` markdown heading on its own line" in prompt
    assert "blank line before the next one" in prompt
    assert "Indent nested items by two spaces" in prompt


def _recall_catalog():
    class Catalog:
        def __init__(self) -> None:
            self.detail_calls: list[list[str]] = []

        def list_tables(self, *, schema: str, catalog: str):
            return ([
                {
                    "name": "salesorderheader",
                    "full_name": f"{catalog}.{schema}.salesorderheader",
                    "schema": schema,
                    "comment": "sales order header",
                },
                {
                    "name": "salesaddress",
                    "full_name": f"{catalog}.{schema}.salesaddress",
                    "schema": schema,
                    "comment": "address",
                },
                {
                    "name": "salesproduct",
                    "full_name": f"{catalog}.{schema}.salesproduct",
                    "schema": schema,
                    "comment": "product",
                },
            ], True)

        def _detail(self, full_name: str):
            bare = full_name.rsplit(".", 1)[-1]
            columns = {
                "salesorderheader": [{"name": "TotalDue"}, {"name": "ShipToAddressID"}],
                "salesaddress": [{"name": "AddressID"}, {"name": "StateProvince"}],
                "salesproduct": [{"name": "ProductID"}, {"name": "Color"}],
            }[bare]
            return {
                "name": bare,
                "full_name": full_name,
                "schema": DatabricksConfig.SCHEMAS[0],
                "columns": columns,
            }

        def get_table(self, table_name: str, *, catalog: str = "", schema: str = ""):
            full_name = (
                table_name
                if table_name.count(".") == 2
                else f"{catalog or DatabricksConfig.CATALOG}."
                f"{schema or DatabricksConfig.SCHEMAS[0]}.{table_name}"
            )
            return self._detail(full_name), True

        def get_tables_details(self, table_names, *, catalog: str = "", schema: str = "", **_):
            names = [name for name in dict.fromkeys(table_names) if name]
            self.detail_calls.append(names)
            return [self._detail(name) for name in names], len(names)

        def cached_table_details(self):
            return []

    return Catalog()


def test_metadata_recall_selects_candidates_and_closes_joins() -> None:
    agent = MetadataAgent.__new__(MetadataAgent)
    agent.catalog_service = _recall_catalog()
    schema = DatabricksConfig.SCHEMAS[0]
    catalog = DatabricksConfig.CATALOG

    tool_results: list = []
    recall = agent.prefetch_candidate_schema(
        tool_results,
        question="compare sales by region",
        ontology_context=json.dumps(
            {
                "primary_business_context": {
                    "status": "ok",
                    "data": {
                        "root_entity": "SalesOrder",
                        "semantic_properties": [
                            {
                                "name": "totalDue",
                                "domain": [{"name": "SalesOrder"}],
                                "evidence": "question_match",
                            }
                        ],
                        "schema_mapping": {
                            "candidate_tables": [{"name": "salesorderheader"}],
                            "candidate_columns": [],
                        },
                    },
                }
            }
        ),
    )

    selected = {name.rsplit(".", 1)[-1] for name in recall["selected"]}
    # salesorderheader is recalled; salesaddress arrives only through ShipToAddressID closure.
    assert "salesorderheader" in selected
    assert "salesaddress" in selected
    assert "salesproduct" not in selected
    assert recall["total"] == 3

    listing = next(item for item in tool_results if item["tool"] == "list_tables")
    assert listing["origin"] == "deterministic"
    assert listing["result"]["selection_basis"] == "recall"
    assert f"{catalog}.{schema}.salesaddress" in listing["result"]["join_closure_tables"]


def test_ontology_verification_supplements_omitted_table_details() -> None:
    agent = MetadataAgent.__new__(MetadataAgent)
    agent.catalog_service = _recall_catalog()
    schema = DatabricksConfig.SCHEMAS[0]
    tool_results = [{
        "tool": "get_table_details",
        "arguments": {"table_name": "salesorderheader"},
        "result": {
            "status": "ok",
            "full_name": (
                f"{DatabricksConfig.CATALOG}.{schema}.salesorderheader"
            ),
        },
    }]

    # Without a prior recall the backstop still completes a small schema.
    assert agent.supplement_schema_snapshot(tool_results) == 2
    assert {
        item["result"]["full_name"].rsplit(".", 1)[-1]
        for item in tool_results
        if item["tool"] == "get_table_details"
    } == {"salesorderheader", "salesaddress", "salesproduct"}
    assert tool_results[-1]["result"]["source"] == "deterministic_schema_snapshot"


def test_supplement_is_scoped_to_the_recalled_candidates() -> None:
    agent = MetadataAgent.__new__(MetadataAgent)
    agent.catalog_service = _recall_catalog()
    schema = DatabricksConfig.SCHEMAS[0]
    catalog = DatabricksConfig.CATALOG
    tool_results = [
        {
            "tool": "list_tables",
            "origin": "deterministic",
            "arguments": {},
            "result": {
                "status": "ok",
                "selected_tables": [
                    f"{catalog}.{schema}.salesorderheader",
                    f"{catalog}.{schema}.salesaddress",
                ],
            },
        },
        {
            "tool": "get_table_details",
            "arguments": {"table_name": "salesorderheader"},
            "result": {
                "status": "ok",
                "full_name": f"{catalog}.{schema}.salesorderheader",
            },
        },
    ]

    # The backstop completes the recalled set only; it must not reload the whole schema.
    assert agent.supplement_schema_snapshot(tool_results) == 1
    assert {
        item["result"]["full_name"].rsplit(".", 1)[-1]
        for item in tool_results
        if item["tool"] == "get_table_details"
    } == {"salesorderheader", "salesaddress"}


def test_measures_used_is_extracted_from_the_executed_sql() -> None:
    measures = _extract_sql_measures(
        """
        SELECT a.StateProvince AS region,
               COUNT(DISTINCT h.SalesOrderID) AS order_count,
               SUM(d.OrderQty) AS units_sold,
               SUM(h.TotalDue) AS sales_amount,
               SUM(d.LineTotal) / SUM(d.OrderQty) AS revenue_per_unit
        FROM cat.silver.salesorderheader h
        JOIN cat.silver.salesaddress a ON h.ShipToAddressID = a.AddressID
        JOIN cat.silver.salesorderdetail d ON h.SalesOrderID = d.SalesOrderID
        WHERE YEAR(h.OrderDate) = 2023
        GROUP BY a.StateProvince
        """
    )

    # Two amount bases in one query is exactly the mismatch this block must expose.
    assert "SUM(TotalDue)" in measures["aggregates"]
    assert "SUM(LineTotal)" in measures["aggregates"]
    assert "COUNT(DISTINCT SalesOrderID)" in measures["aggregates"]
    assert measures["group_by"] == ["StateProvince"]
    assert measures["filter_columns"] == ["OrderDate"]
    assert "silver.salesorderheader" in measures["tables"]

    # CTE names are not physical tables.
    cte_measures = _extract_sql_measures(
        "WITH base AS (SELECT LineTotal FROM cat.silver.salesorderdetail) "
        "SELECT SUM(LineTotal) AS amt FROM base"
    )
    assert cte_measures["tables"] == ["silver.salesorderdetail"]

    # A SQL alias must resolve back to the real column it derives from.
    derived = _extract_sql_measures(
        "WITH chain AS ("
        " SELECT ProductCategoryID AS leaf_id, ParentProductCategoryID AS parent_id,"
        " Name AS node_name FROM cat.silver.salesproductcategory),"
        " top AS (SELECT leaf_id, node_name AS top_category FROM chain WHERE parent_id IS NULL)"
        " SELECT t.top_category, SUM(d.OrderQty) AS units"
        " FROM cat.silver.salesorderdetail d JOIN top t ON d.ProductID = t.leaf_id"
        " GROUP BY t.top_category"
    )
    assert derived["group_by"] == ["top_category"]
    assert derived["derived_names"]["top_category"] == ["Name"]
    assert derived["derived_names"]["parent_id"] == ["ParentProductCategoryID"]
    # A real column is never reported as derived.
    assert "OrderQty" not in derived["derived_names"]

    assert _extract_sql_measures("SELECT ((") == {}

    prompt = " ".join(DATA_INSIGHT_AGENT_PROMPT.split())
    assert "<measures_used>" in prompt
    assert "must not silently mix two different amount bases" in prompt
    assert "is a SQL alias, not a physical column" in prompt


@pytest.mark.asyncio
async def test_data_insight_does_not_retry_known_ontology_failure() -> None:
    calls: list[str] = []

    async def ontology_query(question, schema_context=""):
        calls.append(question)
        return "unexpected"

    agent = DataInsightAgent.__new__(DataInsightAgent)
    agent.metadata_agent = None
    agent.ontology_agent = SimpleNamespace(query=ontology_query)
    agent._recovery_state = ContextVar("failed-ontology-test", default=None)
    tools = {tool.__name__: tool for tool in agent._create_tools()}
    state, _ = agent._prepare_contextual_question(
        "monthly sales",
        schema_context="schema details",
        ontology_context="",
        ontology_fallback="OntologyAgent timed out",
        ontology_enabled=True,
    )
    token = agent._recovery_state.set(state)
    try:
        result = json.loads(
            await tools["recover_ontology_context"](
                reason="No ontology context"
            )
        )
    finally:
        agent._recovery_state.reset(token)

    assert result["status"] == "upstream_failed"
    assert calls == []


def test_metadata_context_preserves_selected_table_details() -> None:
    agent = MetadataAgent.__new__(MetadataAgent)
    agent._tool_result_sink = ContextVar("metadata-test-sink", default=None)
    payload = {
        "status": "ok",
        "full_name": "catalog.silver.orders",
        "comment": "Question-relevant order facts",
        "columns": [
            {
                "name": "OrderDate",
                "type": "DATE",
                "nullable": False,
                "comment": "Order creation date",
            },
            {
                "name": "TotalDue",
                "type": "DECIMAL",
                "nullable": False,
                "comment": "Order total",
            },
        ],
        "cache_hit": False,
    }
    sink: list[dict] = []
    token = agent._tool_result_sink.set(sink)
    try:
        agent._tool_json(
            "get_table_details",
            {"table_name": "orders"},
            payload,
        )
    finally:
        agent._tool_result_sink.reset(token)

    context = json.loads(agent.build_collected_context("orders summary", sink))

    assert context["all_tool_results"][0]["result"] == payload
    assert context["agent_summary"] == "orders summary"


def test_metadata_tools_enforce_configured_schema_allowlist(monkeypatch) -> None:
    monkeypatch.setattr(DatabricksConfig, "CATALOG", "catalog")
    monkeypatch.setattr(DatabricksConfig, "SCHEMAS", ["silver"])
    monkeypatch.setattr(DatabricksConfig, "SCHEMA", "silver")
    calls: list[tuple[str, str, str]] = []

    class CatalogService:
        @staticmethod
        def list_schemas(*, catalog: str):
            calls.append(("list_schemas", catalog, ""))
            return ["gold", "silver", "system"], False

        @staticmethod
        def list_tables(*, catalog: str, schema: str):
            calls.append(("list_tables", catalog, schema))
            return [], False

        @staticmethod
        def search_tables(keyword: str, *, catalog: str, schema: str):
            calls.append(("search_tables", catalog, schema))
            return [], False

        @staticmethod
        def get_table(table_name: str, *, catalog: str, schema: str):
            calls.append(("get_table", catalog, schema))
            return None, False

    agent = MetadataAgent.__new__(MetadataAgent)
    agent.catalog_service = CatalogService()
    agent._tool_result_sink = ContextVar("metadata-allowlist-test", default=None)
    tools = {tool.__name__: tool for tool in agent._create_tools()}

    rejected_search = json.loads(
        tools["search_tables"]("sales", schema="gold")
    )
    rejected_list = json.loads(tools["list_tables"](schema="gold"))
    rejected_detail = json.loads(
        tools["get_table_details"]("catalog.gold.factsales")
    )

    assert rejected_search["status"] == "out_of_scope"
    assert rejected_list["status"] == "out_of_scope"
    assert rejected_detail["status"] == "out_of_scope"
    assert rejected_search["configured_schemas"] == ["silver"]
    assert calls == []

    visible_schemas = json.loads(tools["list_schemas"]())
    default_search = json.loads(tools["search_tables"]("sales"))

    assert visible_schemas["schemas"] == ["silver"]
    assert default_search["schemas_searched"] == ["silver"]
    assert calls == [
        ("list_schemas", "catalog", ""),
        ("search_tables", "catalog", "silver"),
    ]


def test_metadata_builds_bounded_ontology_verification_context() -> None:
    ontology_context = json.dumps(
        {
            "status": "ok",
            "primary_business_context": {
                "status": "partial",
                "data": {
                    "root_entity": "SalesOrder",
                    "filters": [{"field": "date", "value": "2023"}],
                    "semantic_properties": [
                        {
                            "name": "totalDue",
                            "domain": ["SalesOrder"],
                            "range": ["decimal"],
                        },
                        {
                            "name": "orderDate",
                            "domain": ["SalesOrder"],
                            "range": ["date"],
                        },
                    ],
                    "semantic_relationships": [{"entity": "Customer"}],
                    "join_paths": [
                        {
                            "semantic_path": [
                                "Customer",
                                "placedOrder",
                                "SalesOrder",
                            ],
                            "requires_metadata_resolution": True,
                        }
                    ],
                    "schema_mapping": {
                        "candidate_tables": [{"name": "sales_order"}],
                        "candidate_columns": [
                            {"property": "totalDue", "candidates": ["total_due"]}
                        ],
                    },
                    "requires_metadata_resolution": True,
                },
                "warnings": ["UC verification required"],
                "unresolved": ["physical join keys"],
            },
            "all_tool_results": [{"large_raw_payload": "must not be projected"}],
        }
    )

    projected = json.loads(
        MetadataAgent.build_ontology_verification_context(ontology_context)
    )

    assert projected["root_entity"] == "SalesOrder"
    groups = {
        group["entity"]: group
        for group in projected["semantic_property_groups"]
    }
    assert groups["SalesOrder"]["properties"][0]["name"] == "totalDue"
    assert projected["required_relations"] == [
        {
            "source": "Customer",
            "relation": "placedOrder",
            "target": "SalesOrder",
        }
    ]
    assert "measures" not in projected
    assert "dimensions" not in projected
    assert projected["candidate_tables"] == [{"name": "sales_order"}]
    assert projected["join_paths"][0]["requires_metadata_resolution"] is True
    assert projected["unresolved"] == ["physical join keys"]
    assert "all_tool_results" not in projected


def test_metadata_projects_properties_by_required_path_entity_without_global_cutoff() -> None:
    semantic_properties = [
        {
            "name": f"salesOrderProperty{index}",
            "entity": "SalesOrder",
            "domain": ["SalesOrder"],
            "range": ["str"],
        }
        for index in range(25)
    ]
    semantic_properties.append(
        {
            "name": "stateProvince",
            "entity": "Address",
            "domain": ["Address"],
            "range": ["str"],
        }
    )
    ontology_context = json.dumps(
        {
            "status": "ok",
            "primary_business_context": {
                "status": "partial",
                "data": {
                    "root_entity": "SalesOrder",
                    "semantic_properties": semantic_properties,
                    "join_paths": [
                        {
                            "semantic_path": [
                                "SalesOrder",
                                "hasShipToAddress",
                                "Address",
                            ],
                            "requires_metadata_resolution": True,
                        }
                    ],
                    "schema_mapping": {},
                },
            },
        }
    )

    projected = json.loads(
        MetadataAgent.build_ontology_verification_context(ontology_context)
    )
    groups = {
        group["entity"]: group
        for group in projected["semantic_property_groups"]
    }

    assert len(groups["SalesOrder"]["properties"]) == 25
    assert [
        prop["name"] for prop in groups["Address"]["properties"]
    ] == ["stateProvince"]
    assert projected["required_relations"] == [
        {
            "source": "SalesOrder",
            "relation": "hasShipToAddress",
            "target": "Address",
        }
    ]


def test_metadata_projects_recursive_hierarchy_as_a_required_relation() -> None:
    ontology_context = json.dumps(
        {
            "status": "ok",
            "primary_business_context": {
                "status": "partial",
                "data": {
                    "root_entity": "ProductCategory",
                    "semantic_properties": [
                        {
                            "name": "categoryName",
                            "entity": "ProductCategory",
                            "domain": ["ProductCategory"],
                            "range": ["str"],
                        }
                    ],
                    "hierarchy_relations": [
                        {
                            "entity": "ProductCategory",
                            "relation": "hasParentCategory",
                            "recursive": True,
                        }
                    ],
                    "join_paths": [],
                    "schema_mapping": {},
                },
            },
        }
    )

    projected = json.loads(
        MetadataAgent.build_ontology_verification_context(ontology_context)
    )

    assert {
        "source": "ProductCategory",
        "relation": "hasParentCategory",
        "target": "ProductCategory",
        "recursive": True,
    } in projected["required_relations"]
    assert "recursive: true" in METADATA_AGENT_PROMPT


def test_metadata_modes_prepare_distinct_skill_contexts() -> None:
    discovery = MetadataAgent._prepare_contextual_question(
        "monthly sales",
        ontology_context="",
        require_metadata_mapping=True,
    )
    verifier = MetadataAgent._prepare_contextual_question(
        "monthly sales",
        ontology_context=json.dumps(
            {
                "status": "ok",
                "primary_business_context": {"root_entity": "SalesOrder"},
            }
        ),
        require_metadata_mapping=False,
    )

    assert "required_skill=metadata-mapping" in discovery
    assert "<ontology_verification_context>" not in discovery
    assert "<ontology_verification_context>" in verifier
    assert "required_skill=metadata-mapping" not in verifier


@pytest.mark.asyncio
async def test_data_insight_governed_skill_skips_context_recovery() -> None:
    governed = json.dumps(
        {
            "skill_name": "analytics-spec",
            "resource_name": "references/highest-spending-customer.sql",
        }
    )
    agent = DataInsightAgent.__new__(DataInsightAgent)
    agent.metadata_agent = None
    agent.ontology_agent = None
    agent._recovery_state = ContextVar("governed-skill-recovery", default=None)
    tools = {tool.__name__: tool for tool in agent._create_tools()}
    state, full_question = agent._prepare_contextual_question(
        "top customer in 2023",
        schema_context="",
        ontology_context="",
        ontology_fallback="",
        ontology_enabled=True,
        governed_skill_context=governed,
    )
    token = agent._recovery_state.set(state)
    try:
        metadata_result = json.loads(
            await tools["recover_metadata_context"](reason="should not run")
        )
        ontology_result = json.loads(
            await tools["recover_ontology_context"](reason="should not run")
        )
    finally:
        agent._recovery_state.reset(token)

    assert metadata_result["status"] == "not_required"
    assert ontology_result["status"] == "not_required"
    assert "schema=governed_skill" in full_question
    assert "ontology=skipped_by_skill" in full_question
    assert "recover_metadata_context before SQL" not in full_question
    assert "recover_ontology_context before SQL" not in full_question


def test_ontology_context_preserves_every_raw_tool_payload() -> None:
    agent = OntologyAgent.__new__(OntologyAgent)
    agent.ontology_service = SimpleNamespace()
    agent._tool_result_sink = ContextVar("ontology-test-sink", default=None)
    tool_names = [tool.__name__ for tool in agent._create_tools()]
    payload = {
        "status": "ok",
        "data": {
            "root_entity": "SalesOrder",
            "aliases": ["销售订单", "order"],
            "descriptions": ["Complete business description"],
            "relationships": [{"predicate": "hasCustomer", "target": "Customer"}],
            "lineage": {"upstream": ["Lead"], "downstream": ["Invoice"]},
            "evidence": [{"source": "aw_ontology.owl", "iri": "urn:SalesOrder"}],
        },
        "confidence": 0.91,
    }
    sink: list[dict] = []
    token = agent._tool_result_sink.set(sink)
    try:
        agent._tool_json(
            "get_business_context",
            {"question": "sales by customer"},
            payload,
        )
    finally:
        agent._tool_result_sink.reset(token)

    context = json.loads(agent.build_collected_context("short summary", sink))

    assert context["primary_business_context"] == payload
    assert context["primary_tool_call"] == {
        "tool": "get_business_context",
        "arguments": {"question": "sales by customer"},
    }
    assert context["additional_tool_results"] == []
    assert context["semantic_summary"]["root_entity"] == "SalesOrder"
    assert context["primary_business_context"]["data"]["lineage"] == {
        "upstream": ["Lead"],
        "downstream": ["Invoice"],
    }
    assert "lineage" not in context["semantic_summary"]
    assert "agent_summary" not in context
    assert "get_business_context" in tool_names
    assert "get_semantic_candidates" in tool_names
    assert "suggest_analysis_factors" not in tool_names
    assert "execute_sql" not in tool_names
    assert "get_relevant_tables" not in tool_names


def test_master_exposes_one_agentic_data_pipeline() -> None:
    data_calls: list[dict] = []
    metadata_calls: list[dict] = []
    master = MasterAgent.__new__(MasterAgent)
    master.search_agent = SimpleNamespace()
    master.agent_id = "master-test"
    master.metadata_agent = _MetadataAgent(metadata_calls)
    master.data_insight_agent = _DataInsightAgent(data_calls)
    master._current_user_message = "monthly sales"
    event_queue = _EventQueue()
    turn = master._new_turn(
        "monthly sales",
        stream_context=(event_queue, _ImmediateLoop()),
        enable_ontology=False,
    )
    context_var = master._turn_context_var()
    token = context_var.set(turn)

    tools = master._create_tools()
    tool_names = [tool.__name__ for tool in tools]

    assert "delegate_data_analysis" in tool_names
    assert "delegate_data_insight" not in tool_names

    pipeline = next(tool for tool in tools if tool.__name__ == "delegate_data_analysis")
    try:
        result = pipeline("monthly sales")
    finally:
        context_var.reset(token)

    assert result.startswith("[STREAMED]")
    assert [call["agent"] for call in metadata_calls] == ["metadata"]
    assert metadata_calls[0]["require_metadata_mapping"] is True
    assert len(data_calls) == 1
    assert data_calls[0]["question"].startswith("<original_user_question>")
    assert "salesorderheader" in data_calls[0]["schema_context"]
    assert data_calls[0]["ontology_context"] == ""
    assert data_calls[0]["ontology_enabled"] is False

    activities = [payload for event_type, payload in event_queue.items if event_type == "activity"]
    pipeline_activity = next(item for item in activities if item["category"] == "pipeline")
    metadata_started = next(
        index for index, item in enumerate(activities)
        if item["message"] == "MetadataAgent" and item["state"] == "running"
    )
    metadata_completed = next(
        index for index, item in enumerate(activities)
        if item["message"] == "MetadataAgent" and item["state"] == "completed"
    )
    insight_started = next(
        index for index, item in enumerate(activities)
        if item["message"] == "DataInsightAgent" and item["state"] == "running"
    )
    metadata_activity = activities[metadata_started]
    insight_activity = activities[insight_started]

    assert metadata_started < metadata_completed < insight_started
    assert metadata_activity["parent_id"] == pipeline_activity["id"]
    assert insight_activity["parent_id"] == pipeline_activity["id"]


def test_ontology_pipeline_runs_agents_in_strict_sequence() -> None:
    calls: list[dict] = []
    ontology_json = (
        '{"root_entity":"SalesOrder","filters":[],"selected_factors":'
        '["totalDue"],"join_paths":[],"schema_mapping":{},"lineage":{},'
        '"entity_candidates":[],"confidence":1.0,"evidence":[],'
        '"constraints":[],"warnings":[],"unresolved":[]}'
    )
    master = MasterAgent.__new__(MasterAgent)
    master.search_agent = SimpleNamespace()
    master.agent_id = "master-ontology-test"
    master.ontology_agent = _OntologyAgent(calls, ontology_json)
    master.metadata_agent = _MetadataAgent(calls)
    master.data_insight_agent = _DataInsightAgent(calls)
    event_queue = _EventQueue()
    turn = master._new_turn(
        "monthly sales",
        stream_context=(event_queue, _ImmediateLoop()),
        enable_ontology=True,
    )
    context_var = master._turn_context_var()
    token = context_var.set(turn)
    pipeline = next(
        tool for tool in master._create_tools()
        if tool.__name__ == "delegate_data_analysis"
    )
    try:
        result = pipeline("monthly sales")
    finally:
        context_var.reset(token)

    assert result.startswith("[STREAMED]")
    assert [call["agent"] for call in calls] == ["ontology", "metadata", "data"]
    assert calls[0]["schema_context"] == ""
    assert calls[1]["ontology_context"] == calls[2]["ontology_context"]
    assert calls[1]["require_metadata_mapping"] is False
    assert '"root_entity": "SalesOrder"' in calls[1]["ontology_context"]
    assert '"root_entity": "SalesOrder"' in calls[2]["ontology_context"]
    assert '"primary_tool_call"' in calls[2]["ontology_context"]
    assert '"additional_tool_results"' in calls[2]["ontology_context"]
    assert "salesorderheader" in calls[2]["schema_context"]
    assert calls[2]["ontology_fallback"] == ""
    assert calls[2]["ontology_enabled"] is True

    activities = [payload for event_type, payload in event_queue.items if event_type == "activity"]
    metadata_completed = next(
        index for index, item in enumerate(activities)
        if item["message"] == "MetadataAgent" and item["state"] == "completed"
    )
    ontology_completed = next(
        index for index, item in enumerate(activities)
        if item["message"] == "OntologyAgent" and item["state"] == "completed"
    )
    data_started = next(
        index for index, item in enumerate(activities)
        if item["message"] == "DataInsightAgent" and item["state"] == "running"
    )
    assert ontology_completed < metadata_completed < data_started


def test_ontology_governed_skill_route_skips_metadata() -> None:
    calls: list[dict] = []
    master = MasterAgent.__new__(MasterAgent)
    master.search_agent = SimpleNamespace()
    master.agent_id = "master-ontology-skill-test"
    master.ontology_agent = _SkillRoutingOntologyAgent()
    master.metadata_agent = _MetadataAgent(calls)
    master.data_insight_agent = _DataInsightAgent(calls)
    event_queue = _EventQueue()
    turn = master._new_turn(
        "哪个客户在2023年的消费是最高的",
        stream_context=(event_queue, _ImmediateLoop()),
        enable_ontology=True,
    )
    context_var = master._turn_context_var()
    token = context_var.set(turn)
    pipeline = next(
        tool for tool in master._create_tools()
        if tool.__name__ == "delegate_data_analysis"
    )
    try:
        result = pipeline("哪个客户在2023年的消费是最高的")
    finally:
        context_var.reset(token)

    assert result.startswith("[STREAMED]")
    assert [call["agent"] for call in calls] == ["data"]
    assert calls[0]["schema_context"] == ""
    assert calls[0]["ontology_context"] == ""
    governed = json.loads(calls[0]["governed_skill_context"])
    assert governed["skill_name"] == "analytics-spec"
    assert governed["resource_name"] == "references/highest-spending-customer.sql"

    pipeline_completed = next(
        payload for event_type, payload in event_queue.items
        if event_type == "activity"
        and payload.get("category") == "pipeline"
        and payload.get("state") == "completed"
    )
    assert pipeline_completed["metrics"]["skill_fast_path"] is True


def test_ontology_failure_is_visible_and_falls_back_to_standard_pipeline() -> None:
    calls: list[dict] = []
    master = MasterAgent.__new__(MasterAgent)
    master.search_agent = SimpleNamespace()
    master.agent_id = "master-fallback-test"
    master.ontology_agent = _OntologyAgent(calls, "not valid JSON")
    master.metadata_agent = _MetadataAgent(calls)
    master.data_insight_agent = _DataInsightAgent(calls)
    event_queue = _EventQueue()
    turn = master._new_turn(
        "monthly sales",
        stream_context=(event_queue, _ImmediateLoop()),
        enable_ontology=True,
    )
    context_var = master._turn_context_var()
    token = context_var.set(turn)
    pipeline = next(
        tool for tool in master._create_tools()
        if tool.__name__ == "delegate_data_analysis"
    )
    try:
        result = pipeline("monthly sales")
    finally:
        context_var.reset(token)

    assert result.startswith("[STREAMED]")
    assert [call["agent"] for call in calls] == ["ontology", "metadata", "data"]
    assert calls[1]["require_metadata_mapping"] is True
    assert calls[2]["ontology_context"] == ""
    assert "no usable business context" in calls[2]["ontology_fallback"]
    assert calls[2]["ontology_enabled"] is True

    fallback_activity = next(
        payload for event_type, payload in event_queue.items
        if event_type == "activity" and payload.get("category") == "fallback"
    )
    assert fallback_activity["state"] == "completed"
    assert "continuing with standard" in fallback_activity["message"]
    pipeline_outcome = next(
        outcome for outcome in turn.tool_outcomes
        if outcome.name == "delegate_data_analysis"
    )
    assert pipeline_outcome.metadata == {
        "ontology_requested": True,
        "ontology_applied": False,
        "fallback_used": True,
            "skill_fast_path": False,
    }


def test_unavailable_exception_and_empty_ontology_results_all_fall_back() -> None:
    scenarios = [
        (None, "not available"),
        (_RaisingOntologyAgent(), "ontology query exploded"),
        (_OntologyAgent([], ""), "no usable business context"),
    ]

    for index, (ontology_agent, expected_reason) in enumerate(scenarios):
        calls: list[dict] = []
        if isinstance(ontology_agent, _OntologyAgent):
            ontology_agent.calls = calls
        master = MasterAgent.__new__(MasterAgent)
        master.search_agent = SimpleNamespace()
        master.agent_id = f"master-fallback-{index}"
        master.ontology_agent = ontology_agent
        master.metadata_agent = _MetadataAgent(calls)
        master.data_insight_agent = _DataInsightAgent(calls)
        event_queue = _EventQueue()
        turn = master._new_turn(
            "monthly sales",
            stream_context=(event_queue, _ImmediateLoop()),
            enable_ontology=True,
        )
        context_var = master._turn_context_var()
        token = context_var.set(turn)
        pipeline = next(
            tool for tool in master._create_tools()
            if tool.__name__ == "delegate_data_analysis"
        )
        try:
            result = pipeline("monthly sales")
        finally:
            context_var.reset(token)

        assert result.startswith("[STREAMED]")
        data_call = next(call for call in calls if call["agent"] == "data")
        assert expected_reason in data_call["ontology_fallback"]
        fallback_activity = next(
            payload for event_type, payload in event_queue.items
            if event_type == "activity" and payload.get("category") == "fallback"
        )
        assert fallback_activity["state"] == "completed"


def test_metadata_failure_after_ontology_stops_before_data_insight() -> None:
    calls: list[dict] = []
    ontology_json = (
        '{"root_entity":"SalesOrder","filters":[],"selected_factors":'
        '["totalDue"],"join_paths":[],"schema_mapping":{},"lineage":{},'
        '"entity_candidates":[],"confidence":1.0,"evidence":[],'
        '"constraints":[],"warnings":[],"unresolved":[]}'
    )
    master = MasterAgent.__new__(MasterAgent)
    master.search_agent = SimpleNamespace()
    master.agent_id = "master-metadata-failure-test"
    master.ontology_agent = _OntologyAgent(calls, ontology_json)
    master.metadata_agent = _RaisingMetadataAgent(calls)
    master.data_insight_agent = _DataInsightAgent(calls)
    event_queue = _EventQueue()
    turn = master._new_turn(
        "monthly sales",
        stream_context=(event_queue, _ImmediateLoop()),
        enable_ontology=True,
    )
    context_var = master._turn_context_var()
    token = context_var.set(turn)
    pipeline = next(
        tool for tool in master._create_tools()
        if tool.__name__ == "delegate_data_analysis"
    )
    try:
        result = pipeline("monthly sales")
    finally:
        context_var.reset(token)

    assert result == "MetadataAgent error: unity catalog verification exploded"
    assert [call["agent"] for call in calls] == ["ontology", "metadata"]
    assert '"root_entity": "SalesOrder"' in calls[1]["ontology_context"]
    pipeline_activity = next(
        payload for event_type, payload in event_queue.items
        if event_type == "activity" and payload.get("category") == "pipeline"
        and payload.get("state") == "error"
    )
    assert "physical verification failed" in pipeline_activity["summary"]


def test_ontology_cancellation_stops_without_fallback() -> None:
    calls: list[dict] = []
    cancel_event = Event()
    master = MasterAgent.__new__(MasterAgent)
    master.search_agent = SimpleNamespace()
    master.agent_id = "master-cancel-test"
    master.ontology_agent = _CancellingOntologyAgent(cancel_event)
    master.metadata_agent = _MetadataAgent(calls)
    master.data_insight_agent = _DataInsightAgent(calls)
    event_queue = _EventQueue()
    turn = master._new_turn(
        "monthly sales",
        stream_context=(event_queue, _ImmediateLoop()),
        cancel_event=cancel_event,
        enable_ontology=True,
    )
    context_var = master._turn_context_var()
    token = context_var.set(turn)
    pipeline = next(
        tool for tool in master._create_tools()
        if tool.__name__ == "delegate_data_analysis"
    )
    try:
        result = pipeline("monthly sales")
    finally:
        context_var.reset(token)

    assert result == "Data-analysis pipeline cancelled by user."
    assert calls == []
    assert not any(
        event_type == "activity" and payload.get("category") == "fallback"
        for event_type, payload in event_queue.items
    )

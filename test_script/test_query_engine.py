"""MasterAgent query-engine regression tests."""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from src.agents.maf_runtime import create_chat_client
from src.agents.master_agent import MasterAgent
from src.config import AppConfig
from src.query_engine import QueryEngineContext


class _FakeResponseStream:
    def __init__(self) -> None:
        self._updates = [SimpleNamespace(text="Final answer", contents=[])]
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._updates):
            raise StopAsyncIteration
        update = self._updates[self._index]
        self._index += 1
        return update


def test_master_agent_uses_one_maf_run_per_user_turn() -> None:
    async def run_test() -> None:
        master = MasterAgent.__new__(MasterAgent)
        master.agent_id = "master-test"
        master.agent = object()
        calls: list[str] = []

        def fake_stream_agent(agent, message, session=None):
            calls.append(message)
            return _FakeResponseStream()

        with patch("src.agents.master_agent.stream_agent", side_effect=fake_stream_agent):
            updates = [
                update
                async for update in master.chat_stream(
                    "original question",
                    thread=object(),
                )
            ]

        assert len(calls) == 1
        assert "ontology_enabled=true" in calls[0]
        assert "<original_user_message>\noriginal question\n</original_user_message>" in calls[0]
        assert [update.text for update in updates] == ["Final answer"]

    asyncio.run(run_test())


def test_query_context_tracks_observable_search_attempts() -> None:
    context = QueryEngineContext(
        original_question="question",
        max_search_attempts=2,
    )
    context.record_tool("search_knowledge", success=False, summary="No results")
    context.record_tool("decompose_query", success=True, summary="Prepared queries")
    context.record_tool("search_multiple_queries", success=True, summary="Found evidence")

    assert context.search_attempts == 2
    assert len(context.tool_outcomes) == 3


def test_query_context_blocks_equivalent_query_sets() -> None:
    context = QueryEngineContext(
        original_question="question",
        max_search_attempts=5,
    )

    assert context.register_search_request(["GB 38031 safety", "Battery requirements"])
    assert not context.register_search_request(
        [" battery REQUIREMENTS ", "gb-38031 SAFETY"]
    )
    assert context.register_search_request(["GB 38031 applicability"])


def test_query_context_tracks_cross_attempt_evidence_gain() -> None:
    context = QueryEngineContext(
        original_question="question",
        max_search_attempts=5,
    )

    first = context.register_search_results(
        [{"id": "doc-1"}, {"filepath": "/standards/doc-2.pdf"}]
    )
    second = context.register_search_results(
        [{"id": "doc-1"}, {"id": "doc-3"}]
    )
    repeated = context.register_search_results(
        [{"id": "doc-1"}, {"id": "doc-3"}]
    )

    assert first == {
        "selected_count": 2,
        "new_unique_count": 2,
        "overlap_count": 0,
        "total_unique_count": 2,
        "no_new_evidence": False,
    }
    assert second["new_unique_count"] == 1
    assert second["overlap_count"] == 1
    assert second["total_unique_count"] == 3
    assert repeated["no_new_evidence"] is True
    assert context.search_stopped_for_no_gain is True


def test_master_search_guard_blocks_duplicates_and_stops_after_no_gain() -> None:
    class FakeSearchAgent:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.search_tool = SimpleNamespace(
                enable_semantic_reranker=True,
                enable_agentic_retrieval=False,
            )

        async def search_knowledge_base(self, query, progress_callback=None):
            self.calls.append(query)
            return {
                "results": [
                    {
                        "id": "doc-1",
                        "citation_id": "1",
                        "title": "GB 38031",
                        "content": "Verified standard evidence",
                        "url": "https://example.test/gb-38031",
                        "score": 1.0,
                        "reranker_score": 2.0,
                    }
                ]
            }

    search_agent = FakeSearchAgent()
    master = MasterAgent.__new__(MasterAgent)
    master.agent_id = "search-guard-test"
    master.search_agent = search_agent
    master.metadata_agent = None
    master.data_insight_agent = None
    master.ontology_agent = None
    turn = QueryEngineContext(
        original_question="Summarize GB 38031",
        max_search_attempts=5,
    )
    context_var = master._turn_context_var()
    token = context_var.set(turn)
    search = next(
        tool for tool in master._create_tools()
        if tool.__name__ == "search_knowledge"
    )
    try:
        first = search("GB 38031 scope")
        duplicate = search(" gb-38031 SCOPE ")
        second = search("GB 38031 applicability")
        stopped = search("GB 38031 compliance requirements")
    finally:
        context_var.reset(token)

    assert "attempt=1" in first
    assert "remaining_attempts=4" in first
    assert "stop_reason=equivalent_query_already_executed" in duplicate
    assert "new_unique_evidence=0" in second
    assert "stop_reason=no_new_evidence" in stopped
    assert search_agent.calls == ["GB 38031 scope", "GB 38031 applicability"]
    assert turn.search_attempts == 2


def test_master_parallel_search_uses_one_attempt_per_query_set() -> None:
    class FakeSearchTool:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        async def parallel_search(self, queries, progress_callback=None):
            self.calls.append(list(queries))
            return [
                [
                    {
                        "id": "doc-1",
                        "title": "GB 38031",
                        "content": "Verified standard evidence",
                        "url": "https://example.test/gb-38031",
                        "score": 1.0,
                        "reranker_score": 2.0,
                    }
                ]
                for _query in queries
            ]

    search_tool = FakeSearchTool()
    master = MasterAgent.__new__(MasterAgent)
    master.agent_id = "parallel-search-guard-test"
    master.search_agent = SimpleNamespace(search_tool=search_tool)
    master.metadata_agent = None
    master.data_insight_agent = None
    master.ontology_agent = None
    turn = QueryEngineContext(
        original_question="Compare GB 38031 scope and applicability",
        max_search_attempts=5,
    )
    context_var = master._turn_context_var()
    token = context_var.set(turn)
    search = next(
        tool for tool in master._create_tools()
        if tool.__name__ == "search_multiple_queries"
    )
    try:
        first = search(["GB 38031 scope", "GB 38031 applicability"])
        duplicate = search(["gb-38031 APPLICABILITY", "gb 38031 scope"])
        second = search(["GB 38031 test requirements"])
        stopped = search(["GB 38031 compliance evidence"])
    finally:
        context_var.reset(token)

    assert "attempt=1" in first
    assert "stop_reason=equivalent_query_already_executed" in duplicate
    assert "new_unique_evidence=0" in second
    assert "stop_reason=no_new_evidence" in stopped
    assert search_tool.calls == [
        ["GB 38031 scope", "GB 38031 applicability"],
        ["GB 38031 test requirements"],
    ]
    assert turn.search_attempts == 2


def test_decompose_query_repairs_wording_without_adding_qualifiers() -> None:
    captured: list[str] = []

    async def fake_run_agent(agent, prompt, session=None):
        captured.append(prompt)
        return SimpleNamespace(text="1. first\n2. second\n3. third")

    master = MasterAgent.__new__(MasterAgent)
    master.agent_id = "decompose-policy-test"
    master.search_agent = SimpleNamespace()
    master.metadata_agent = None
    master.data_insight_agent = None
    master.ontology_agent = None
    decompose = next(
        tool for tool in master._create_tools()
        if tool.__name__ == "decompose_query"
    )

    with patch("src.agents.master_agent.create_maf_agent", return_value=object()), patch(
        "src.agents.master_agent.run_agent", side_effect=fake_run_agent
    ):
        result = decompose("电动汽车用动力蓄电池安全要求")

    assert result.startswith("Successfully decomposed query.")
    prompt = captured[0]
    assert "电动汽车用动力蓄电池安全要求" in prompt
    assert "Repair only defective wording" in prompt
    assert "Never add a qualifier the user did not state" in prompt
    assert "never to narrow the question" in prompt
    assert "Enrich each query" not in prompt


def test_master_turn_keeps_ontology_mode_request_local() -> None:
    master = MasterAgent.__new__(MasterAgent)
    master.agent_id = "mode-test"

    enabled = master._new_turn("question", enable_ontology=True)
    disabled = master._new_turn("question", enable_ontology=False)

    assert enabled.enable_ontology is True
    assert disabled.enable_ontology is False


def test_maf_function_loop_is_bounded_from_active_configuration() -> None:
    client = create_chat_client()
    config = client.function_invocation_configuration

    assert config["enabled"] is True
    assert config["max_iterations"] == AppConfig.QUERY_ENGINE_MAX_MODEL_ROUNDTRIPS
    assert config["max_function_calls"] == AppConfig.QUERY_ENGINE_MAX_FUNCTION_CALLS
    assert config["max_consecutive_errors_per_request"] == (
        AppConfig.QUERY_ENGINE_MAX_CONSECUTIVE_ERRORS
    )

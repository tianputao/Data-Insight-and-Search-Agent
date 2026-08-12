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

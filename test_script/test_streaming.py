"""SSE adapter regression tests independent of external model services."""

import asyncio
import json
from threading import Event

from src.api import main


class _Content:
    def __init__(self, content_type: str, **kwargs) -> None:
        self.type = content_type
        for key, value in kwargs.items():
            setattr(self, key, value)


class _Update:
    def __init__(self, text: str | None = None, contents: list | None = None) -> None:
        self.text = text
        self.contents = contents or []


class _FakeMasterAgent:
    async def chat_stream(
        self,
        message,
        thread,
        stream_context=None,
        cancel_event=None,
        enable_ontology=None,
        business_layer="",
    ):
        yield _Update(text="I will verify the source.")
        yield _Update(
            contents=[
                _Content(
                    "function_call",
                    name="search_knowledge",
                    arguments='{"query":"source"}',
                    call_id="call-1",
                )
            ]
        )
        yield _Update(
            contents=[
                _Content(
                    "function_result",
                    result="",
                    call_id="call-1",
                    exception=None,
                )
            ]
        )
        yield _Update(text="Final answer.")


def test_working_text_is_separate_from_final_answer() -> None:
    async def run_test() -> None:
        original = main.state.master_agent
        main.state.master_agent = _FakeMasterAgent()
        active_run = main.ActiveRun(run_id="test-run", cancel_event=Event())
        try:
            chunks = [
                chunk
                async for chunk in main._stream_agent_response(
                    "question", object(), "stream-test-thread", active_run
                )
            ]
        finally:
            main.state.master_agent = original

        payloads = [json.loads(chunk.removeprefix("data: ").strip()) for chunk in chunks]
        narration_index = next(
            index for index, payload in enumerate(payloads) if payload.get("kind") == "narration"
        )
        reset_index = next(
            index for index, payload in enumerate(payloads)
            if payload.get("type") == "answer_reset"
        )
        candidate_index = next(
            index for index, payload in enumerate(payloads)
            if payload.get("content") == "I will verify the source."
        )
        final_index = next(
            index for index, payload in enumerate(payloads)
            if payload.get("content") == "Final answer."
        )

        assert candidate_index < reset_index < narration_index < final_index
        assert [payload["content"] for payload in payloads if payload["type"] == "text"] == [
            "I will verify the source.",
            "Final answer."
        ]
        assert payloads[-1] == {"type": "done", "content": "Final answer."}

    asyncio.run(run_test())


def test_disabled_ontology_sanitizes_incorrect_data_pipeline_narration() -> None:
    class _IncorrectNarrationMasterAgent:
        async def chat_stream(
            self,
            message,
            thread,
            stream_context=None,
            cancel_event=None,
            enable_ontology=None,
            business_layer="",
        ):
            yield _Update(text="我会让 OntologyAgent 优先匹配企业正式定义。")
            yield _Update(
                contents=[
                    _Content(
                        "function_call",
                        name="delegate_data_analysis",
                        arguments='{"question":"monthly sales"}',
                        call_id="data-call",
                    )
                ]
            )
            yield _Update(
                contents=[
                    _Content(
                        "function_result",
                        result="[STREAMED] complete",
                        call_id="data-call",
                        exception=None,
                    )
                ]
            )
            yield _Update(text="分析完成。")

    async def run_test() -> None:
        original = main.state.master_agent
        original_history = main.state.thread_history
        main.state.master_agent = _IncorrectNarrationMasterAgent()
        main.state.thread_history = {"disabled-ontology-thread": []}
        active_run = main.ActiveRun(run_id="disabled-mode", cancel_event=Event())
        try:
            chunks = [
                chunk
                async for chunk in main._stream_agent_response(
                    "monthly sales",
                    object(),
                    "disabled-ontology-thread",
                    active_run,
                    enable_ontology=False,
                )
            ]
        finally:
            main.state.master_agent = original
            main.state.thread_history = original_history

        payloads = [
            json.loads(chunk.removeprefix("data: ").strip()) for chunk in chunks
        ]
        narrations = [
            payload["message"]
            for payload in payloads
            if payload.get("kind") == "narration"
        ]
        assert len(narrations) == 1
        assert "OntologyAgent" not in narrations[0]
        assert "MetadataAgent" in narrations[0]
        assert "DataInsightAgent" in narrations[0]

    asyncio.run(run_test())


def test_session_cache_is_scoped_to_one_thread() -> None:
    original_history = main.state.thread_history
    try:
        main.state.thread_history = {
            "thread-a": [
                {
                    "user": "  SAME   Question ",
                    "assistant": "cached answer",
                    "timestamp": main.datetime.now(main.timezone.utc).isoformat(),
                    "enable_ontology": True,
                    "cache_eligible": True,
                }
            ],
            "thread-b": [],
        }

        assert main._find_cached_response("thread-a", "same question", True) == "cached answer"
        assert main._find_cached_response("thread-a", "same question", False) is None
        assert main._find_cached_response("thread-b", "same question", True) is None
    finally:
        main.state.thread_history = original_history


def test_session_cache_rejects_failed_and_unqualified_turns() -> None:
    original_history = main.state.thread_history
    timestamp = main.datetime.now(main.timezone.utc).isoformat()
    try:
        main.state.thread_history = {
            "thread-a": [
                {
                    "user": "same question",
                    "assistant": "A legacy answer without an explicit completion outcome.",
                    "timestamp": timestamp,
                    "enable_ontology": False,
                },
                {
                    "user": "same question",
                    "assistant": "Data analysis timed out after 180 seconds. Please try again.",
                    "timestamp": timestamp,
                    "enable_ontology": False,
                    "cache_eligible": False,
                },
            ]
        }

        assert main._find_cached_response("thread-a", "same question", False) is None
        assert main._response_cache_eligibility(
            "本次数据分析在 180 秒后超时，未能获得可靠结果，请稍后重试。"
        ) == (False, "failure_response")
        assert main._response_cache_eligibility(
            "Connection reset while contacting the model. Please retry."
        ) == (False, "failure_response")
        assert main._response_cache_eligibility("分析完成，结果为 42。") == (
            True,
            "completed_response",
        )
    finally:
        main.state.thread_history = original_history


def test_timeout_answer_stream_is_stored_but_never_reused() -> None:
    class _TimeoutAnswerMasterAgent:
        async def chat_stream(
            self,
            message,
            thread,
            stream_context=None,
            cancel_event=None,
            enable_ontology=None,
            business_layer="",
        ):
            yield _Update(
                text=(
                    "本次数据分析在 180 秒后超时，未能获得可靠结果，"
                    "因此暂时无法确认结论。请稍后重试。"
                )
            )

    async def run_test() -> None:
        original_agent = main.state.master_agent
        original_history = main.state.thread_history
        main.state.master_agent = _TimeoutAnswerMasterAgent()
        main.state.thread_history = {"timeout-thread": []}
        active_run = main.ActiveRun(run_id="timeout-run", cancel_event=Event())
        try:
            chunks = [
                chunk
                async for chunk in main._stream_agent_response(
                    "same question",
                    object(),
                    "timeout-thread",
                    active_run,
                    enable_ontology=False,
                )
            ]
            turn = main.state.thread_history["timeout-thread"][0]
            assert turn["cache_eligible"] is False
            assert turn["cache_eligibility_reason"] == "failure_response"
            assert main._find_cached_response(
                "timeout-thread",
                "same question",
                False,
            ) is None
            payloads = [
                json.loads(chunk.removeprefix("data: ").strip())
                for chunk in chunks
            ]
            assert payloads[-1]["type"] == "done"
        finally:
            main.state.master_agent = original_agent
            main.state.thread_history = original_history

    asyncio.run(run_test())


def test_tool_failure_prevents_cache_when_final_text_is_generic() -> None:
    class _ToolFailureMasterAgent:
        async def chat_stream(
            self,
            message,
            thread,
            stream_context=None,
            cancel_event=None,
            enable_ontology=None,
            business_layer="",
        ):
            yield _Update(
                contents=[
                    _Content(
                        "function_call",
                        name="delegate_data_analysis",
                        arguments='{"question":"same question"}',
                        call_id="analysis-call",
                    )
                ]
            )
            yield _Update(
                contents=[
                    _Content(
                        "function_result",
                        result="DataInsight query timed out (180 s).",
                        call_id="analysis-call",
                        exception=None,
                    )
                ]
            )
            yield _Update(text="The run ended without a result.")

    async def run_test() -> None:
        original_agent = main.state.master_agent
        original_history = main.state.thread_history
        main.state.master_agent = _ToolFailureMasterAgent()
        main.state.thread_history = {"tool-failure-thread": []}
        active_run = main.ActiveRun(run_id="tool-failure-run", cancel_event=Event())
        try:
            _ = [
                chunk
                async for chunk in main._stream_agent_response(
                    "same question",
                    object(),
                    "tool-failure-thread",
                    active_run,
                    enable_ontology=False,
                )
            ]
            turn = main.state.thread_history["tool-failure-thread"][0]
            assert main._response_cache_eligibility(turn["assistant"]) == (
                True,
                "completed_response",
            )
            assert turn["cache_eligible"] is False
            assert turn["cache_eligibility_reason"] == "explicitly_ineligible"
            assert main._find_cached_response(
                "tool-failure-thread",
                "same question",
                False,
            ) is None
        finally:
            main.state.master_agent = original_agent
            main.state.thread_history = original_history

    asyncio.run(run_test())


def test_cancelled_stream_emits_stopped_without_done() -> None:
    class _BlockingMasterAgent:
        async def chat_stream(
            self,
            message,
            thread,
            stream_context=None,
            cancel_event=None,
            enable_ontology=None,
            business_layer="",
        ):
            while not cancel_event.is_set():
                await asyncio.sleep(0.01)
            if False:
                yield None

    async def run_test() -> None:
        original_agent = main.state.master_agent
        original_runs = main.state.active_runs
        main.state.master_agent = _BlockingMasterAgent()
        main.state.active_runs = {}
        active_run = main.ActiveRun(run_id="cancel-test", cancel_event=Event())
        main.state.active_runs["cancel-thread"] = active_run
        try:
            stream = main._stream_agent_response(
                "question",
                object(),
                "cancel-thread",
                active_run,
            )
            collector = asyncio.create_task(_collect(stream))
            await asyncio.sleep(0.03)
            active_run.cancel_event.set()
            assert active_run.task is not None
            active_run.task.cancel()
            chunks = await collector
        finally:
            main.state.master_agent = original_agent
            main.state.active_runs = original_runs

        payloads = [json.loads(chunk.removeprefix("data: ").strip()) for chunk in chunks]
        assert any(payload.get("type") == "stopped" for payload in payloads)
        assert not any(payload.get("type") == "done" for payload in payloads)

    async def _collect(stream):
        return [chunk async for chunk in stream]

    asyncio.run(run_test())


def test_two_threads_stream_concurrently_without_cross_talk() -> None:
    observed_modes: dict[str, bool] = {}

    class _ConcurrentMasterAgent:
        async def chat_stream(
            self,
            message,
            thread,
            stream_context=None,
            cancel_event=None,
            enable_ontology=None,
            business_layer="",
        ):
            observed_modes[message] = enable_ontology
            yield _Update(text=f"{message}-part-1")
            await asyncio.sleep(0.02)
            yield _Update(text=f"{message}-part-2")

    async def run_test() -> None:
        original_agent = main.state.master_agent
        original_runs = main.state.active_runs
        main.state.master_agent = _ConcurrentMasterAgent()
        main.state.active_runs = {}
        run_a = main.ActiveRun(run_id="run-a", cancel_event=Event())
        run_b = main.ActiveRun(run_id="run-b", cancel_event=Event())
        main.state.active_runs.update({"thread-a": run_a, "thread-b": run_b})
        try:
            chunks_a, chunks_b = await asyncio.gather(
                _collect(
                    main._stream_agent_response(
                        "alpha", object(), "thread-a", run_a, True
                    )
                ),
                _collect(
                    main._stream_agent_response(
                        "beta", object(), "thread-b", run_b, False
                    )
                ),
            )
        finally:
            main.state.master_agent = original_agent
            main.state.active_runs = original_runs

        payloads_a = [json.loads(chunk.removeprefix("data: ").strip()) for chunk in chunks_a]
        payloads_b = [json.loads(chunk.removeprefix("data: ").strip()) for chunk in chunks_b]
        assert payloads_a[-1]["content"] == "alpha-part-1alpha-part-2"
        assert payloads_b[-1]["content"] == "beta-part-1beta-part-2"
        assert "beta" not in payloads_a[-1]["content"]
        assert "alpha" not in payloads_b[-1]["content"]
        assert observed_modes == {"alpha": True, "beta": False}

    async def _collect(stream):
        return [chunk async for chunk in stream]

    asyncio.run(run_test())

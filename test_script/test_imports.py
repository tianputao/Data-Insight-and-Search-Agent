"""Import and construction smoke tests for the supported MAF runtime."""

from agent_framework import Agent, AgentSession, SkillsProvider
from agent_framework.openai import OpenAIChatCompletionClient

from src.agents.maf_runtime import create_agent, create_chat_client
from src.config import AzureOpenAIConfig


def test_maf_runtime_imports() -> None:
    client = create_chat_client()

    assert isinstance(client, OpenAIChatCompletionClient)
    assert Agent is not None
    assert AgentSession is not None
    assert SkillsProvider is not None


def test_maf_runtime_can_route_to_small_deployment() -> None:
    client = create_chat_client(model=AzureOpenAIConfig.SMALL_GPT_DEPLOYMENT)

    assert client.model == AzureOpenAIConfig.SMALL_GPT_DEPLOYMENT


def test_gpt5_agents_send_reasoning_effort_not_temperature() -> None:
    toolless = create_agent(
        name="reasoning-effort-test",
        instructions="test",
        tools=[],
        reasoning_effort="low",
        model=AzureOpenAIConfig.SMALL_GPT_DEPLOYMENT,
    )

    # gpt-5 deployments reject temperature/top_p and expose reasoning_effort instead.
    assert toolless.default_options["reasoning_effort"] == "low"
    assert "temperature" not in toolless.default_options
    assert "top_p" not in toolless.default_options

    def sample_tool() -> str:
        """A tool."""
        return "ok"

    with_tools = create_agent(
        name="reasoning-effort-with-tools",
        instructions="test",
        tools=[sample_tool],
        reasoning_effort="low",
        model=AzureOpenAIConfig.SMALL_GPT_DEPLOYMENT,
    )

    # Chat Completions rejects reasoning_effort alongside function tools.
    assert "reasoning_effort" not in with_tools.default_options

"""Microsoft Agent Framework runtime helpers shared by application agents."""

from __future__ import annotations

from collections.abc import AsyncIterable, Sequence
from typing import Any, Optional

from agent_framework import (
    Agent,
    AgentResponse,
    AgentResponseUpdate,
    AgentSession,
    ContextProvider,
    ResponseStream,
)
from agent_framework.openai import OpenAIChatCompletionClient
from azure.identity import DefaultAzureCredential

from ..config import AppConfig, AzureOpenAIConfig


def create_chat_client() -> OpenAIChatCompletionClient:
    """Create the MAF OpenAI provider configured for Azure OpenAI."""
    common: dict[str, Any] = {
        "model": AzureOpenAIConfig.GPT_DEPLOYMENT,
        "azure_endpoint": AzureOpenAIConfig.ENDPOINT,
        "api_version": AzureOpenAIConfig.API_VERSION,
        "function_invocation_configuration": {
            "enabled": True,
            "max_iterations": AppConfig.QUERY_ENGINE_MAX_MODEL_ROUNDTRIPS,
            "max_function_calls": AppConfig.QUERY_ENGINE_MAX_FUNCTION_CALLS,
            "max_consecutive_errors_per_request": (
                AppConfig.QUERY_ENGINE_MAX_CONSECUTIVE_ERRORS
            ),
        },
    }
    if AzureOpenAIConfig.use_api_key():
        common["api_key"] = AzureOpenAIConfig.API_KEY
    else:
        common["credential"] = DefaultAzureCredential()
    return OpenAIChatCompletionClient(**common)


def create_agent(
    *,
    name: str,
    instructions: str,
    tools: Sequence[Any],
    temperature: float,
    context_providers: Optional[Sequence[ContextProvider]] = None,
) -> Agent:
    """Create a MAF Agent while keeping construction consistent across sub-agents."""
    return create_chat_client().as_agent(
        name=name,
        instructions=instructions,
        tools=list(tools),
        context_providers=list(context_providers or []),
        default_options={"temperature": temperature},
    )


def create_session(agent: Agent) -> AgentSession:
    """Create an in-memory conversation session for an agent."""
    return agent.create_session()


async def run_agent(
    agent: Agent,
    message: str,
    *,
    session: AgentSession | None = None,
) -> AgentResponse:
    """Run an agent without streaming."""
    return await agent.run(message, session=session)


def stream_agent(
    agent: Agent,
    message: str,
    *,
    session: AgentSession | None = None,
) -> ResponseStream[AgentResponseUpdate, AgentResponse[Any]]:
    """Run an agent as an async stream."""
    return agent.run(message, stream=True, session=session)

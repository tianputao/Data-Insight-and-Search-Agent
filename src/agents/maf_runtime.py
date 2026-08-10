"""Microsoft Agent Framework runtime helpers shared by application agents."""

from __future__ import annotations

from collections.abc import Sequence
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


def create_chat_client(
    *,
    model: Optional[str] = None,
    max_iterations: Optional[int] = None,
    max_function_calls: Optional[int] = None,
) -> OpenAIChatCompletionClient:
    """Create the MAF OpenAI provider configured for Azure OpenAI."""
    common: dict[str, Any] = {
        "model": model or AzureOpenAIConfig.GPT_DEPLOYMENT,
        "azure_endpoint": AzureOpenAIConfig.ENDPOINT,
        "api_version": AzureOpenAIConfig.API_VERSION,
        "function_invocation_configuration": {
            "enabled": True,
            "max_iterations": (
                max_iterations
                if max_iterations is not None
                else AppConfig.QUERY_ENGINE_MAX_MODEL_ROUNDTRIPS
            ),
            "max_function_calls": (
                max_function_calls
                if max_function_calls is not None
                else AppConfig.QUERY_ENGINE_MAX_FUNCTION_CALLS
            ),
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
    reasoning_effort: str,
    context_providers: Optional[Sequence[ContextProvider]] = None,
    model: Optional[str] = None,
    max_iterations: Optional[int] = None,
    max_function_calls: Optional[int] = None,
) -> Agent:
    """Create a MAF Agent while keeping construction consistent across sub-agents."""
    selected_model = model or AzureOpenAIConfig.GPT_DEPLOYMENT
    resolved_tools = list(tools)
    default_options: dict[str, Any] = {}
    # gpt-5 rejects temperature/top_p and exposes reasoning_effort, but Chat Completions
    # refuses reasoning_effort whenever function tools are present; that combination needs
    # the Responses API, which this client does not use.
    if selected_model.strip().lower().startswith("gpt-5") and not resolved_tools:
        default_options["reasoning_effort"] = reasoning_effort
    return create_chat_client(
        model=selected_model,
        max_iterations=max_iterations,
        max_function_calls=max_function_calls,
    ).as_agent(
        name=name,
        instructions=instructions,
        tools=resolved_tools,
        context_providers=list(context_providers or []),
        default_options=default_options,
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

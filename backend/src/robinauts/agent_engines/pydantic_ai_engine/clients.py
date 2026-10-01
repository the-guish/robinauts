# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The model of a configured model, built from the settings and never from the environment."""

from __future__ import annotations

import pydantic_ai
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from robinauts.agent_engines.contract.domain import ProviderKind, UnknownModelError
from robinauts.agent_engines.contract.ports import EngineSettings

ENDPOINTS = {
    ProviderKind.ANTHROPIC: "https://api.anthropic.com",
    ProviderKind.OPENAI: "https://api.openai.com/v1",
}
ANTHROPIC_KINDS = frozenset({ProviderKind.ANTHROPIC, ProviderKind.ANTHROPIC_COMPATIBLE})


def force_tracing_off() -> None:
    # Undoes a process-wide default set by `logfire.instrument_pydantic_ai()`; no variable turns
    # it on. The banner is an advertisement written to standard error on the first run.
    Agent.instrument_all(False)
    pydantic_ai.BANNER_ENABLED = False


def chat_model(model_id: str, settings: EngineSettings) -> tuple[Model, ModelSettings]:
    model = settings.models.models.get(model_id)
    if model is None:
        raise UnknownModelError(model_id)
    provider = settings.models.providers[model.provider]
    key = settings.keys.key_for(provider.id)
    endpoint = ENDPOINTS.get(provider.kind, provider.base_url)
    model_settings = ModelSettings(timeout=model.timeout_seconds)
    if model.max_output_tokens is not None:
        model_settings["max_tokens"] = model.max_output_tokens
    if provider.kind in ANTHROPIC_KINDS:
        anthropic = AsyncAnthropic(
            api_key=key, base_url=endpoint, max_retries=0, timeout=model.timeout_seconds
        )
        return (
            AnthropicModel(model.name, provider=AnthropicProvider(anthropic_client=anthropic)),
            model_settings,
        )
    openai = AsyncOpenAI(
        api_key=key, base_url=endpoint, max_retries=0, timeout=model.timeout_seconds
    )
    return (
        OpenAIChatModel(model.name, provider=OpenAIProvider(openai_client=openai)),
        model_settings,
    )

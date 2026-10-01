# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The chat model of a configured model, built from the settings and never from the environment."""

from __future__ import annotations

import os

import langsmith
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from robinauts.agent_engines.contract.domain import ProviderKind, UnknownModelError
from robinauts.agent_engines.contract.ports import EngineSettings

ENDPOINTS = {
    ProviderKind.ANTHROPIC: "https://api.anthropic.com",
    ProviderKind.OPENAI: "https://api.openai.com/v1",
}
ANTHROPIC_KINDS = frozenset({ProviderKind.ANTHROPIC, ProviderKind.ANTHROPIC_COMPATIBLE})


def force_tracing_off() -> None:
    langsmith.configure(enabled=False)
    # langchain-core raises when these ask for the v1 tracer and v2 tracing is off
    for name in ("LANGCHAIN_TRACING", "LANGCHAIN_HANDLER"):
        os.environ.pop(name, None)


def chat_model(model_id: str, settings: EngineSettings) -> BaseChatModel:
    model = settings.models.models.get(model_id)
    if model is None:
        raise UnknownModelError(model_id)
    provider = settings.models.providers[model.provider]
    key = settings.keys.key_for(provider.id)
    endpoint = ENDPOINTS.get(provider.kind, provider.base_url)
    if provider.kind in ANTHROPIC_KINDS:
        return ChatAnthropic(
            model=model.name,
            api_key=key,
            base_url=endpoint,
            timeout=model.timeout_seconds,
            max_retries=0,
            max_tokens=model.max_output_tokens,
        )
    return ChatOpenAI(
        model=model.name,
        api_key=key,
        base_url=endpoint,
        timeout=model.timeout_seconds,
        max_retries=0,
        max_tokens=model.max_output_tokens,
    )

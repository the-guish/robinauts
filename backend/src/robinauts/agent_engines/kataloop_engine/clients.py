# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The model client of a configured model, built from the settings and never from the environment.

kataloop reaches the vendors through uni-llm-client, whose providers are named, each with a kind,
a key and an endpoint, and whose model ids are ``<provider>/<model>``. A turn's client has one
provider: the model's, under its id in the configuration.
"""

from __future__ import annotations

from collections.abc import Mapping

import httpx2
from kataloop_core import ModelSpec
from uni_llm_client import AsyncClient, ProviderConfig

from robinauts.agent_engines.contract.domain import (
    MODEL_RETRIES,
    ProviderKind,
    UnknownModelError,
)
from robinauts.agent_engines.contract.ports import EngineSettings

KINDS: Mapping[ProviderKind, str] = {
    ProviderKind.ANTHROPIC: "anthropic",
    ProviderKind.ANTHROPIC_COMPATIBLE: "anthropic",
    # uni-llm-client's OpenRouter kind is its Chat Completions client, at the endpoint it is
    # given. OpenAI itself waits for the OpenAI kinds of uni-llm-client.
    ProviderKind.OPENAI_COMPATIBLE: "openrouter",
}
"""The uni-llm-client kind each provider kind this engine reaches is spoken in."""

PINNED = frozenset({ProviderKind.ANTHROPIC})
"""The kinds whose endpoint is the vendor's own, which uni-llm-client pins: no ``base_url``."""


def client_for(
    model_id: str, settings: EngineSettings, *, http_client: httpx2.AsyncClient | None = None
) -> tuple[AsyncClient, ModelSpec]:
    """The client that reaches the model's provider, with its key, and the model as kataloop
    takes it. The caller closes the client, and ``http_client`` with it.

    ``UnknownModelError`` for a model the settings do not have, or on a kind this engine does
    not reach.
    """
    model = settings.models.models.get(model_id)
    if model is None:
        raise UnknownModelError(model_id)
    provider = settings.models.providers[model.provider]
    kind = KINDS.get(provider.kind)
    if kind is None:
        raise UnknownModelError(f"{model_id}: kataloop does not reach {provider.kind.value!r}")
    if "/" in provider.id:
        # A uni-llm-client model id is split at its first "/" into the provider and the model.
        raise UnknownModelError(f"{model_id}: provider id {provider.id!r} holds a '/'")
    base_url = None if provider.kind in PINNED else provider.base_url
    config = ProviderConfig(kind, settings.keys.key_for(provider.id), base_url)
    client = AsyncClient(
        providers={provider.id: config}, http_client=http_client, max_retries=MODEL_RETRIES
    )
    spec = ModelSpec(
        f"{provider.id}/{model.name}",
        timeout=model.timeout_seconds,
        max_output_tokens=model.max_output_tokens,
        context_window=model.context_window,
    )
    return client, spec

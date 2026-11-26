# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The one way OpenCode authenticates from this worker: the operator's API key of each model
provider, read from the variable its ``api_key_env`` names, as for the server's own agents
(CREDENTIALS.md).

Robinauts creates, requests, refreshes, stores, prints and shares no credential. This module
reads the *names* of variables and checks which are set; the engine hands OpenCode the keys in
its configuration, in its environment alone, and they never leave it but towards the provider.
OpenCode's own logins (``opencode auth login``, its ``auth.json``) are not used: a subscription
login is one person's, and this version of the worker runs on keys alone.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from robinauts.agent_engines.contract.domain import ModelProviderConfig, ProviderKind

REFUSED = (
    "the provider refused the API key in {variable}: check it with the provider and replace the"
    " value (CREDENTIALS.md)"
)
AUTH_STATUSES = (401, 403)


def credential_problems(
    environ: Mapping[str, str], providers: Iterable[ModelProviderConfig]
) -> list[str]:
    """What keeps this worker from starting, with the providers its agents' models are on: one
    line each, none for a start that may go on."""
    problems: list[str] = []
    for provider in providers:
        if provider.kind is ProviderKind.OPENAI_COMPATIBLE and not provider.base_url:
            problems.append(
                f"provider {provider.id!r} is of kind 'openai-compatible' and names no base_url:"
                " OpenCode needs the endpoint to reach it"
            )
        if not environ.get(provider.api_key_env, "").strip():
            problems.append(
                f"the API key of provider {provider.id!r} is read from {provider.api_key_env},"
                " which is unset or empty"
            )
    return problems


def refused_text(provider: ModelProviderConfig) -> str:
    """What the operator reads when the provider refuses the key: which one, and what to do,
    never its value."""
    return REFUSED.format(variable=provider.api_key_env)

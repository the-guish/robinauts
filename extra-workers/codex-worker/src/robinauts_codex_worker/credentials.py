# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The two ways Codex may authenticate to OpenAI from this worker, and the checks that keep each
where it belongs (CREDENTIALS.md).

Robinauts creates, requests, refreshes, stores, prints and shares no credential. This module
reads the *names* of variables and whether a login file is there, and hands the Codex
subprocess the one the mode calls for. The values are never logged, and never leave the process
but towards the provider, inside Codex.

- ``personal``: one person, their own machine, their own ChatGPT plan. They signed in
  themselves with ``codex login`` (``--device-auth`` from a container), which wrote
  ``$CODEX_HOME/auth.json``. The worker refuses to run it against anything but a local server,
  or towards any endpoint but OpenAI's.
- ``deployed``: an instance with sign-in, several people. The operator's API key, read from the
  variable the model provider's ``api_key_env`` names, which Codex reads per request as that
  provider's ``env_key``. A ChatGPT login in ``$CODEX_HOME`` is refused: it is one person's,
  and may not serve a deployment.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path

from robinauts.agent_engines.contract.domain import ModelProviderConfig, ProviderKind
from robinauts.agent_engines.contract.ports import ProviderKeyLookup
from robinauts_codex_worker.settings import AuthMode
from robinauts_worker_common.network import is_local

LOGIN_FILE = "auth.json"
"""Where ``codex login`` keeps a login, under ``CODEX_HOME``: the personal mode's credential."""
ACCESS_TOKEN_VARIABLE = "CODEX_ACCESS_TOKEN"
"""A token Codex reads before the login file. Not supported by this version of the worker."""

REFUSED_PERSONAL = (
    "OpenAI refused the ChatGPT login in CODEX_HOME: it may have expired or been signed out."
    " Run `codex login --device-auth` yourself again, against the same CODEX_HOME; Robinauts"
    " never signs in for you (CREDENTIALS.md)"
)
REFUSED_DEPLOYED = (
    "the provider refused the API key in {variable}: check it with the provider and replace the"
    " value (CREDENTIALS.md)"
)


def _set(environ: Mapping[str, str], variable: str) -> bool:
    return bool(environ.get(variable, "").strip())


def credential_problems(
    mode: AuthMode,
    environ: Mapping[str, str],
    home: Path,
    broker_url: str,
    providers: Iterable[ModelProviderConfig],
) -> list[str]:
    """What keeps this worker from starting in ``mode``, with the providers its agents' models
    are on: one line each, none for a start that may go on."""
    problems: list[str] = []
    providers = list(providers)
    login = home / LOGIN_FILE
    if _set(environ, ACCESS_TOKEN_VARIABLE):
        problems.append(f"{ACCESS_TOKEN_VARIABLE} is not supported by this version of the worker")
    if mode is AuthMode.PERSONAL:
        if not login.is_file():
            problems.append(
                f"personal mode needs {login}: the login you made yourself with"
                " `codex login --device-auth`, with CODEX_HOME set to that folder"
                " (CREDENTIALS.md)"
            )
        for provider in providers:
            if provider.kind is not ProviderKind.OPENAI or provider.base_url:
                problems.append(
                    f"personal mode refuses provider {provider.id!r}: a ChatGPT login goes to"
                    " OpenAI alone, kind 'openai' with no base_url"
                )
        if not is_local(broker_url):
            problems.append(
                f"personal mode serves the person's own instance alone, and {broker_url} is not"
                " on this machine: a deployment runs in deployed mode, with an API key"
            )
    else:
        if login.exists():
            problems.append(
                f"deployed mode refuses {login}: a personal ChatGPT login may not serve a"
                " deployment (CREDENTIALS.md); remove it and use an API key"
            )
        for provider in providers:
            if provider.kind not in (ProviderKind.OPENAI, ProviderKind.OPENAI_COMPATIBLE):
                problems.append(
                    f"provider {provider.id!r} is of kind {provider.kind.value!r}, which Codex"
                    " cannot reach: it speaks OpenAI's Responses API alone"
                )
            elif not _set(environ, provider.api_key_env):
                problems.append(
                    f"deployed mode reads the API key of provider {provider.id!r} from"
                    f" {provider.api_key_env}, which is unset or empty"
                )
    return problems


def subprocess_environment(
    mode: AuthMode, provider: ModelProviderConfig, keys: ProviderKeyLookup
) -> dict[str, str]:
    """What the Codex subprocess is handed beside the environment it inherits: in deployed mode
    the provider's key, under the name its ``env_key`` gives; in personal mode nothing, since
    the login file is the credential."""
    if mode is AuthMode.PERSONAL:
        return {}
    return {provider.api_key_env: keys.key_for(provider.id)}


def refused_text(mode: AuthMode, provider: ModelProviderConfig) -> str:
    """What the operator reads when the provider refuses the credential: which one, and what to
    do, never its value."""
    if mode is AuthMode.PERSONAL:
        return REFUSED_PERSONAL
    return REFUSED_DEPLOYED.format(variable=provider.api_key_env)

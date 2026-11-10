# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The two ways Claude Code may authenticate to Anthropic from this worker, and the checks that
keep each where it belongs (CREDENTIALS.md).

Robinauts creates, requests, refreshes, stores, prints and shares no credential. This module
reads the *names* of variables, checks which are set, and hands the Claude Code subprocess the
one the mode calls for. The values are never logged, and never leave the process but towards
Anthropic, inside Claude Code.

- ``personal``: one person, their own machine, their own subscription. They made a long-lived
  token themselves with ``claude setup-token`` and put it in ``CLAUDE_CODE_OAUTH_TOKEN``. The
  worker refuses to run it against anything but a local server, with any API key beside it,
  or towards any endpoint but Anthropic's.
- ``deployed``: an instance with sign-in, several people. The operator's Claude Platform API
  key, read from the variable the model provider's ``api_key_env`` names. A subscription token
  in the environment is refused: it is one person's, and may not serve a deployment.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Mapping
from urllib.parse import urlsplit

from robinauts.agent_engines.contract.domain import ModelProviderConfig, ProviderKind
from robinauts.agent_engines.contract.ports import ProviderKeyLookup
from robinauts_claude_worker.settings import AuthMode

OAUTH_TOKEN_VARIABLE = "CLAUDE_CODE_OAUTH_TOKEN"
"""A subscription token from ``claude setup-token``: the personal mode's credential."""

API_KEY_VARIABLE = "ANTHROPIC_API_KEY"
AUTH_TOKEN_VARIABLE = "ANTHROPIC_AUTH_TOKEN"
BASE_URL_VARIABLE = "ANTHROPIC_BASE_URL"
CLOUD_VARIABLES = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
"""What Claude Code reads before, or instead of, the token: each outranks it in its order of
precedence, so a personal worker runs with none of them set."""

LOCAL_HOSTS = frozenset({"localhost", "host.docker.internal", "host-gateway"})

REFUSED_PERSONAL = (
    f"Anthropic refused the subscription token in {OAUTH_TOKEN_VARIABLE}: it may have expired"
    " (they last a year) or been revoked. Run `claude setup-token` yourself again and replace"
    " the value; Robinauts never makes one for you (CREDENTIALS.md)"
)
REFUSED_DEPLOYED = (
    "Anthropic refused the API key in {variable}: check it in the Claude Console and replace"
    " the value (CREDENTIALS.md)"
)


def is_local(url: str) -> bool:
    """Whether the server is on this machine, as a personal worker's must be: loopback, a
    private address, Docker's name for the host, or a unix socket."""
    parts = urlsplit(url)
    if parts.scheme in ("unix", "http+unix"):
        return True
    host = (parts.hostname or "").lower()
    if host in LOCAL_HOSTS:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def _set(environ: Mapping[str, str], variable: str) -> bool:
    return bool(environ.get(variable, "").strip())


def credential_problems(
    mode: AuthMode,
    environ: Mapping[str, str],
    broker_url: str,
    providers: Iterable[ModelProviderConfig],
) -> list[str]:
    """What keeps this worker from starting in ``mode``, with the providers its agents' models
    are on: one line each, none for a start that may go on."""
    problems: list[str] = []
    providers = list(providers)
    if mode is AuthMode.PERSONAL:
        if not _set(environ, OAUTH_TOKEN_VARIABLE):
            problems.append(
                f"personal mode needs {OAUTH_TOKEN_VARIABLE}: the token you made yourself with"
                " `claude setup-token` (CREDENTIALS.md)"
            )
        for variable in (API_KEY_VARIABLE, AUTH_TOKEN_VARIABLE, *CLOUD_VARIABLES):
            if _set(environ, variable):
                problems.append(
                    f"personal mode refuses {variable}: it would outrank the subscription token"
                )
        for provider in providers:
            if _set(environ, provider.api_key_env):
                problems.append(
                    f"personal mode refuses {provider.api_key_env}, the key of provider"
                    f" {provider.id!r}: it would outrank the subscription token"
                )
            if provider.kind is not ProviderKind.ANTHROPIC or provider.base_url:
                problems.append(
                    f"personal mode refuses provider {provider.id!r}: a subscription token goes"
                    " to Anthropic alone, kind 'anthropic' with no base_url"
                )
        if _set(environ, BASE_URL_VARIABLE):
            problems.append(
                f"personal mode refuses {BASE_URL_VARIABLE}: a subscription token goes to"
                " Anthropic alone"
            )
        if not is_local(broker_url):
            problems.append(
                f"personal mode serves the person's own instance alone, and {broker_url} is not"
                " on this machine: a deployment runs in deployed mode, with an API key"
            )
    else:
        if _set(environ, OAUTH_TOKEN_VARIABLE):
            problems.append(
                f"deployed mode refuses {OAUTH_TOKEN_VARIABLE}: a personal subscription token"
                " may not serve a deployment (CREDENTIALS.md); use an API key"
            )
        for variable in (AUTH_TOKEN_VARIABLE, *CLOUD_VARIABLES):
            if _set(environ, variable):
                problems.append(f"{variable} is not supported by this version of the worker")
        for provider in providers:
            if provider.kind not in (ProviderKind.ANTHROPIC, ProviderKind.ANTHROPIC_COMPATIBLE):
                problems.append(
                    f"provider {provider.id!r} is of kind {provider.kind.value!r}, which Claude"
                    " Code cannot reach"
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
    """What the Claude Code subprocess is handed beside the environment it inherits: in
    deployed mode the provider's key, and its endpoint when it has one; in personal mode
    nothing, since the inherited token is the credential."""
    if mode is AuthMode.PERSONAL:
        return {}
    handed = {API_KEY_VARIABLE: keys.key_for(provider.id)}
    if provider.base_url:
        handed[BASE_URL_VARIABLE] = provider.base_url
    return handed


def refused_text(mode: AuthMode, provider: ModelProviderConfig) -> str:
    """What the operator reads when Anthropic refuses the credential: which one, and what to
    do, never its value."""
    if mode is AuthMode.PERSONAL:
        return REFUSED_PERSONAL
    return REFUSED_DEPLOYED.format(variable=provider.api_key_env)

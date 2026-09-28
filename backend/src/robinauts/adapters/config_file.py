# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Reading the configuration: the file, and the environment the secrets are in.

**Adapters read, core validates** (``docs/layout.md``). Everything here hands
back raw data or raises: ``read_toml`` returns whatever tables were in the
file, and ``robinauts.core.parse_sign_in_config`` -- which this may not import
-- is what decides whether they describe a deployment. The composition root
calls the two in turn.

Reading is where a file can be missing, unreadable or misspelt, and each of
those is a ``ConfigError`` naming the file, because an operator reading a
start-up failure has a path to look at and nothing else. TOML puts the line
and the column of a syntax error in its own message, so the message is
repeated whole.

``environment``, ``check_client_secrets`` and ``check_api_keys`` are the other
half. A secret is never in the file: a provider names the environment variable
its client secret or its model API key is read from
(``docs/specs/operations.md``), and this is the layer that may touch the
environment. The checks exist so that a deployment with three providers and
three unset variables is told about all three at once, at start-up, rather
than one at a time as people try to sign in or to ask an agent something.

What ``check_api_keys`` read travels on in a ``ProviderKeys``, which prints
nothing: the keys have to reach the engine adapter, and the shortest path
from the environment to the vendor's client is the one with the fewest places
a key could be written down.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from robinauts.domain import ConfigError, ModelsConfig, SignInConfig

SecretLookup = Callable[[str], str | None]
"""How a secret is asked for: given a variable's name, its value or ``None``.

A callable rather than a direct read of ``os.environ``, so that a test scripts
what the environment holds without setting a real variable -- and so that a
later deployment may take its secrets from somewhere else entirely without
anything above this line changing.
"""


def read_toml(path: str | os.PathLike[str]) -> Mapping[str, Any]:
    """The tables in the TOML file at ``path``, unexamined.

    ``ConfigError`` when the file cannot be read or is not TOML, naming the
    path in every case and the line in a syntax error. Nothing else is judged
    here: an empty file is an empty mapping, and unknown keys, missing values
    and everything else are ``core``'s to refuse.
    """
    where = _named(path)
    try:
        with open(path, "rb") as file:
            return tomllib.load(file)
    except tomllib.TOMLDecodeError as exc:
        # tomllib's message already ends in "(at line L, column C)", which is
        # the whole reason it is repeated rather than summarised.
        raise ConfigError([f"{where}: not valid TOML: {exc}"]) from exc
    except OSError as exc:
        raise ConfigError([f"{where}: {exc.strerror or type(exc).__name__}"]) from exc
    except UnicodeDecodeError as exc:
        # TOML is UTF-8 by definition; a file that is not is not a TOML file.
        raise ConfigError([f"{where}: not valid UTF-8, which TOML must be: {exc.reason}"]) from exc


def environment(name: str) -> str | None:
    """The value of the environment variable ``name``; ``None`` if it has none.

    An empty variable counts as unset. A deployment that exports
    ``ROBINAUTS_GOOGLE_SECRET=`` has not configured a secret, and answering
    "yes, it is there, and it is the empty string" would turn a start-up
    failure that names the variable into a sign-in that the provider refuses.
    """
    return os.environ.get(name) or None


def check_client_secrets(config: SignInConfig, *, secret_for: SecretLookup = environment) -> None:
    """Refuse to start when a provider's client secret is not in the environment.

    Every provider is looked at and **every** missing variable is reported at
    once, in one ``ConfigError`` -- never one restart per variable. Only the
    variable's **name** is in the message; its value is a secret and its
    absence is the whole of what is being reported.

    That is all this promises: every missing *variable*, together. It is not
    "every problem a deployment has, together", which is what
    ``docs/specs/operations.md`` asks of start-up, because this cannot see the
    others -- the file did not parse, a table is malformed, a model provider's
    key is missing. Putting those in one list is the composition root's job,
    and it is the one place that has them all. What it has to do: a file that
    does not parse stops there, since there is no configuration to check
    secrets against; but once ``core`` has accepted one, the problems from
    here belong in the same ``ConfigError`` as every other start-up problem it
    can gather, rather than in a second failure after the first is fixed.

    It takes a ``SignInConfig`` -- a domain record that ``core`` has already
    made -- rather than the raw tables, so that this reads the environment and
    judges nothing else.
    """
    problems = [
        f"providers.{provider.id}: the client secret is read from the environment"
        f" variable {named(provider.client_secret_env)}, which is unset or empty"
        for provider in config.providers.values()
        if not secret_for(provider.client_secret_env)
    ]
    if problems:
        raise ConfigError(problems)


_CONVENTIONAL_VARIABLE = re.compile(r"[A-Z_][A-Z0-9_]*")
"""How a variable an operator means to name is spelt: upper case, digits, underscores."""


def named(variable: str) -> str:
    """The variable's name as a start-up message may carry it.

    A message about an unset variable names the variable so that the operator
    knows what to set -- and a **secret pasted where the name belongs** is a
    valid name too, as far as the spelling rule goes: a GitHub token is
    letters, digits and underscores. Echoing that "name" would put the secret
    in the start-up log. So a name spelt as variables conventionally are is
    printed, and anything else is described rather than repeated; the
    operator finds it in the file, under the key the message names.
    """
    if _CONVENTIONAL_VARIABLE.fullmatch(variable):
        return variable
    return "named in the configuration (not repeated here: it is not spelt like one)"


class ProviderKeys:
    """The model providers' API keys, as this process read them, and nothing else.

    **A carrier, not a store.** It exists so that a key can be handed from the
    one layer that may read the environment to the one layer that talks to a
    vendor, without passing through anything that might write it down. What it
    promises is what it refuses to do:

    - it **prints nothing**. ``repr`` and ``str`` name the providers it holds
      and never a key, so a key cannot reach a log, a traceback frame summary
      or a debugger transcript by being somewhere a value is formatted;
    - it holds a **copy**, so the mapping a caller built cannot be edited
      behind the engine's back;
    - it is **not iterable** and has no ``items``: the only question it
      answers is "the key for this provider", which is the only question an
      engine has.

    It carries the values rather than the ``SecretLookup`` they came from,
    because the promise made at start-up is that the environment was read
    *then*: a deployment that started is a deployment whose keys were there,
    and a variable unset later must not turn into a turn that fails halfway
    (``docs/specs/agents.md``).
    """

    __slots__ = ("_keys",)

    def __init__(self, keys: Mapping[str, str]) -> None:
        self._keys = dict(keys)

    def key_for(self, provider_id: str) -> str:
        """The key of that provider; ``ConfigError`` if this process has none.

        Unreachable in a deployment that started, because ``check_api_keys``
        is what lets one start; it is here so that a mistake in the wiring is
        a refusal naming the provider rather than a ``KeyError`` in the middle
        of somebody's turn.
        """
        try:
            return self._keys[provider_id]
        except KeyError:
            raise ConfigError(
                [f"model_providers.{provider_id}: no key was read for this provider"]
            ) from None

    def __repr__(self) -> str:
        """The providers, never the keys: this is what a log line would hold."""
        return f"ProviderKeys({', '.join(sorted(self._keys))})"


class ToolServerSecrets:
    """The tool servers' secrets, as this process read them, and nothing else.

    ``ProviderKeys`` for the tool servers, with the same promises and for the
    same reasons: it **prints nothing**, it holds a **copy**, it is **not
    iterable**, and the one question it answers is "the secret for this
    server". It carries the values rather than the lookup they came from,
    because the promise made at start-up is that the environment was read
    *then* (``docs/specs/agents.md``, "Tools").
    """

    __slots__ = ("_secrets",)

    def __init__(self, secrets: Mapping[str, str]) -> None:
        self._secrets = dict(secrets)

    def secret_for(self, server_id: str) -> str:
        """The secret of that server; ``ConfigError`` if this process has none.

        Unreachable in a deployment that started, because ``check_tool_secrets``
        is what lets one start; it is here so that a mistake in the wiring is a
        refusal naming the server rather than a ``KeyError`` in the middle of
        somebody's turn.
        """
        try:
            return self._secrets[server_id]
        except KeyError:
            raise ConfigError(
                [f"mcp_servers.{server_id}: no secret was read for this tool server"]
            ) from None

    def __repr__(self) -> str:
        """The servers, never the secrets: this is what a log line would hold."""
        return f"ToolServerSecrets({', '.join(sorted(self._secrets))})"


def check_tool_secrets(
    config: ModelsConfig, *, secret_for: SecretLookup = environment
) -> ToolServerSecrets:
    """Read every tool server's secret, refusing if any variable is unset.

    ``check_api_keys`` for the tool servers: **every** missing variable at
    once, in one ``ConfigError``, only the variable's **name** in the message,
    and every **declared** server looked at whether or not an agent names it
    (``docs/specs/agents.md``, "Tools"). Start-up reads the secret and does
    not connect: whether the server takes it is found out at the first turn
    of an agent naming it, by name.
    """
    problems: list[str] = []
    secrets: dict[str, str] = {}
    for server in config.tool_servers.values():
        secret = secret_for(server.secret_env)
        if secret:
            secrets[server.id] = secret
        else:
            problems.append(
                f"mcp_servers.{server.id}: the secret is read from the environment variable"
                f" {named(server.secret_env)}, which is unset or empty"
            )
    if problems:
        raise ConfigError(problems)
    return ToolServerSecrets(secrets)


def check_api_keys(config: ModelsConfig, *, secret_for: SecretLookup = environment) -> ProviderKeys:
    """Read every model provider's key, refusing if any variable is unset.

    **Every** missing variable is reported at once, in one ``ConfigError``,
    the same promise and for the same reason as ``check_client_secrets``: a
    deployment with three providers and three unset variables is fixed in one
    pass. Only the variable's **name** is in the message.

    Every **declared** provider is looked at, whether or not a model uses it.
    A provider in the file is a provider the operator meant to have, and a
    deployment that started with one of its keys missing would be a deployment
    that works until somebody picks the wrong agent.

    It returns what it read, because the read that proves a variable is set is
    the read whose result is used: going back to the environment later would
    be a second answer to a question already asked, and a chance for the two
    to differ.
    """
    problems: list[str] = []
    keys: dict[str, str] = {}
    for provider in config.providers.values():
        key = secret_for(provider.api_key_env)
        if key:
            keys[provider.id] = key
        else:
            problems.append(
                f"model_providers.{provider.id}: the API key is read from the environment"
                f" variable {named(provider.api_key_env)}, which is unset or empty"
            )
    if problems:
        raise ConfigError(problems)
    return ProviderKeys(keys)


def _named(path: str | os.PathLike[str]) -> str:
    """The path as a message names it: the text of it, however it was given."""
    try:
        return str(Path(path))
    except TypeError:  # pragma: no cover -- a caller passing something else
        return repr(path)

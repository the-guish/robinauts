# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Agents are configuration: the engine that runs one, and how ids are spelt.

An agent is a name, a system prompt, a default model, an engine and the tool
servers it may use (``docs/specs/agents.md``); the operator writes them in the
configuration and users do not create them. What the conversation format needs
of it is here: the engine a message was produced by, and the shape of the ids
an agent and a model are referred to by. So are the records an operator's
configuration is read into -- ``AgentDefinition``, ``ModelProviderConfig``,
``ModelConfig``, ``ToolServerConfig`` and the ``ModelsConfig`` that holds the
four tables together.

**The records alone, with no reading of any file.** An adapter reads the TOML
and ``robinauts.core.parse_models_config`` decides whether it describes a
deployment, exactly as sign-in is read (``docs/layout.md``). What is here is
what every layer above needs and the rules each record keeps to.

**No key is ever in one of these.** A provider names the *environment
variable* its key is read from, and a tool server the one its secret is read
from, which is the whole of what the configuration carries; reading them is
the adapter's, and what it read travels in a ``ProviderKeys`` or a
``ToolServerSecrets`` (``robinauts.adapters.config_file``) that prints nothing.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from urllib.parse import urlsplit

from robinauts.domain.errors import InvalidValueError, UnknownModelError
from robinauts.domain.tools import MAX_TOOL_PREFIX_CHARS, checked_tool_prefix, is_tool_prefix
from robinauts.domain.values import checked_line, checked_text, describe

MAX_CONFIG_ID_CHARS = 40

_CONFIG_ID = re.compile(rf"[a-z0-9][a-z0-9_-]{{0,{MAX_CONFIG_ID_CHARS - 1}}}")
"""What an id the operator writes in the configuration may be spelt with.

Deliberately the same rule as a provider's id
(``robinauts.domain.sign_in.is_provider_id``), and for the same reason: these
ids are keys of a configuration table, they travel in URLs, and they are
recorded on every message an agent produced. One spelling rule means an
operator learns it once, and nothing downstream has to wonder how long an id
can be or what may be in it.
"""


def is_config_id(value: object) -> bool:
    """Whether ``value`` is spelt the way an agent's or a model's id is spelt."""
    return isinstance(value, str) and _CONFIG_ID.fullmatch(value) is not None


def checked_config_id(value: object, what: str) -> str:
    """``value`` if it is an id of that shape; ``InvalidValueError`` if not."""
    if not isinstance(value, str) or _CONFIG_ID.fullmatch(value) is None:
        raise InvalidValueError(
            f"{what} is a name: lower-case letters, digits, '-' and '_', at most "
            f"{MAX_CONFIG_ID_CHARS} of them, not {describe(value)}"
        )
    return value


class Engine(StrEnum):
    """The agent frameworks a turn can be run by (``docs/specs/agents.md``).

    A property of the agent, recorded on every message, so that a conversation
    says which engine produced which answer after the agent has been changed.
    The values are the ones the configuration is written with.
    """

    LANGGRAPH = "langgraph"
    PYDANTIC_AI = "pydantic-ai"


MAX_AGENT_TITLE_CHARS = 120
"""The longest an agent's title may be.

A conversation's title's bound (``robinauts.domain.conversation``), because
both are shown in the same kind of place -- a list, a picker, a heading -- and
one bound is one thing for an operator to learn. It is not imported from
there: that module is built on this one.
"""

MAX_SYSTEM_PROMPT_CHARS = 100_000
"""The longest a system prompt may be.

Generous, since a long prompt is a real way of writing an agent, and bounded
all the same: it is read from a file the operator wrote, sent to a provider on
every turn, and a configuration that holds a megabyte of it is a mistake
somebody should be told about at start-up rather than at the first turn.
"""


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    """An agent as the operator defined it: a name, a prompt, a model, an engine, its tools.

    The record alone (``docs/specs/agents.md``). **Reading the configuration
    is not here**: an adapter reads the raw tables and ``core`` turns them into
    these, as it does for sign-in, and that is a step of its own. What is here
    is what every layer above needs -- the application to run a turn with it,
    an agent adapter to be handed it -- and the rules it keeps to.

    The system prompt is **not a message** and is never stored in a
    conversation: it is taken from the agent's configuration at every turn, so
    editing an agent takes effect at the next turn of its existing
    conversations (``docs/specs/conversations.md``).
    """

    id: str
    """How the configuration and every message produced by it name this agent."""
    title: str
    """What a person picks it by."""
    system_prompt: str
    """What the agent is told before the conversation. May be empty."""
    model: str
    """The platform's own id for its **default** model, not the vendor's name for it.

    Copied into a conversation when it starts, unless its author picks another
    of the configured models, and read off the conversation from then on
    (``Conversation.model``). So a change to it reaches new conversations
    only.
    """
    engine: Engine
    tools: tuple[str, ...] = ()
    """The ids of the tool servers this agent may use (``ToolServerConfig``).

    Read at every turn, so adding a server to an agent reaches its existing
    conversations at their next turn (``docs/specs/agents.md``, "Tools"); the
    order is the operator's and means nothing, since the tools a run is shown
    are sorted by name. ``ModelsConfig`` is what proves every id names a
    server the deployment has.
    """

    def __post_init__(self) -> None:
        checked_config_id(self.id, "an agent's id")
        checked_config_id(self.model, "a model's id")
        checked_line(self.title, "an agent's title", MAX_AGENT_TITLE_CHARS)
        if not self.title.strip():
            raise InvalidValueError("an agent has a title: it is what a person picks it by")
        checked_text(self.system_prompt, "an agent's system prompt", MAX_SYSTEM_PROMPT_CHARS)
        if not isinstance(self.engine, Engine):
            raise InvalidValueError(f"an engine is an Engine, not {describe(self.engine)}")
        if not isinstance(self.tools, tuple):
            raise InvalidValueError(
                f"an agent's tools are a tuple of ids, not {describe(self.tools)}"
            )
        for server_id in self.tools:
            checked_config_id(server_id, "a tool server's id")
        if len(set(self.tools)) != len(self.tools):
            raise InvalidValueError("an agent names each tool server once")


class ProviderKind(StrEnum):
    """The kinds of model provider the configuration knows how to describe.

    The platform's own vocabulary, not a framework's: an engine translates
    these into whichever client it reaches the vendor with
    (``docs/specs/agents.md``). Two of them are a *protocol at an address the
    operator gives* rather than a vendor: ``ANTHROPIC_COMPATIBLE`` is an
    endpoint that speaks Anthropic's Messages API, and ``OPENAI_COMPATIBLE``
    one that speaks OpenAI's. They are the kinds that carry a ``base_url``
    (``KINDS_WITH_BASE_URL``); the vendors' own kinds have one endpoint each
    and their engines pin it.

    **Not every kind is reachable from every build.** A kind whose client does
    not pass the dependency policy is not offered until it does
    (``DEPENDENCIES.md``), which is why ``robinauts.core.parse_models_config``
    is told which kinds the deployment can build rather than assuming all of
    them. This build reaches ``ANTHROPIC`` and ``ANTHROPIC_COMPATIBLE``, which
    is how OpenRouter is reached here: it serves Anthropic's Messages API at
    ``https://openrouter.ai/api/v1/messages`` and takes the key in the same
    ``x-api-key`` header, so the Anthropic client the engines already have is
    the client for it and no OpenAI tree is needed.
    """

    ANTHROPIC = "anthropic"
    ANTHROPIC_COMPATIBLE = "anthropic-compatible"
    OPENAI = "openai"
    OPENAI_COMPATIBLE = "openai-compatible"


KINDS_WITH_BASE_URL: frozenset[ProviderKind] = frozenset(
    {ProviderKind.ANTHROPIC_COMPATIBLE, ProviderKind.OPENAI_COMPATIBLE}
)
"""The kinds whose ``base_url`` is required, and the only ones that may have one.

A kind that names a *protocol* needs the address to speak it to; a kind that
names a *vendor* has one endpoint, pinned in the engine, and a ``base_url``
there would be either a mistake or a way to send the operator's key somewhere
else (``ModelProviderConfig``).
"""

_KINDS_WITH_BASE_URL_NAMED = ", ".join(sorted(kind.value for kind in KINDS_WITH_BASE_URL))
"""Those kinds' names, for a refusal that says which they are.

Built from the set, so that a kind added to it is named by every message
without anybody remembering to edit one.
"""


MAX_ENV_NAME_CHARS = 120
"""The longest the name of an environment variable in the configuration may be.

A bound on a *name*, never on a value: what the variable holds is a key this
never sees.
"""

MAX_MODEL_NAME_CHARS = 200
"""The longest a vendor's name for a model may be, such as ``claude-sonnet-5``."""

MAX_MODEL_TITLE_CHARS = MAX_AGENT_TITLE_CHARS
"""The longest a model's title may be.

An agent's bound, because a model is picked beside an agent, in the same kind
of place, and one bound is one thing for an operator to learn.
"""

MAX_BASE_URL_CHARS = 500
"""The longest a configured endpoint's URL may be (``KINDS_WITH_BASE_URL``)."""

DEFAULT_MODEL_TIMEOUT_SECONDS = 120.0
"""How long one call to a model may take when the configuration does not say.

Per **model call**, and therefore always shorter than the timeout on the whole
turn (``robinauts.application.DEFAULT_TURN_SECONDS``): a turn that has run out
of time is failed by the application whatever the provider is doing, and a
model call that hangs should be the engine's own failure and not that.
"""

MAX_MODEL_TIMEOUT_SECONDS = 3600.0
"""Past this a timeout is not a timeout. An hour is already far past any turn."""

MAX_OUTPUT_TOKENS = 10_000_000
"""The largest ``max_output_tokens`` a model may be configured with.

Nothing close to any model's limit; it is here so that a digit typed twice is
refused at start-up rather than paid for.
"""


_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
"""What a portable environment variable's name is spelt with (POSIX)."""


def is_env_name(value: object) -> bool:
    """Whether ``value`` is the name of an environment variable rather than a value.

    The one rule, here rather than in ``core``, because two configurations ask
    it -- a sign-in provider's client secret and a model provider's key -- and
    a rule in two places is two rules. It is also the cheapest guard there is
    against the mistake that matters: a key pasted where its variable's name
    belongs, which would put the key in the file.
    """
    return isinstance(value, str) and _ENV_NAME.fullmatch(value) is not None


LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
"""The hosts a plain-text endpoint may be on: this machine, and nowhere else.

Spelt as ``urlsplit`` reports a host -- lower case, and an IPv6 address
without the brackets it is written in -- so ``http://[::1]:8080/v1`` is this
machine and is allowed.
"""


def is_endpoint_url(value: object) -> bool:
    """Whether ``value`` is an endpoint the platform will send a key to.

    ``https://`` anywhere, ``http://`` on the loopback interface only: an
    operator's key is what travels to this URL, and a plain-text endpoint on
    another machine is a key given away. A host is required -- ``https://`` on
    its own reaches nothing -- and there is no query and no fragment, because
    a base URL is a prefix a client appends paths to and either of those would
    land in the middle of the request it builds.

    **No userinfo.** ``https://user:sk-live@gateway/v1`` is a credential
    written into the configuration file, and from there into every log line,
    error message and copy of the file that names the endpoint -- which is the
    one thing this configuration promises never to hold
    (``docs/specs/agents.md``). A provider's credential is the environment
    variable ``api_key_env`` names, and nowhere else.

    The host is taken from ``urlsplit``, which is what unbrackets an IPv6
    address, separates any userinfo and lower-cases what is left; deciding
    that by hand is how ``[::1]`` becomes a host called ``[``, and how
    ``http://localhost@evil.example/v1`` becomes "localhost". Deliberately
    narrow all the same: ``core`` has the parser that normalises what a
    browser is redirected to, and ``domain`` may not import it. What this
    refuses is what would be dangerous or would not work; what it accepts is
    handed to the client as it stands.
    """
    if not isinstance(value, str) or not value:
        return False
    if any(character.isspace() for character in value) or not value.isascii():
        return False
    if "?" in value or "#" in value:
        return False
    try:
        split = urlsplit(value)
        # `urlsplit` parses lazily: it is reading the port that raises on one
        # that is not a number, so the port is read here, inside the `try`,
        # and not left for a client to fall over later.
        _ = split.port
    except ValueError:
        return False
    if split.username is not None or split.password is not None:
        return False
    if not split.hostname:
        return False
    if split.scheme == "https":
        return True
    return split.scheme == "http" and split.hostname in LOOPBACK_HOSTS


@dataclass(frozen=True, slots=True)
class ModelProviderConfig:
    """One vendor a deployment may reach, and where its key is read from.

    ``api_key_env`` is the **name** of an environment variable, exactly as a
    sign-in provider's ``client_secret_env`` is: keys are the operator's, read
    once at start-up, never stored in the database, never logged and never
    sent to the browser (``docs/specs/agents.md``).
    """

    id: str
    """The platform's own id for this provider, which a model refers to."""
    kind: ProviderKind
    api_key_env: str
    """The name of the environment variable the key is read from."""
    base_url: str | None = None
    """Where the endpoint lives; ``None`` for a kind that names a vendor.

    Required for every kind in ``KINDS_WITH_BASE_URL`` and refused for the
    rest, because a base URL for a vendor with one endpoint is either a
    mistake or a way to send the operator's key somewhere else.

    **It is a prefix, not a path**: the client appends the vendor protocol's
    own path to it, so an ``anthropic-compatible`` provider at OpenRouter is
    written ``https://openrouter.ai/api`` and the request goes to
    ``https://openrouter.ai/api/v1/messages`` (``docs/specs/agents.md``).
    """

    def __post_init__(self) -> None:
        checked_config_id(self.id, "a model provider's id")
        if not isinstance(self.kind, ProviderKind):
            raise InvalidValueError(
                f"a model provider's kind is a ProviderKind, not {describe(self.kind)}"
            )
        checked_line(self.api_key_env, "a model provider's api_key_env", MAX_ENV_NAME_CHARS)
        if not is_env_name(self.api_key_env):
            raise InvalidValueError(
                f"api_key_env is the NAME of an environment variable holding the key,"
                f" not {describe(self.api_key_env)}"
            )
        if self.kind in KINDS_WITH_BASE_URL:
            if not self.base_url:
                raise InvalidValueError(
                    f"a provider of kind {self.kind.value} needs its base_url: there"
                    f" is no endpoint to guess"
                )
            checked_line(self.base_url, "a model provider's base_url", MAX_BASE_URL_CHARS)
            if not is_endpoint_url(self.base_url):
                # The value is not echoed: it may hold the very credential
                # this refuses, and a refusal is not a place to print one.
                raise InvalidValueError(
                    "base_url is an https:// endpoint (http:// only on the loopback"
                    " interface), with no query, no fragment and no user:password in it"
                )
        elif self.base_url is not None:
            raise InvalidValueError(
                f"only these kinds have a base_url: {_KINDS_WITH_BASE_URL_NAMED};"
                f" {self.kind.value} has one endpoint of its own"
            )


@dataclass(frozen=True, slots=True)
class ModelConfig:
    """One model a conversation may run on: whose it is, and what it is called.

    The platform's id (``id``) and the vendor's name for it (``name``) are two
    things on purpose: an agent and a conversation refer to the platform's, so
    that changing which vendor model an id means is a line of configuration
    and not a change to every agent (``docs/specs/agents.md``).
    """

    id: str
    provider: str
    """The id of the ``ModelProviderConfig`` this model is reached through."""
    name: str
    """The vendor's own name for the model, sent to the provider as it stands."""
    timeout_seconds: float = DEFAULT_MODEL_TIMEOUT_SECONDS
    """How long one call to this model may take.

    **The turn's timeout is the outer bound**, and it is not this one: the
    application fails a turn that has taken longer than ``turn_seconds``
    whatever the provider is doing (``robinauts.application.Turns``), so a
    model timeout set above it is simply never reached -- the turn ends
    first, and it ends as a turn that ran out of time rather than as a
    provider that would not answer. It is left settable past that all the
    same: the two numbers belong to different people, the bound that matters
    is enforced either way, and refusing a configuration over a number that
    can only make it stricter would be a start-up failure about nothing.
    """
    max_output_tokens: int | None = None
    """The most tokens one answer may hold; ``None`` leaves it to the engine.

    ``None`` rather than a number, because what a sensible ceiling is belongs
    to the client and moves with the vendor's models. An engine that must send
    one says what it sends.
    """
    title: str = ""
    """What a person picks it by; empty is the model's id.

    Optional in the configuration, as an agent's is, so that an operator who
    does not care is not made to write the id twice, and one who does can
    write "Claude Sonnet 5" rather than ``sonnet-via-openrouter``. The record
    fills the id in itself, so every reader finds a title and none of them
    has to know the rule.
    """

    def __post_init__(self) -> None:
        checked_config_id(self.id, "a model's id")
        checked_config_id(self.provider, "a model provider's id")
        checked_line(self.name, "a model's name", MAX_MODEL_NAME_CHARS)
        if not self.name.strip():
            raise InvalidValueError("a model has a name: what the vendor calls it")
        checked_line(self.title, "a model's title", MAX_MODEL_TITLE_CHARS)
        if not self.title:
            object.__setattr__(self, "title", self.id)
        elif not self.title.strip():
            raise InvalidValueError("a model's title is what a person picks it by, not spaces")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int | float)
            or not 0 < self.timeout_seconds <= MAX_MODEL_TIMEOUT_SECONDS
        ):
            raise InvalidValueError(
                f"a model's timeout_seconds is a number of seconds over 0 and at most"
                f" {MAX_MODEL_TIMEOUT_SECONDS:g}, not {describe(self.timeout_seconds)}"
            )
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))
        if self.max_output_tokens is not None and (
            isinstance(self.max_output_tokens, bool)
            or not isinstance(self.max_output_tokens, int)
            or not 0 < self.max_output_tokens <= MAX_OUTPUT_TOKENS
        ):
            raise InvalidValueError(
                f"a model's max_output_tokens is a whole number over 0 and at most"
                f" {MAX_OUTPUT_TOKENS}, not {describe(self.max_output_tokens)}"
            )


class ToolServerAuth(StrEnum):
    """How a tool server is sent the secret its table names (``docs/specs/agents.md``).

    ``BEARER`` is ``Authorization: Bearer <secret>``, and the default; ``BASIC``
    is ``Authorization: Basic base64(<user>:<secret>)``, where the table names
    the user part too and the secret is the token; ``NONE`` sends no credential
    at all -- a public server, which names no variable and is sent no header.
    The values are the ones the configuration is written with.
    """

    BEARER = "bearer"
    BASIC = "basic"
    NONE = "none"


def sends_alone(auth: ToolServerAuth) -> str:
    """What an auth that has no user part sends instead, for a refusal to say."""
    return (
        "none sends no credential"
        if auth is ToolServerAuth.NONE
        else f"{auth.value} sends the secret alone"
    )


DEFAULT_TOOL_TIMEOUT_SECONDS = 60.0
"""How long one call to a tool may take when the configuration does not say.

Per **tool call**, as a model's is per model call, and for the same reason
shorter than the turn: a tool that hangs is the tool's failure, reported as
a result the model reads (``docs/specs/agents.md``, "Tools"), and not the
turn running out of time.
"""

MAX_TOOL_TIMEOUT_SECONDS = MAX_MODEL_TIMEOUT_SECONDS
"""Past this a timeout is not a timeout, for a tool as for a model."""

MAX_BASIC_USER_CHARS = 200
"""The longest the user part of a ``basic`` credential may be."""


@dataclass(frozen=True, slots=True)
class ToolServerConfig:
    """One remote MCP server a deployment may reach, and how (``docs/specs/agents.md``).

    ``url`` is checked as every configured endpoint is (``is_endpoint_url``):
    the secret travels to it. ``secret_env`` is the **name** of an environment
    variable, exactly as a model provider's ``api_key_env`` is: the secret is
    the operator's, read once at start-up, never in this file, never logged --
    and empty for a server with no ``auth``, which has no secret to read.
    ``prefix`` is what the server's tools are shown to the model under
    (``<prefix>__<name>``), the server's id when the operator wrote none --
    and an id that would not do as a prefix (too long, or one a name could not
    be told apart from) is refused until one is written.
    """

    id: str
    url: str
    secret_env: str = ""
    """The name of the environment variable the secret is read from; empty for ``none``."""
    auth: ToolServerAuth = ToolServerAuth.BEARER
    user: str = ""
    """The user part of a ``basic`` credential; empty for ``bearer``, which has none."""
    prefix: str = ""
    """What this server's tools are named under for the model; empty is the id."""
    timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS
    """How long one call to one of this server's tools may take."""

    def __post_init__(self) -> None:
        checked_config_id(self.id, "a tool server's id")
        checked_line(self.url, "a tool server's url", MAX_BASE_URL_CHARS)
        if not is_endpoint_url(self.url):
            # The value is not echoed: it may hold the very credential this
            # refuses, and a refusal is not a place to print one.
            raise InvalidValueError(
                "a tool server's url is an https:// endpoint (http:// only on the loopback"
                " interface), with no query, no fragment and no user:password in it"
            )
        if not isinstance(self.auth, ToolServerAuth):
            raise InvalidValueError(
                f"a tool server's auth is a ToolServerAuth, not {describe(self.auth)}"
            )
        if self.auth is ToolServerAuth.NONE:
            if not isinstance(self.secret_env, str) or self.secret_env:
                raise InvalidValueError(
                    "a tool server with no auth names no secret_env: there is no secret to read"
                )
        else:
            checked_line(self.secret_env, "a tool server's secret_env", MAX_ENV_NAME_CHARS)
            if not is_env_name(self.secret_env):
                raise InvalidValueError(
                    f"secret_env is the NAME of an environment variable holding the secret,"
                    f" not {describe(self.secret_env)}"
                )
        checked_line(self.user, "a tool server's user", MAX_BASIC_USER_CHARS)
        if self.auth is ToolServerAuth.BASIC:
            if not self.user.strip():
                raise InvalidValueError("a tool server with basic auth names the user part: user")
            if ":" in self.user:
                raise InvalidValueError("the user part of a basic credential holds no ':'")
        elif self.user:
            raise InvalidValueError(f"only basic auth has a user part; {sends_alone(self.auth)}")
        if not self.prefix:
            if not is_tool_prefix(self.id):
                raise InvalidValueError(
                    f"tool server {self.id!r} needs a prefix written down: its id is not one"
                    f" its tools can be named under (at most {MAX_TOOL_PREFIX_CHARS} characters,"
                    f" no '__', not ending in '_')"
                )
            object.__setattr__(self, "prefix", self.id)
        checked_tool_prefix(self.prefix, "a tool server's prefix")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int | float)
            or not 0 < self.timeout_seconds <= MAX_TOOL_TIMEOUT_SECONDS
        ):
            raise InvalidValueError(
                f"a tool server's timeout_seconds is a number of seconds over 0 and at most"
                f" {MAX_TOOL_TIMEOUT_SECONDS:g}, not {describe(self.timeout_seconds)}"
            )
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))


@dataclass(frozen=True, slots=True)
class ModelsConfig:
    """The model half of a deployment's configuration: providers, models, agents, servers.

    Four tables that refer to one another, held together so that the thing
    handed to the composition root is whole: every agent names a model that is
    here and tool servers that are here, every model names a provider that is
    here, and no two servers name their tools under one prefix. ``core`` is
    what proves that of an operator's file; this record is what the proof
    produces, and a caller may look things up in it without wondering.

    Empty is a deployment with no agents, which is allowed and starts: the
    picker has nothing in it and ``/api/agents`` is empty
    (``docs/working-notes/poc-scope.md``).
    """

    providers: Mapping[str, ModelProviderConfig] = field(default_factory=dict)
    models: Mapping[str, ModelConfig] = field(default_factory=dict)
    agents: Mapping[str, AgentDefinition] = field(default_factory=dict)
    tool_servers: Mapping[str, ToolServerConfig] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for where, table, wanted in (
            ("providers", self.providers, ModelProviderConfig),
            ("models", self.models, ModelConfig),
            ("agents", self.agents, AgentDefinition),
            ("tool_servers", self.tool_servers, ToolServerConfig),
        ):
            for key, value in table.items():
                if not isinstance(value, wanted) or value.id != key:
                    raise InvalidValueError(f"the entry under {where}.{key} is not that entry")
            object.__setattr__(self, where, MappingProxyType(dict(table)))
        for model in self.models.values():
            if model.provider not in self.providers:
                raise InvalidValueError(
                    f"model {model.id!r} is reached through provider {model.provider!r},"
                    f" which is not configured"
                )
        for agent in self.agents.values():
            if agent.model not in self.models:
                raise InvalidValueError(
                    f"agent {agent.id!r} runs on model {agent.model!r}, which is not" f" configured"
                )
            for server_id in agent.tools:
                if server_id not in self.tool_servers:
                    raise InvalidValueError(
                        f"agent {agent.id!r} uses tool server {server_id!r}, which is not"
                        f" configured"
                    )
        prefixes: dict[str, str] = {}
        for server in self.tool_servers.values():
            other = prefixes.setdefault(server.prefix, server.id)
            if other != server.id:
                raise InvalidValueError(
                    f"tool servers {other!r} and {server.id!r} would both name their tools"
                    f" under {server.prefix!r}: give one a prefix of its own"
                )

    def model_by_id(self, model_id: str) -> ModelConfig:
        """The model of that id; ``UnknownModelError`` if this deployment has none.

        This can miss, though the configuration is whole: the id is one a
        conversation carries (``docs/specs/agents.md``), chosen by a person or
        copied from an agent's default when the conversation started, and the
        operator may have removed that model since. That is refused, rather
        than answered by some other model.
        """
        checked_config_id(model_id, "a model's id")
        found = self.models.get(model_id)
        if found is None:
            raise UnknownModelError(f"no model {model_id!r} is configured in this deployment")
        return found

    def provider_for(self, model: ModelConfig) -> ModelProviderConfig:
        """The provider that model is reached through."""
        return self.providers[model.provider]

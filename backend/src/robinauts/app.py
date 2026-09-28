# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Composition root: what a deployment is made of, and who owns its lifetime.

This is the one module that knows every layer at once (``docs/layout.md``,
"infrastructure"). It reads the configuration, builds the adapters and the
datastore, injects them into the application, and mounts the api. Nothing
below it constructs a collaborator, and nothing below it reads a file or an
environment variable.

Two moments, deliberately apart:

- **configuring**, which happens the moment ``create_app`` is called and needs
  no event loop: the one TOML file is read (``adapters.read_toml``, path from
  ``ROBINAUTS_CONFIG``), ``core`` turns its two halves into a ``SignInConfig``
  and a ``ModelsConfig``, the client secrets and the model providers' API keys
  are looked for, and **every problem found is reported together** in one
  ``ConfigError``. An operator with three mistakes fixes three mistakes, not
  one restart at a time (``docs/specs/operations.md``);
- **opening**, which happens in the ASGI lifespan, because that is where a
  process may hold something: the connection pool, the schema check, the
  identity provider's HTTP client. What the lifespan opens, the lifespan
  closes (``docs/specs/backend.md``, "Background work"). A deployment whose
  database is not the one this build was written against fails there, with the
  message ``domain.SchemaError`` writes, and the server does not start.

**It is testable without either.** Every collaborator may be handed in --
``Deployment`` takes a credential store, a conversation store, an identity
provider, a clock and a source of secrets -- so the route tests wire the in-memory fakes and the
stand-in provider and never touch a database, a file or the environment.
**Both stores or neither**: a deployment that is open has a credential store
*and* a conversation store, so handing in one of them and no database url is
refused rather than quietly leaving the other unbuilt. What
is *not* handed in is built here, and only what is built here is closed here
... with one exception, said plainly: an identity provider that holds an HTTP
client is closed whichever way it arrived, because the process holds it for
its life and a client nobody closes is a warning at shutdown.

**What the process holds includes work.** Runs execute on the loop that serves
requests (``docs/specs/backend.md``, "Background work"), so the lifespan opens
the executor that carries them and the signals their watchers wait on, wires
the three services the routes call -- ``conversations``, ``turns``, ``watch``
-- and, once the stores are open, **sweeps once**: a run still active when this
process starts was left by one that went away, and it is ended ``interrupted``
with the event that says so. Shutdown is the same in reverse and in this order:
say that the process is stopping, so that the runs its own shutdown cancels are
recorded as interrupted and not as cancelled; cancel them and wait, bounded;
then close the stores, because the last thing a cancelled run does is write its
end into one.

**There is one turn timeout**, and the deployment holds it: ``turn_seconds``
goes to the run lifecycle, which fails a turn that takes longer, and to the
watchers, which give up on a run that has stored nothing for as long. They are
the same question asked from the two sides, so a deployment that lengthens one
lengthens the other rather than discovering that the numbers were copies.

The environment is read through one ``SecretLookup`` (``adapters.environment``
by default), the same callable the client secrets are read with: a test
scripts what the environment holds by passing a mapping's ``get``, and sets no
real variable.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from importlib import resources
from pathlib import Path
from typing import Any, Protocol

from fastapi import FastAPI

from robinauts.adapters import (
    AsyncioRunExecutor,
    HttpIdentityProvider,
    MemoryRunSignals,
    OsIdSource,
    OsSecretSource,
    ProviderKeys,
    SecretLookup,
    SystemClock,
    ToolServerSecrets,
    check_api_keys,
    check_client_secrets,
    check_tool_secrets,
    environment,
    read_toml,
)

# The discard test (``docs/specs/agents.md``): deleting either agent adapter
# and its dependencies breaks its import here and its one entry in ``ENGINES``
# below -- one name, in one place -- and nothing else in the platform. What
# the root needs to know about an engine besides how to build it, it asks the
# engine (``Agent.kinds``), so that knowing more never means importing more.
# Each is named by its sub-package and not re-exported from
# ``robinauts.adapters``, so that importing the adapters does not import an
# agent framework.
from robinauts.adapters.agents.langgraph import LangGraphAgent
from robinauts.adapters.agents.pydantic_ai import PydanticAIAgent
from robinauts.adapters.tools.mcp import McpToolServers
from robinauts.api import NOT_BUILT, create_api, ui_inside
from robinauts.application import (
    DEFAULT_MAX_TOOL_ROUNDS,
    DEFAULT_TURN_SECONDS,
    DEFAULT_WAIT_SECONDS,
    ENDING_BUDGET_SECONDS,
    Conversations,
    LocalAccess,
    SignIn,
    Turns,
    Watch,
)
from robinauts.core import SIGN_IN_KEYS, parse_models_config, parse_sign_in_config
from robinauts.datastore import (
    PostgresConversationStore,
    PostgresCredentialStore,
    check_schema,
    open_pool,
)
from robinauts.domain import (
    LOCAL_PROVIDER,
    LOCAL_SUBJECT,
    AgentDefinition,
    ConfigError,
    Engine,
    InvalidValueError,
    LocalMode,
    ModelConfig,
    ModelsConfig,
    ProviderKind,
    SignInConfig,
    ToolServerConfig,
    is_loopback_bind_host,
)
from robinauts.ports import (
    Agent,
    Clock,
    ConversationStore,
    CredentialStore,
    IdentityProvider,
    SecretSource,
    ToolServers,
)

_log = logging.getLogger(__name__)

CONFIG_VARIABLE = "ROBINAUTS_CONFIG"
"""Names the TOML file describing this deployment: sign-in **and** models.

One file, two halves (``docs/specs/operations.md``). It was
``ROBINAUTS_AUTH_CONFIG`` while sign-in was all it held; that name is still
read, with a warning, and is the one thing about it that is deprecated.
"""

AUTH_CONFIG_VARIABLE = "ROBINAUTS_AUTH_CONFIG"
"""The older name of the same file. Still read, with a warning at start-up.

The file stopped being about authentication alone when it grew the model
tables (``docs/specs/agents.md``), so the variable that names it stopped being
right. A deployment that already exports the old name goes on working and is
told, once, to rename it; if both are set the new one is what is read, and
that is said too, because two names for one file are two files waiting to
disagree.
"""

OLD_CONFIG_VARIABLE = (
    "%s is the old name of %s and is what this deployment was configured from."
    " Rename the variable: the old name will not be read for ever."
)
"""Logged once, at start-up, when the deprecated name is the one in use."""

OLD_CONFIG_VARIABLE_IGNORED = (
    "%s and %s are both set; %s is what is read and the old name is ignored. Unset %s."
)
"""Logged once, at start-up, when both names are set: the new one wins."""

DATABASE_URL_VARIABLE = "ROBINAUTS_DATABASE_URL"
"""Names the one PostgreSQL of the deployment. It may hold a password."""

BOTH_MODES = (
    f"the local development mode has no sign-in: it cannot be combined with a sign-in"
    f" configuration, so take the sign-in tables out of the file {CONFIG_VARIABLE}"
    f" (or {AUTH_CONFIG_VARIABLE}) names -- in this mode its model tables are read and"
    f" nothing else -- or start without the local development mode"
)
"""Asking for both is a start-up refusal (``docs/specs/sign-in.md``).

**The file itself is not refused any more**, only sign-in inside it: the mode
needs agents to develop a chat against (``docs/specs/frontend.md``), and the
agents are in the same file as the sign-in it must not have.
"""

NO_MODE = (
    f"a deployment is either signed in to or developed on: set {CONFIG_VARIABLE} to the"
    f" TOML file describing it, or ask for the local development mode"
)
"""Neither was given. ``configured`` says it with whatever else is missing."""


class EngineAdapter(Protocol):
    """What the composition root needs of an agent adapter, and no more.

    Two things: what it can reach (``kinds``, which the port declares) and how
    to build one -- the model configuration and the providers' keys, which is
    the same constructor for every engine because it is the platform's
    configuration and not a framework's (``docs/specs/agents.md``). Written as
    a protocol rather than a base class so that an adapter satisfies it by
    being what it already is.
    """

    kinds: frozenset[ProviderKind]

    def __call__(self, models: ModelsConfig, keys: ProviderKeys) -> Agent:
        """Build the engine for this deployment's models."""
        ...  # pragma: no cover -- a structural type, never called


ENGINES: Mapping[Engine, EngineAdapter] = {
    Engine.LANGGRAPH: LangGraphAgent,
    Engine.PYDANTIC_AI: PydanticAIAgent,
}
"""The agent adapters this build constructs, one per engine it runs.

**The whole of the choice of agent framework** (``docs/layout.md``,
"infrastructure"): adding an engine is an entry here, and removing one is the
same entry and the import above. Both of the specified engines are here, which
is what makes the swap a line of configuration: an agent moved from one to the
other keeps its conversations, because the conversation record is the whole of
the state (ADR 0002) and both engines are handed the same history.

An engine this build does not construct is a start-up refusal naming what to
do (``robinauts.core.parse_models_config``) rather than a deployment that
starts and fails at that agent's first turn -- which is also what a test that
hands in its own engines gets, since those are then the deployment's whole
answer to "which engines are there".
"""

WIRED_ENGINES = frozenset(ENGINES)
"""The engines this build runs, which is what the configuration is judged against."""

BUILDABLE_KINDS = frozenset().union(*(adapter.kinds for adapter in ENGINES.values()))
"""The model providers those engines have a client for, as they report them.

**Asked, not known.** Which vendors an engine can reach is the engine's own
answer -- it depends on which client passes the dependency policy at the
version this build pins (``DEPENDENCIES.md``) -- and the root would be keeping
a copy of it that could only go stale (``robinauts.ports.Agent.kinds``).
"""

NO_AGENTS = (
    "no [agents] table: this deployment starts with no agents, so /api/agents is empty"
    " and there is nothing to pick. Add [model_providers], [models] and [agents] to the"
    " configuration file to give it one."
)
"""Logged, not raised. A deployment with no agents is a deployment
(``docs/working-notes/poc-scope.md``): everything but answering a turn works.
It is said out loud so that an empty picker is a thing somebody was told about
rather than something to guess at -- which is the whole of what the local
development mode gets when it is started with no configuration file."""

SIGN_IN_TABLES = SIGN_IN_KEYS | {"admin"}
"""The top-level keys that make a file a sign-in configuration.

``admin`` among them: it is the sign-in parser that refuses roles by name, and
in the local development mode that parser never runs, so a file carrying one
there would be a rule the operator believes is in force and is not.
"""

NO_DATABASE = f"no database: set {DATABASE_URL_VARIABLE} to the PostgreSQL this deployment uses"
"""Said wherever a store this deployment needs would have to come from one.

Both stores are the same database, and a deployment that is open has both
(``Deployment.open``), so the url is wanted unless **every** store was handed
in -- which only a test does.
"""

OFF_LOOPBACK = (
    "the local development mode serves the loopback interface only: %s is not an address of"
    " it. Serve it on 127.0.0.1, ::1 or localhost. It is not a way to deploy"
    " (docs/specs/operations.md)."
)
"""Why a bind host is refused, with the host put in by the caller.

The rule is ``domain.is_loopback_bind_host``, which is stricter than the one a
request's ``Host`` header is judged by: a name of another shape -- even
``dev.localhost`` -- is resolved by whatever this machine resolves names with,
and a bind address is not a thing to leave to a resolver."""

SHUTDOWN_SLACK_SECONDS = 5.0
"""How much longer than one run's ending a shutdown waits, for everything else.

The wait is for tasks that were cancelled together: each has its own ending to
write, and they write them at the same time, so the bound is one ending's
worth and not one per run. This is what is added for the rest of a stop --
letting an engine go, the last event of each stream, the reports of work that
never began.
"""

SHUTDOWN_SECONDS = ENDING_BUDGET_SECONDS + SHUTDOWN_SLACK_SECONDS
"""How long this deployment's shutdown may take, in seconds.

**Derived, not chosen.** A cancelled run writes its ending under a shield,
every attempt bounded and tried a few times, and the application is what says
how long all of that may be (``application.ENDING_BUDGET_SECONDS``). A
shutdown bound shorter than that would abandon writes that were about to land
-- runs left ``running`` for the next start-up sweep to find, for no reason
but two numbers that had drifted apart. The adapter cannot read the
application's numbers (``docs/layout.md``), so the composition root adds them
up and hands the result to ``aclose``, which is what it is for.
"""

LOCAL_MODE_WARNING = (
    "SIGN-IN IS OFF. This is the local development mode: it serves %s and nothing else, and"
    " every request runs as the one local user %s:%s. It is not a way to deploy"
    " (docs/specs/operations.md)."
)
"""Logged once, at start-up, because a server that asks nobody who they are
has to say so wherever it is looked at. The interface says it too, in a
permanent banner (``docs/specs/frontend.md``)."""


def packaged_ui() -> Path | None:
    """The built interface inside this installation, or ``None``.

    The wheel carries ``frontend/dist`` as ``robinauts/ui/`` -- put there at
    build time, by a hook that refuses to build a wheel without it
    (``backend/hatch_build.py``) -- so an installed Robinauts has its interface
    wherever the package landed, and `pip install` plus a PostgreSQL is a whole
    deployment (``docs/specs/operations.md``).

    ``None`` is a **development checkout**: the built files are never committed
    (``docs/specs/frontend.md``), so a source tree has none until somebody runs
    the build, and the interface is developed against Vite's own server anyway.
    The composition root says so once, at start-up, and ``/ui/`` answers a page
    that says it too -- neither of which is a reason to refuse to serve the API.

    Read through ``importlib.resources`` rather than from ``__file__``, as
    ``datastore.schema_sql`` reads its own file: it is the question "where is
    this package's data", and the import system is what answers it.
    """
    return ui_inside(Path(str(resources.files(__package__))))


def _configured_path(secret_for: SecretLookup) -> str | None:
    """The configuration file the environment names, under either variable.

    ``ROBINAUTS_CONFIG`` is the name; ``ROBINAUTS_AUTH_CONFIG`` is what it was
    called while sign-in was all the file held, and it is still read so that a
    deployment written against the old name keeps starting. Whichever way, the
    deprecation is **said at start-up**, once, in the log -- an alias that
    works silently is an alias nobody ever renames -- and the new name wins if
    both are set, because two names for one file are two files waiting to
    disagree.
    """
    path = secret_for(CONFIG_VARIABLE)
    older = secret_for(AUTH_CONFIG_VARIABLE)
    if older and path:
        _log.warning(
            OLD_CONFIG_VARIABLE_IGNORED,
            AUTH_CONFIG_VARIABLE,
            CONFIG_VARIABLE,
            CONFIG_VARIABLE,
            AUTH_CONFIG_VARIABLE,
        )
    elif older:
        _log.warning(OLD_CONFIG_VARIABLE, AUTH_CONFIG_VARIABLE, CONFIG_VARIABLE)
    return path or older


class Deployment:
    """One deployment's collaborators: how they are made, and when they go.

    ``open`` builds what is missing and returns the application's ``SignIn``;
    ``aclose`` gives back what ``open`` took. Both are called by the ASGI
    lifespan in ``create_app``, and a test may call them itself.
    """

    def __init__(
        self,
        config: SignInConfig | None = None,
        *,
        local_development_host: str | None = None,
        database_url: str | None = None,
        credentials: CredentialStore | None = None,
        conversation_store: ConversationStore | None = None,
        provider: IdentityProvider | None = None,
        clock: Clock | None = None,
        secrets: SecretSource | None = None,
        secret_for: SecretLookup = environment,
        agents: Mapping[str, AgentDefinition] | None = None,
        models: Mapping[str, ModelConfig] | None = None,
        engines: Mapping[Engine, Agent] | None = None,
        turn_seconds: float = DEFAULT_TURN_SECONDS,
        tool_secrets: ToolServerSecrets | None = None,
        tool_servers: ToolServers | None = None,
        servers: Mapping[str, ToolServerConfig] | None = None,
        max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS,
    ) -> None:
        if (config is None) == (local_development_host is None):
            raise ConfigError([BOTH_MODES] if config is not None else [NO_MODE])
        self.config = config
        """The sign-in configuration, or ``None`` in the local development mode."""
        self.local_mode = (
            None if local_development_host is None else LocalMode(host=local_development_host)
        )
        """The local development mode, or ``None`` in a deployment.

        Built here, which is where a non-loopback bind host is refused: the
        root binds no socket -- the command that starts the server does -- so
        the rule is kept at the one moment that is certain to happen, before
        anything is opened and long before anything is served.
        """
        self.database_url = database_url
        self.turn_seconds = turn_seconds
        """How long one turn of this deployment may take, in seconds.

        **One number, given to both sides of the same question**: the run
        lifecycle fails a turn that takes longer than this
        (``Turns(turn_seconds=...)``), and a watcher gives up on a run that has
        stored nothing for as long as it (``Watch(quiet_seconds=...)``). Two
        of them would be a deployment that lengthened a turn and then gave up
        on watching one while it was still allowed to answer, or the other way
        about -- so a deployment that lengthens one lengthens the other. It is
        checked where it is used, by the services that are built with it.
        """
        self.sign_in: SignIn | None = None
        """The application's sign-in, between ``open`` and ``aclose``."""
        self.local_access: LocalAccess | None = None
        """The local development mode's one user, between ``open`` and ``aclose``."""
        self.conversation_store: ConversationStore | None = None
        """Conversations, messages, runs and their events, between ``open`` and ``aclose``.

        Built here on the same pool as the credential store, because it is the
        same database. It is exposed so that a deployment, and a test, can see
        what the process holds; what is built **on** it is below.
        """
        self.conversations: Conversations | None = None
        """Listing, opening, renaming and deleting conversations."""
        self.turns: Turns | None = None
        """The run lifecycle: beginning a turn, executing it, cancelling it."""
        self.watch: Watch | None = None
        """A run's events, from a position, for whoever may see them.

        The three services are what the routes of the next step call, and they
        are here because this is the only place that may build them
        (``docs/layout.md``): ``turns.begin`` / ``begin_again`` to start a
        turn, ``turns.cancel`` to stop one, ``conversations.open`` to be told
        where to attach, and ``watch.events`` to follow it from there.
        """
        self.pool: Any = None
        """The connection pool this opened, if it opened one; ``None`` after.

        Untyped on purpose: the driver is confined to ``robinauts.datastore``
        (``docs/layout.md``), so nothing here may name ``asyncpg.Pool``, not
        even in an annotation -- the contract is about what a module imports,
        and an annotation is an import. It is here so that an operator, a
        command and a test can see what the process is holding.
        """
        self._credentials = credentials
        self._conversation_store = conversation_store
        self._provider = provider
        self._clock = clock or SystemClock()
        self._secrets = secrets or OsSecretSource()
        self._secret_for = secret_for
        self.tool_secrets = tool_secrets if tool_secrets is not None else ToolServerSecrets({})
        """The tool servers' secrets, as start-up read them (``check_tool_secrets``).

        Read at start-up so that a deployment refuses to start with one
        unset, naming every one; handed to the MCP adapter ``open`` builds. A
        deployment built without them has none to hand over.
        """
        self._tool_servers = tool_servers
        """What reaches the tool servers, if a test handed one in; else ``open``
        builds the MCP adapter.
        """
        self._servers = dict(servers or {})
        """The tool servers the configuration names, by id (``[mcp_servers.*]``)."""
        self._max_tool_rounds = max_tool_rounds
        self._agents = dict(agents or {})
        """The agents this deployment offers, as the operator defined them.

        ``configured`` reads them out of the ``[agents]`` table of the
        configuration file; handing them in is how a test names its own. Empty
        is a deployment that can do everything but answer a turn, which is
        allowed and is what a file with no agents -- and the local development
        mode, which has no file -- describes.
        """
        self._models = dict(models or {})
        """The models a conversation of this deployment may run on.

        ``configured`` reads them out of the ``[models]`` table, likewise;
        handing them in is how a test whose agents run on its own engine names
        the models those agents' conversations run on. Every agent's default
        is one of them, which the run lifecycle checks when it is built.
        """
        self._engines = dict(engines or {})
        """The engine of each kind this deployment runs, likewise from ``configured``.

        This is the one place the choice of agent framework is made
        (``docs/layout.md``, "infrastructure"), and handing one in is how a
        test runs a turn over a scripted engine and reaches no provider."""
        self._executor = AsyncioRunExecutor()
        """Where a run's work happens: tasks on the loop that serves requests."""
        self._signals = MemoryRunSignals()
        """How a watcher hears that a run has stored something new."""
        self._ids = OsIdSource()
        self._closing: list[Callable[[], Awaitable[object]]] = []
        self._opened = False

    @classmethod
    def configured(
        cls,
        *,
        config_path: str | os.PathLike[str] | None = None,
        local_development_host: str | None = None,
        database_url: str | None = None,
        secret_for: SecretLookup = environment,
        credentials: CredentialStore | None = None,
        conversation_store: ConversationStore | None = None,
        provider: IdentityProvider | None = None,
        clock: Clock | None = None,
        secrets: SecretSource | None = None,
        agents: Mapping[str, AgentDefinition] | None = None,
        models: Mapping[str, ModelConfig] | None = None,
        engines: Mapping[Engine, Agent] | None = None,
        turn_seconds: float = DEFAULT_TURN_SECONDS,
        tool_servers: ToolServers | None = None,
        servers: Mapping[str, ToolServerConfig] | None = None,
        max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS,
    ) -> Deployment:
        """Read the configuration and refuse, once, with everything wrong with it.

        ``config_path`` and ``database_url`` fall back to the environment.
        Every problem this can see goes in one ``ConfigError``: a file that
        cannot be read, a configuration ``core`` will not accept, a client
        secret whose variable is unset, a database that was not named. A file
        that does not parse means there is no configuration to check secrets
        against, so those problems are simply not among them.

        ``local_development_host`` asks for the **local development mode**, and
        is the address the server will be served on -- the mode is loopback
        only, and this is where that is refused, beside every other start-up
        problem. It is **never** read from the environment and there is no
        variable that switches it on: a mode that signs nobody in is asked for
        in the command that starts the server and nowhere else, so that
        nothing a process inherits -- a stale export, a unit file, a container
        image -- can turn sign-in off in a deployment.

        **One file, two halves.** The same TOML holds the sign-in tables and
        the model tables (``docs/specs/agents.md``), and which halves are read
        is what the mode decides:

        - **signed in to**: the file is required and both halves are read.
          Each parser is handed the whole of it and reads its own share, so a
          mistake in either lands in the one list of problems;
        - **developed on**: the file is **optional**, and only the model
          tables are read. A file that also holds sign-in tables is refused
          (``BOTH_MODES``) -- the mode exists because there is nothing to sign
          in to -- but the file itself is welcome, because the chat is
          developed in this mode and a chat needs an agent
          (``docs/specs/frontend.md``).

        Either way, a file with no ``[agents]`` table is a deployment with
        **no agents**: it starts, ``/api/agents`` is empty, and the log says
        so. What is built out of the model half -- the agents, and the engine
        that runs them -- is built here unless the caller handed its own in,
        which is how the tests wire a scripted engine and reach no provider.
        """
        problems: list[str] = []
        path = config_path if config_path is not None else _configured_path(secret_for)
        url = database_url if database_url is not None else secret_for(DATABASE_URL_VARIABLE)
        if (credentials is None or conversation_store is None) and not url:
            problems.append(NO_DATABASE)
        local = local_development_host is not None
        config: SignInConfig | None = None
        configured_models = ModelsConfig()
        if local and not is_loopback_bind_host(local_development_host):
            problems.append(OFF_LOOPBACK % (local_development_host,))
        if not path:
            if not local:
                problems.append(
                    f"no configuration: set {CONFIG_VARIABLE} to the TOML file describing"
                    f" this deployment"
                )
        else:
            # A file that does not parse stops here: there is nothing to judge,
            # and every other check would only say so again in its own words.
            try:
                data = read_toml(path)
            except ConfigError as exc:
                problems.extend(exc.problems)
            else:
                if not local:
                    try:
                        config = parse_sign_in_config(data)
                    except ConfigError as exc:
                        problems.extend(exc.problems)
                elif any(key in data for key in SIGN_IN_TABLES):
                    # Read no further: the sign-in half of this file was
                    # written for a deployment, and this is not one.
                    problems.append(BOTH_MODES)
                    data = {}
                try:
                    configured_models = parse_models_config(
                        data,
                        # What this deployment can actually build, which core
                        # cannot know: the engines wired below, and the
                        # provider kinds the engine has a client for.
                        engines=frozenset(engines) if engines is not None else WIRED_ENGINES,
                        kinds=BUILDABLE_KINDS,
                    )
                except ConfigError as exc:
                    problems.extend(exc.problems)
        if config is not None:
            try:
                check_client_secrets(config, secret_for=secret_for)
            except ConfigError as exc:
                problems.extend(exc.problems)
        keys: ProviderKeys | None = None
        try:
            keys = check_api_keys(configured_models, secret_for=secret_for)
        except ConfigError as exc:
            problems.extend(exc.problems)
        tool_secrets: ToolServerSecrets | None = None
        try:
            tool_secrets = check_tool_secrets(configured_models, secret_for=secret_for)
        except ConfigError as exc:
            problems.extend(exc.problems)
        if agents is not None and engines is None:
            # Agents handed in, engines not: they will be run by the engines
            # built below, out of the model configuration read above. So each
            # must name an engine this build constructs and a model that
            # configuration has -- said here rather than found out in the
            # middle of somebody's turn, when the engine looks the model up
            # and there is nothing there, or at `open`, where the same mistake
            # would arrive as a different kind of error.
            problems.extend(
                f"the agent {agent_id!r} that was handed in runs on the"
                f" {definition.engine.value} engine, which this build does not"
                f" construct: hand in the engine that is to run the agent"
                for agent_id, definition in agents.items()
                if definition.engine not in ENGINES
            )
            problems.extend(
                f"the agent {agent_id!r} that was handed in runs on model"
                f" {definition.model!r}, which is not in the configuration: configure the"
                f" model, or hand in the engine that is to run the agent"
                for agent_id, definition in agents.items()
                if definition.model not in configured_models.models
            )
        if models is not None and engines is None:
            # The engines built below reach the configuration's models and no
            # others, so a model handed in beside them would be offered and
            # then fail in the middle of somebody's turn.
            problems.append(
                "models were handed in without the engines to run them: the engines this"
                " build constructs reach the configuration's models, so configure them,"
                " or hand in the engines as well"
            )
        if servers is not None and tool_servers is None:
            # The same rule for tools: the adapter `open` builds holds the
            # secrets start-up read for the **file's** servers, so a server
            # handed in beside it would be reached with no credential -- and
            # found out in the middle of somebody's turn.
            problems.append(
                "tool servers were handed in without the port that reaches them: the"
                " adapter this build constructs holds the secrets of the configuration's"
                " servers, so configure them, or hand in the tool servers as well"
            )
        wired_servers = servers if servers is not None else configured_models.tool_servers
        if agents is not None:
            # An agent's servers are checked against what will be wired, as
            # the parser checks the file's agents against the file's servers:
            # `Turns` refuses the pair at `open` otherwise, which is the wrong
            # moment and the wrong kind of error.
            problems.extend(
                f"the agent {agent_id!r} that was handed in uses tool server"
                f" {server_id!r}, which is not configured: configure the server, or"
                f" hand it in beside the agent"
                for agent_id, definition in agents.items()
                for server_id in definition.tools
                if server_id not in wired_servers
            )
        if engines is not None:
            # Whatever runs them, a conversation runs on a model this
            # deployment offers, and one begun on an agent's default would be
            # refused at its first turn if the default were not among them.
            offered = models if models is not None else configured_models.models
            problems.extend(
                f"the agent {agent_id!r} starts its conversations on model"
                f" {definition.model!r}, which this deployment does not offer: configure"
                f" the model, or hand it in beside the agent"
                for agent_id, definition in (
                    agents if agents is not None else configured_models.agents
                ).items()
                if definition.model not in offered
            )
        if problems or (config is None and local_development_host is None):
            raise ConfigError(problems)
        # Every failure above is a problem, and we raised.
        assert keys is not None and tool_secrets is not None
        if not configured_models.agents:
            _log.info(NO_AGENTS)
        return cls(
            config,
            local_development_host=local_development_host,
            database_url=url,
            credentials=credentials,
            conversation_store=conversation_store,
            provider=provider,
            clock=clock,
            secrets=secrets,
            secret_for=secret_for,
            agents=agents if agents is not None else configured_models.agents,
            models=models if models is not None else configured_models.models,
            # Built whether or not an agent uses it: it holds nothing -- a
            # graph and a client are made per turn -- and constructing it is
            # what turns hosted tracing off for this process, which is true of
            # every deployment and not only of one that answers.
            engines=(
                engines
                if engines is not None
                else {name: adapter(configured_models, keys) for name, adapter in ENGINES.items()}
            ),
            turn_seconds=turn_seconds,
            tool_secrets=tool_secrets,
            # The port a test hands in stands where the MCP adapter would;
            # the servers it names stand for the configuration's, as the
            # agents and the models handed in do.
            tool_servers=tool_servers,
            servers=wired_servers,
            max_tool_rounds=max_tool_rounds,
        )

    async def open(self) -> SignIn | None:
        """Open what the process holds, and wire the application on top of it.

        ``None`` in the local development mode, which has no sign-in to
        return: what it wires instead is ``local_access``, the one user every
        request runs as. Either way the pool is opened and the schema checked
        first, and either way the agents of the configuration -- if it was
        given one -- are wired into ``turns`` and listed by ``/api/agents``:
        the mode changes **who is asking** and nothing else about the
        platform, which is what makes a chat developed there the same chat.

        The pool is opened and the schema checked before anything is built on
        them: a database of another version is a deployment that does not
        start (``docs/specs/backend.md``). Anything that fails part way gives
        back what it already took -- there is no half-open deployment.

        **Once.** A second call would open a second pool and a second client
        over the first, and the first pair would be unreachable and never
        closed: connections held for the life of the process against a
        deployment that believes it has one set. One ``Deployment`` is one
        process's worth of collaborators, opened by the lifespan, and a second
        call is a mistake to say so about rather than to carry out.
        """
        if self._opened:
            raise InvalidValueError(
                "this deployment has been opened already; build another one rather than"
                " opening this one twice"
            )
        self._opened = True
        try:
            credentials = self._credentials
            self.conversation_store = self._conversation_store
            # **A deployment that is open has both stores.** Either is
            # injectable and neither is optional: one of them missing would be
            # a process that starts, serves, and fails on the first request
            # that needs it. So a pool is opened whenever either is still
            # missing, and whatever is still missing is built on it.
            if credentials is None or self.conversation_store is None:
                if not self.database_url:  # pragma: no cover -- `configured` refuses first
                    raise ConfigError([NO_DATABASE])
                self.pool = await open_pool(self.database_url)
                self._closing.append(self._closed_pool)
                # One check for the whole schema: `check_schema` looks for
                # every table of `schema.sql`, the conversation and run tables
                # among them, so a database made before they existed is a
                # deployment that does not start.
                await check_schema(self.pool)
                if credentials is None:
                    credentials = PostgresCredentialStore(self.pool)
                if self.conversation_store is None:
                    self.conversation_store = PostgresConversationStore(self.pool)
            # The services, in both modes: the local development mode changes
            # who is asking and nothing about conversations or runs.
            self.conversations = Conversations(store=self.conversation_store, clock=self._clock)
            # The one thing that reaches a tool server, built here so that its
            # connection pool is closed with the rest (``aclose``); a test
            # hands in the fake instead.
            tool_servers = self._tool_servers
            if tool_servers is None:
                built = McpToolServers(self.tool_secrets)
                self._closing.append(built.aclose)
                tool_servers = built
            self.turns = Turns(
                store=self.conversation_store,
                clock=self._clock,
                ids=self._ids,
                agents=self._agents,
                models=self._models,
                engines=self._engines,
                executor=self._executor,
                signals=self._signals,
                turn_seconds=self.turn_seconds,
                tool_servers=tool_servers,
                servers=self._servers,
                max_tool_rounds=self._max_tool_rounds,
            )
            self.watch = Watch(
                store=self.conversation_store,
                signals=self._signals,
                # Never longer than the silence it gives up after, which a
                # deployment with a short turn timeout would otherwise be
                # (``application.DEFAULT_WAIT_SECONDS``).
                wait_seconds=min(DEFAULT_WAIT_SECONDS, self.turn_seconds),
                quiet_seconds=self.turn_seconds,
            )
            # From here the process may hold work, so it has something to give
            # back before the pool goes: the closers are popped in reverse, and
            # this one was appended after the pool's.
            self._closing.append(self._closed_executor)
            await self._swept()
            if self.local_mode is not None:
                # No identity provider is built: there is nobody to talk to,
                # and an HTTP client nothing uses is a client to close.
                self.local_access = LocalAccess(
                    self.local_mode, credentials=credentials, clock=self._clock
                )
                _log.warning(
                    LOCAL_MODE_WARNING, self.local_mode.host, LOCAL_PROVIDER, LOCAL_SUBJECT
                )
                return None
            assert self.config is not None  # one of the two, decided in __init__
            provider = self._provider or HttpIdentityProvider(secret_for=self._secret_for)
            closer = getattr(provider, "aclose", None)
            if callable(closer):
                self._closing.append(closer)
            self.sign_in = SignIn(
                self.config,
                credentials=credentials,
                provider=provider,
                clock=self._clock,
                secrets=self._secrets,
            )
            return self.sign_in
        except BaseException:
            await self.aclose()
            raise

    async def _swept(self) -> None:
        """End the runs a process that went away left going. Once, at start-up.

        The POC is one process (``docs/working-notes/poc-scope.md``), so a run
        that is still ``running`` when this one starts is a run whose process
        is gone: it is marked ``interrupted``, with the event that ends it, so
        that its conversation is not blocked behind it and a watcher of it is
        told it is over (``docs/specs/runs.md``).

        **It does not stop the deployment.** The schema has just been checked,
        so a sweep that fails is a store that went away between two calls, and
        a process that refused to start over housekeeping would be a
        deployment down for a reason nobody asked about. It is logged, and the
        next start-up sweeps again.
        """
        assert self.turns is not None  # built a few lines above
        try:
            swept = await self.turns.sweep_interrupted()
        except Exception:
            _log.exception(
                "the start-up sweep could not end the runs left by a process that went"
                " away; they stay active until a later start-up sweeps them"
            )
            return
        _log.info(
            "the start-up sweep ended %d run(s) left going by a process that went away",
            len(swept),
        )

    async def _closed_executor(self) -> None:
        """Stop the work this process is carrying, under a bound.

        Every run still going is cancelled and writes that it was
        ``interrupted`` -- ``Turns.stopping`` has already said that this is a
        process going away rather than somebody cancelling. Nothing is
        drained, and what has not stopped when the bound (``SHUTDOWN_SECONDS``,
        added up from the application's own ending budget so that there is one
        number and not two that drift) passes is abandoned
        (``docs/working-notes/poc-scope.md``). Work the close gave up on
        before it ever began is reported to the application here too, while
        the stores are still open, and inside that same bound.
        """
        await self._executor.aclose(timeout=SHUTDOWN_SECONDS)

    async def _closed_pool(self) -> None:
        """Close the pool and forget it, so that nothing reaches a shut one."""
        pool, self.pool = self.pool, None
        await pool.close()

    async def aclose(self) -> None:
        """Close everything ``open`` opened, in reverse, whatever any of it does.

        A close that fails is logged and the next one still runs: at shutdown
        there is nothing left to protect by stopping, and a pool left open
        because a client would not close is the worse of the two.

        Idempotent, and safe on a deployment that was never opened: each
        closer is taken off the list before it is called, so a second call has
        nothing left to do. A lifespan that unwinds twice -- and a test that
        closes what a ``finally`` has closed -- is then not a second close of
        anything.

        **In order**: this process says it is stopping, so that every run its
        shutdown cancels is recorded as ``interrupted`` rather than
        ``cancelled``; then nothing can be reached to start anything new; then
        the work in flight is cancelled and waited for, under a bound; and
        only then are the stores let go of -- because the last thing a
        cancelled run does is write its end into one.
        """
        if self.turns is not None:
            self.turns.stopping()
        self.sign_in = None
        self.local_access = None
        self.conversations = None
        self.turns = None
        self.watch = None
        # The stores hold nothing of their own -- the pool is what is closed,
        # below -- so letting go of them is forgetting them.
        self.conversation_store = None
        while self._closing:
            close = self._closing.pop()
            try:
                await close()
            except Exception:
                _log.exception("closing %r failed", close)


def create_app(
    *,
    config_path: str | os.PathLike[str] | None = None,
    local_development_host: str | None = None,
    database_url: str | None = None,
    secret_for: SecretLookup = environment,
    credentials: CredentialStore | None = None,
    conversation_store: ConversationStore | None = None,
    provider: IdentityProvider | None = None,
    clock: Clock | None = None,
    secrets: SecretSource | None = None,
    agents: Mapping[str, AgentDefinition] | None = None,
    models: Mapping[str, ModelConfig] | None = None,
    engines: Mapping[Engine, Agent] | None = None,
    turn_seconds: float = DEFAULT_TURN_SECONDS,
    ui_dir: Path | None = None,
) -> FastAPI:
    """The whole deployment as one ASGI application.

    ``ConfigError`` straight away if the configuration cannot be used, so that
    ``robinauts start`` fails before it binds a port. Everything that needs a
    running loop -- the pool, the schema check, the HTTP client -- happens in
    the lifespan, which an ASGI server runs before the first request and
    unwinds after the last.

    The collaborators are the arguments: pass **both** stores and no database
    is opened, pass an identity provider and none is built. Both stores,
    because a deployment that is open has both: one of them handed in and no
    database url is a start-up ``ConfigError``. That is
    how the tests wire fakes and a stand-in provider into the real application
    (``docs/layout.md``, "Testing strategy").

    ``local_development_host`` is how the **local development mode** is asked
    for, and the address it will be served on. It is never the default and no
    environment variable turns it on; a later step gives the command a flag
    (``--dev-no-sign-in``) that passes the host it is about to bind. In that
    mode ``config_path`` (``ROBINAUTS_CONFIG``) is **optional** and only its
    model tables are read: with one, the deployment has the agents it names
    and the chat can be developed against a real engine; without one, it
    starts with none. A file that also holds sign-in tables is refused there,
    because the mode exists where there is nothing to sign in to.

    ``ui_dir`` is the built interface to serve under ``/ui/``; left out, it is
    the one inside this installation (``packaged_ui``). A checkout that has not
    been built has none, which is said here once and answered on the page
    itself -- an API that refused to start because nobody had run ``npm`` would
    be a deployment down over a screen.
    """
    served = packaged_ui() if ui_dir is None else ui_dir
    if served is None:
        _log.warning(NOT_BUILT)
    deployment = Deployment.configured(
        config_path=config_path,
        local_development_host=local_development_host,
        database_url=database_url,
        secret_for=secret_for,
        credentials=credentials,
        conversation_store=conversation_store,
        provider=provider,
        clock=clock,
        secrets=secrets,
        agents=agents,
        models=models,
        engines=engines,
        turn_seconds=turn_seconds,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await deployment.open()
        app.state.sign_in = deployment.sign_in
        app.state.local = deployment.local_access
        # The conversation and streaming routes read these off the state at the
        # moment of a request, exactly as the auth routes read the sign-in, so
        # they are put there the moment they exist and taken away with
        # everything else.
        app.state.conversations = deployment.conversations
        app.state.turns = deployment.turns
        app.state.watch = deployment.watch
        try:
            yield
        finally:
            app.state.sign_in = None
            app.state.local = None
            app.state.conversations = None
            app.state.turns = None
            app.state.watch = None
            await deployment.aclose()

    app = create_api(lifespan=lifespan, ui_dir=served)
    app.state.deployment = deployment
    return app

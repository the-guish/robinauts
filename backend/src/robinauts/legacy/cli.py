# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The ``robinauts`` command: start the server, and create the schema.

Infrastructure (``docs/layout.md``): it may know every layer, and nothing
knows it. It reads the arguments, turns on logging, builds the one application
``robinauts.legacy.app`` describes and hands it to a server -- and it decides nothing
a deployment would want to argue with, because everything a deployment is
configured with is in the file ``ROBINAUTS_CONFIG`` names
(``docs/specs/operations.md``).

Three commands, which is the whole of what a deployment needs
(``docs/specs/backend.md``):

- ``robinauts start`` serves the API and the built interface on one port;
- ``robinauts db init`` creates the schema in an empty database. **The server
  never does this**: it refuses to start against a database that is not the one
  this build was written against, and names this command;
- ``robinauts version`` says which build this is and which schema it wants,
  which is what an upgrade is decided by.

``argparse`` from the standard library, and no dependency for a command line.

**Exit codes.** ``0`` when the command did what it was asked; ``2`` when the
command line or the configuration was wrong -- the code ``argparse`` already
uses for a usage error, so that "you have to change something before trying
again" is one code and not two; ``1`` when what was asked could not be done.
A ``ConfigError`` carries every problem at once (``docs/specs/operations.md``)
and is printed one problem per line, with no traceback: an operator with three
mistakes fixes three mistakes.

**Logging is configured here and nowhere else.** A library that configured it
would be deciding for whoever imported it; a command is the one program that
may. The root logger gets one handler on **stdout** -- a server's log is its
output, not its error channel -- and a plain format. Levels that a module set
for itself are left alone, which is what keeps the vendor SDK loggers the
engines quietened (``adapters.agents.*.quiet_client_loggers``) quiet: this sets
the level of the **root** logger, and a logger with a level of its own is not
lowered by it.

**The socket is bound here**, when ``--uds`` asks for a unix one. Uvicorn
would bind it too, and chmod it to ``0o666`` -- every account on the machine,
which for a deployment running without sign-in is every account's Robinauts.
So ``bound_socket`` makes it, narrows it to ``--uds-mode`` (``0o600`` by
default) before it listens, and hands it over already listening; the path is
removed on the way out.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import socket
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from importlib import metadata
from pathlib import Path
from typing import Any

import uvicorn

from robinauts.legacy.adapters import environment
from robinauts.legacy.app import DATABASE_URL_VARIABLE, NO_DATABASE, create_app
from robinauts.legacy.datastore import SCHEMA_SHA256, SCHEMA_VERSION, create_schema, open_pool
from robinauts.legacy.domain import ConfigError, RobinautsError

_log = logging.getLogger(__name__)

PROGRAM = "robinauts"
"""What the console script is called, and what a message is prefixed with."""

DISTRIBUTION = "robinauts"
"""The installed distribution whose version ``robinauts version`` reports."""

OK = 0
FAILED = 1
MISUSED = 2
"""The three answers. ``MISUSED`` is ``argparse``'s own code for a usage error,
and is also what a configuration that cannot be used gets: both are "change
something and run it again"."""

DEFAULT_HOST = "127.0.0.1"
"""The loopback interface, because a deployment is behind a reverse proxy.

Robinauts is served at the root of an origin over https, and the TLS is
terminated in front of it (``docs/specs/operations.md``). Binding every
interface by default would put an http server carrying session cookies on the
network of whoever forgot to say otherwise; a deployment that really wants that
says ``--host``.
"""

DEFAULT_PORT = 8000

MAX_PORT = 65535

ADDRESS_AND_SOCKET = (
    "--uds is bound instead of an address, so it cannot be given with --host or"
    " --port: choose the unix socket or the TCP address this is served on"
)
"""Both were given. Refused rather than one of them quietly ignored.

uvicorn binds the socket and leaves the address alone, so a deployment that
asked for both would be served somewhere it had not read about -- and, in the
local development mode, would have been let past the loopback rule by an
address nothing ever bound.
"""

NO_SOCKET_PATH = "--uds needs the path of the unix socket to bind"
"""``--uds ''`` is not "no socket": it is a path that names nothing."""

SOCKET_TAKEN = (
    "there is already something there. This command binds a socket of its own and"
    " will not write over a file it did not make: remove it if no Robinauts is"
    " serving on it, or serve on another path."
)
"""A path that is already there. A stale socket is the usual reason, and a
socket somebody else is serving on is the reason not to guess: replacing it
would take their callers without their knowing. The path itself is put in
front of this by ``not_bound``, which names it for every way a bind fails."""

DEFAULT_SOCKET_MODE = 0o600
"""Who may open the socket: this user, and nobody else.

**This is the whole of who may reach the server over it.** A unix socket is a
file: what a network cannot do, a file's mode and its directory's can, and
uvicorn's own answer -- it chmods a socket it created to ``0o666`` -- would
hand the API to every account on the machine. In the local development mode
that API answers as the one local user without asking anybody who they are, so
the mode is not a detail.

So the socket is **bound here**, with this mode, and handed to uvicorn already
listening; uvicorn leaves the mode of a socket it did not create alone. A
deployment whose proxy runs as another user says ``--uds-mode`` and puts the
socket in a directory only those two can enter.
"""

MAX_SOCKET_MODE = 0o777

SOCKET_BACKLOG = 2048
"""Connections waiting to be accepted, which is uvicorn's own default."""

SIGNAL_EXIT = 128
"""What a shell adds to a signal number to report a process that died of it."""

STOPPING_SIGNALS = (signal.SIGINT, signal.SIGTERM)
"""What stops a server, and what ``taken_away`` has to clean up after.

The two uvicorn captures on every platform this runs on; it re-raises whichever
it caught, which is why they are handled here at all.
"""

SERVING_ON = "serving on the unix socket %s, which mode %04o says who may open"
"""Said once, at start-up: uvicorn says where it is serving only when it bound
the socket itself, and an operator has to be told where this one is."""

SOCKET_IS_LOCAL = "127.0.0.1"
"""What a unix socket counts as when the local development mode asks.

The mode is loopback-only because nothing **on another machine** may reach it
(``docs/specs/sign-in.md``), and a unix socket satisfies that by construction:
it is a file, and a file is not on the network at all. Who may open it is the
other question, and it is answered by the socket's mode and by its directory's
-- ``DEFAULT_SOCKET_MODE`` is ``0o600``, so with the default only the user
running the server can. This is the address the root is told about; nothing
binds it, and the request protection judges a unix-socket request on the scope
it really arrives in (``robinauts.legacy.api.protection``).
"""

DEFAULT_FORWARDED_ALLOW_IPS = "127.0.0.1"
"""Whose ``X-Forwarded-*`` headers are believed: the proxy in front, and no one.

``proxy_headers`` is on, so the client address and the scheme of a request come
from those headers -- but **only** when the connection itself came from one of
these addresses. A deployment whose proxy is on another host names it here;
``*`` believes anybody, which on a port reachable from a network means anybody
can claim to be anybody, and is why it is not the default.
"""

SOCKET_FORWARDED_ALLOW_IPS = "*"
"""The default when ``--uds`` is given, and only then.

A connection over a unix socket has no address to compare with anything --
uvicorn reports the client as ``None`` -- so the default above would believe
nobody, and a deployment behind a proxy on that socket would read every request
as plain ``http`` from nowhere. There is nothing given up: the only thing that
can open the socket is whatever the mode and the directory let open it, which
with ``DEFAULT_SOCKET_MODE`` is this user alone, and that is the proxy.

**It assumes that mode.** A deployment that widens ``--uds-mode`` has widened
who may connect, and believing every one of them about who *they* are is then a
choice rather than a tautology: such a deployment says
``--forwarded-allow-ips`` for itself.
"""

GRACEFUL_SHUTDOWN_SECONDS = 10
"""How long a stop waits for the connections that are open, in seconds.

A stream is a connection that is not going to close on its own: the interface
follows a run over SSE for as long as the run lasts (``docs/specs/wire.md``), so
a shutdown that waited for every connection to end would wait for every answer
in flight -- and, if a watcher reconnected, for ever. After this, what is still
open is cancelled and the **application's** shutdown runs: that is where the
runs in flight are ended as ``interrupted`` and everything the process holds is
given back, under a bound of its own (``robinauts.legacy.app.SHUTDOWN_SECONDS``).
"""

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s %(message)s"
"""Plain text, one line, no colour: a log is read by ``journalctl`` and ``grep``."""

LOG_LEVELS = ("debug", "info", "warning", "error", "critical")

DEFAULT_LOG_LEVEL = "info"

QUERY_MARK = "?"
"""Where a logged request line is cut. See ``NoQueryStrings``."""

DEV_MODE_HELP = (
    "serve with no sign-in at all, as one fixed local user, on the loopback"
    " interface only. For developing on your own machine; it is not a way to"
    " deploy, and it refuses any --host that is not loopback"
)

SCHEMA_READY = "the database is at schema version %d (schema.sql %s)"
"""Said by ``db init`` whether it created the schema or found it already there:
the command's promise is the state of the database, not the work it did."""


class NoQueryStrings(logging.Filter):
    """Cut every access-log line at the ``?``.

    An ordinary ASGI access log writes the path **with its query string**, and
    one of this platform's paths carries credentials in one:
    ``GET /auth/callback/google?code=...&state=...`` is an authorization code
    and the state that goes with it, written to a file, on every sign-in
    (``docs/specs/sign-in.md``).

    So the query goes, everywhere and not only there. A rule that named the
    sign-in paths would be a list to keep in step with the routes, and the
    query string of every other request is of no use in a log: what the API is
    asked for is in its path and its body, and what a body holds never reaches
    a log at all. The path, the method and the status stay.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        arguments = record.args
        if isinstance(arguments, tuple) and len(arguments) > 2:
            path = arguments[2]
            if isinstance(path, str) and QUERY_MARK in path:
                cut = path.split(QUERY_MARK, 1)[0]
                record.args = (*arguments[:2], cut, *arguments[3:])
        return True


def configure_logging(level: str) -> None:
    """One handler on stdout, at ``level``, for everything that does not say more.

    ``force``: a second call replaces the handler rather than adding one, which
    is what keeps a test -- or a command run twice in one process -- from
    printing everything twice.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logging.basicConfig(level=level.upper(), handlers=[handler], force=True)
    # The access log is uvicorn's own logger; it has no level and no handler of
    # its own here, so it lands on the root handler above, in the same format.
    # Its filters are replaced rather than added to, for the same reason as
    # ``force`` above: a second call configures logging again, not twice.
    access = logging.getLogger("uvicorn.access")
    access.filters = [NoQueryStrings()]


def port(given: str) -> int:
    """A TCP port, refused by ``argparse`` rather than by the socket."""
    try:
        number = int(given)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{given!r} is not a port number") from None
    if not 1 <= number <= MAX_PORT:
        raise argparse.ArgumentTypeError(f"a port is between 1 and {MAX_PORT}, not {number}")
    return number


def mode(given: str) -> int:
    """A file mode written in octal, as ``chmod`` takes one."""
    try:
        number = int(given, 8)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{given!r} is not a file mode in octal, such as 600 or 660"
        ) from None
    if not 0 <= number <= MAX_SOCKET_MODE:
        raise argparse.ArgumentTypeError(f"a file mode is between 0 and 777, not {given!r}")
    return number


def parser() -> argparse.ArgumentParser:
    """The whole command line, so that a test can read it without running it."""
    top = argparse.ArgumentParser(
        prog=PROGRAM,
        description="Robinauts: conversational agents that play fair.",
    )
    commands = top.add_subparsers(dest="command", required=True, metavar="COMMAND")

    start = commands.add_parser("start", help="serve the API and the interface")
    # ``None`` rather than the default value, so that "not given" and "given
    # the default" are told apart: that is what makes `--uds --host 127.0.0.1`
    # a refusal rather than a silent choice between them.
    start.add_argument("--host", default=None, help=f"the address to bind ({DEFAULT_HOST})")
    start.add_argument("--port", type=port, default=None, help=f"the port to bind ({DEFAULT_PORT})")
    start.add_argument(
        "--uds",
        default=None,
        help="a unix socket to bind instead of an address, for a reverse proxy on"
        " this machine; not to be given with --host or --port",
    )
    start.add_argument(
        "--uds-mode",
        type=mode,
        default=DEFAULT_SOCKET_MODE,
        help=f"who may open that socket, in octal (default {DEFAULT_SOCKET_MODE:04o}:"
        f" this user alone). It is the whole of who may reach the server over it",
    )
    start.add_argument("--dev-no-sign-in", action="store_true", help=DEV_MODE_HELP)
    start.add_argument(
        "--forwarded-allow-ips",
        default=None,
        help=f"whose X-Forwarded-* headers to believe: the reverse proxy in front"
        f" (default {DEFAULT_FORWARDED_ALLOW_IPS}, or {SOCKET_FORWARDED_ALLOW_IPS} with"
        f" --uds, where a connection has no address and only what may open the socket"
        f" can connect at all)",
    )
    _log_level(start)

    database = commands.add_parser("db", help="the deployment's database")
    database_commands = database.add_subparsers(
        dest="database_command", required=True, metavar="COMMAND"
    )
    initialise = database_commands.add_parser(
        "init", help="create this build's schema in an empty database"
    )
    _log_level(initialise)

    commands.add_parser("version", help="the version of this build and of its schema")
    return top


def _log_level(command: argparse.ArgumentParser) -> None:
    """``--log-level`` on a command that runs long enough to say anything."""
    command.add_argument("--log-level", choices=LOG_LEVELS, default=DEFAULT_LOG_LEVEL)


def run(argv: Sequence[str] | None = None) -> int:
    """The console script. Returns this command's exit code.

    Everything a command raises that an operator can do something about is
    answered here, in one place, so that no command has to remember to: a
    ``ConfigError`` is its problems, one per line, and anything else of ours --
    a database that will not open, a schema of another version -- is one line.
    A traceback is for a bug, which is what anything else is.

    An ``OSError`` is **not** caught here. There is exactly one place one is
    expected, binding the socket the server listens on, and it is caught there
    (``served``); one from anywhere else is a file or a device behaving in a
    way this code did not allow for, which is a bug and deserves its frames.
    """
    arguments = parser().parse_args(argv)
    try:
        if arguments.command == "version":
            return version()
        configure_logging(arguments.log_level)
        if arguments.command == "start":
            return start(arguments)
        if arguments.database_command == "init":
            return database_init()
        # Unreachable while `db` has one sub-command, and written out so that
        # the day it has two, the second one is not quietly this one.
        raise ConfigError([f"there is no such command: db {arguments.database_command}"])
    except ConfigError as refused:
        for problem in refused.problems:
            print(f"{PROGRAM}: {problem}", file=sys.stderr)
        return MISUSED
    except RobinautsError as failure:
        print(f"{PROGRAM}: {failure}", file=sys.stderr)
        return FAILED
    except KeyboardInterrupt:  # pragma: no cover -- a signal, not a code path
        # uvicorn turns an interrupt into its own shutdown, so this is for an
        # interrupt anywhere else: it is not a failure of the command.
        return OK


def version() -> int:
    """This build, and the schema it was written against.

    Both, because they are the two halves of an upgrade. Until the first
    release the schema version stays 1, so the start of the ``schema.sql``
    hash is printed beside it: a new wheel whose hash differs is a database
    to recreate (``docs/specs/backend.md``, "Schema"), and the server would
    refuse the old one anyway.
    """
    print(
        f"{PROGRAM} {_installed_version()}"
        f" (schema {SCHEMA_VERSION}, schema.sql {hash_prefix()})"
    )
    return OK


def hash_prefix() -> str:
    """Enough of ``SCHEMA_SHA256`` to tell two edits of the file apart by eye."""
    return SCHEMA_SHA256[:12]


def _installed_version() -> str:
    """The distribution's version, or a word saying it is not installed.

    Running from a checkout that was never installed is a thing people do, and
    it is not a reason for this command to fail.
    """
    try:
        return metadata.version(DISTRIBUTION)
    except metadata.PackageNotFoundError:  # pragma: no cover -- always installed here
        return "unknown (not installed as a distribution)"


def start(arguments: argparse.Namespace) -> int:
    """Serve, until a signal stops it. ``FAILED`` if it never started.

    ``create_app`` reads the configuration and refuses **before** a socket is
    bound, with every problem it can see at once; what needs a running loop --
    the connection pool, the schema check, the HTTP client -- happens in the
    lifespan the server runs, which is where a database of another version
    stops the deployment with the sentence ``domain.SchemaError`` writes.

    The server is built rather than run by ``uvicorn.run``, and ``served``
    below says why: the codes this command answers with are its own.

    ``--dev-no-sign-in`` passes the address that is about to be bound to the
    composition root, which is where a non-loopback one is refused
    (``robinauts.legacy.app.OFF_LOOPBACK``): the root binds nothing, so the rule is
    kept at the one moment that is certain to happen. With ``--uds`` there is
    no address at all, and what the root is told is ``SOCKET_IS_LOCAL``.

    **The unix socket is bound here** rather than by uvicorn, which chmods one
    it created to ``0o666`` -- every account on the machine. See
    ``bound_socket`` for the binding and ``taken_away`` for the removal: a
    socket file outlives the process that made it, and the next start is
    refused by what the last one left.
    """
    if arguments.uds is not None and (arguments.host is not None or arguments.port is not None):
        raise ConfigError([ADDRESS_AND_SOCKET])
    if arguments.uds is not None and not arguments.uds:
        raise ConfigError([NO_SOCKET_PATH])
    host = arguments.host if arguments.host is not None else DEFAULT_HOST
    local = (
        (SOCKET_IS_LOCAL if arguments.uds is not None else host)
        if arguments.dev_no_sign_in
        else None
    )
    application = create_app(local_development_host=local)
    server = uvicorn.Server(
        uvicorn.Config(
            application,
            host=host,
            port=arguments.port if arguments.port is not None else DEFAULT_PORT,
            # Logging is this command's, configured above: uvicorn's own
            # dictionary config would install a second format and a second
            # handler beside it.
            log_config=None,
            access_log=True,
            # The reverse proxy in front terminates TLS, so the scheme and the
            # client address of a request are in its headers and nowhere else.
            proxy_headers=True,
            forwarded_allow_ips=believed(arguments),
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
            # Nothing is gained by telling every caller which server and which
            # version is answering.
            server_header=False,
        )
    )
    if arguments.uds is None:
        return served(server)
    try:
        listening = bound_socket(arguments.uds, arguments.uds_mode)
    except OSError as refused:
        return not_bound(arguments.uds, refused)
    with taken_away(arguments.uds):
        # Uvicorn says where it is serving only when it bound the socket
        # itself, and an operator has to be told where this one is.
        _log.info(SERVING_ON, arguments.uds, arguments.uds_mode)
        try:
            return served(server, sockets=[listening])
        finally:
            listening.close()


@contextmanager
def taken_away(path: str) -> Iterator[None]:
    """Remove the socket file however the server ends, **a signal included**.

    A socket is a file and outlives the process that bound it, and the next
    start is refused by what the last one left (``SOCKET_TAKEN``). So it has to
    go on the way out, and "the way out" of a server is almost always a signal.

    A ``finally`` is not enough for that one. Uvicorn captures ``SIGINT`` and
    ``SIGTERM``, shuts the server down, restores the handlers it found -- these
    -- and then **re-raises the signal**, so the process dies inside
    ``server.run()`` and nothing after it runs. That re-raise is exactly the
    right moment: the server has stopped and nothing is listening. So the
    handler installed here removes the file, puts back the handler that was
    there before any of this, and raises the signal again so that the process
    dies of what it was told to die of.

    The ``finally`` is for every other way out -- a start-up that failed, an
    exception, a server that stopped on its own -- and for putting the handlers
    back in a test, which runs this in a process that goes on afterwards.

    **What is removed is the file this process bound**, and nothing else: the
    inode is read once, here, and compared before every unlink
    (``the_same_file``). Otherwise a stop that took two goes, or a handler that
    ignored the first signal, could delete the socket of the server that was
    started in between.

    **And a server told to terminate terminates.** If the handler that was
    there before this one *ignores* the signal -- ``SIG_IGN``, inherited from
    whatever started the process -- the re-raise does nothing and control comes
    back here. Then the process exits with ``128 + signum`` itself, which is
    what it would have exited with, rather than going on serving out of a
    socket it has just removed.
    """
    before = {number: signal.getsignal(number) for number in STOPPING_SIGNALS}
    ours = which_file(path)

    def take_it_away(number: int, frame: Any) -> None:
        unlink_ours(path, ours)
        signal.signal(number, before[number])
        signal.raise_signal(number)
        # Still here: the previous handler ignores this signal.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(SIGNAL_EXIT + number)

    for number in STOPPING_SIGNALS:
        signal.signal(number, take_it_away)
    try:
        yield
    finally:
        for number, handler in before.items():
            signal.signal(number, handler)
        unlink_ours(path, ours)


def which_file(path: str) -> tuple[int, int, int] | None:
    """Which file is at ``path`` right now, or ``None`` if there is none.

    The device and the inode are what "the same file" means to a file system:
    a name can be unlinked and another file given the same name, and neither of
    those follows it. **And the moment it was made**, because an inode number
    that has just been freed is one a file system will hand out again straight
    away -- which is exactly what happens when one server stops and another
    starts on the same path, the case this exists for.
    """
    try:
        found = os.stat(path)
    except OSError:  # pragma: no cover -- the socket was bound a moment ago
        return None
    return (found.st_dev, found.st_ino, found.st_mtime_ns)


def unlink_ours(path: str, ours: tuple[int, int, int] | None) -> None:
    """Remove ``path``, but only while it is still the file ``ours`` names.

    A socket at that path that this process did not bind belongs to a server
    that is running, and taking its name away would leave it answering
    something nobody can reach. It is the file system that is asked, not a
    flag, because between two stops the file can have been replaced.
    """
    if ours is not None and which_file(path) == ours:
        Path(path).unlink(missing_ok=True)


def not_bound(where: str, refused: OSError) -> int:
    """What an address that could not be bound costs: one line, and ``FAILED``.

    A port already taken, an address that is not this machine's, a directory
    that is not there, a path somebody is already serving on: every one of them
    is an operator's to fix and none of them is worth a stack of frames. The
    address or the path is always named -- a deployment may be starting several
    and "permission denied" on its own says nothing about which.
    """
    print(f"{PROGRAM}: {where}: {refused.strerror or refused}", file=sys.stderr)
    return FAILED


def believed(arguments: argparse.Namespace) -> str:
    """Whose ``X-Forwarded-*`` this deployment reads; see the two constants."""
    if arguments.forwarded_allow_ips is not None:
        return str(arguments.forwarded_allow_ips)
    return SOCKET_FORWARDED_ALLOW_IPS if arguments.uds is not None else DEFAULT_FORWARDED_ALLOW_IPS


def bound_socket(path: str, permissions: int) -> socket.socket:
    """A unix socket at ``path``, listening, and openable by ``permissions`` alone.

    Bound here and handed to uvicorn already listening, because uvicorn binds
    one of its own with ``0o666`` on it -- and a Robinauts in the local
    development mode answers as its one local user without asking anybody who
    they are, so "every account on this machine" is not a mode to be given by
    default (``DEFAULT_SOCKET_MODE``). Uvicorn leaves the mode of a socket it
    was handed alone.

    The mode is set **before** the socket is listening, so there is no moment
    at which it is both reachable and open to everybody: ``bind`` creates the
    file, ``chmod`` narrows it, ``listen`` starts answering.

    A path that is already there is refused rather than replaced
    (``SOCKET_TAKEN``): it is either a socket this command left behind or one
    another server is answering on, and taking the second one's callers away
    without its knowing is not something to do by guessing. It is a
    ``FileExistsError`` and not an error of ours, because that is what it is
    and because it is answered with every other way a socket will not bind.
    """
    if Path(path).exists():
        raise FileExistsError(SOCKET_TAKEN)
    listening = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    made_it = False
    try:
        listening.bind(path)
        # **Only now** is the file at that path this process's to remove. A
        # bind that failed created nothing -- and it can fail *because another
        # server holds the path*, the check above having raced with it, in
        # which case the file there is theirs and is answering.
        made_it = True
        os.chmod(path, permissions)
        listening.listen(SOCKET_BACKLOG)
    except BaseException:
        listening.close()
        if made_it:
            Path(path).unlink(missing_ok=True)
        raise
    return listening


def served(server: uvicorn.Server, *, sockets: list[socket.socket] | None = None) -> int:
    """Run ``server`` and answer with this command's exit code, not uvicorn's.

    ``uvicorn.run`` exits the process itself when start-up fails, and
    ``Server.startup`` does the same on a lifespan that raised -- a schema of
    another version, a configuration only the lifespan can see. Both are this
    command's ``FAILED``.
    """
    try:
        server.run(sockets=sockets)
    except SystemExit:  # pragma: no cover -- uvicorn's own answer to a dead start
        return FAILED
    except OSError as refused:
        # The address could not be bound. Uvicorn answers most of these itself
        # -- it logs and exits -- and this is the rest.
        return not_bound(f"{server.config.host}:{server.config.port}", refused)
    return OK if server.started else FAILED


def database_init() -> int:
    """Create this build's schema in the configured database.

    Idempotent where it can be: a database already at this version is left
    exactly as it is. Everything else -- a schema of another version, our
    tables with no version recorded, a search path that reaches somebody else's
    -- is refused by ``datastore.create_schema`` with the sentence that says
    what to do about it, because there are no migrations yet
    (``docs/specs/backend.md``).
    """
    url = environment(DATABASE_URL_VARIABLE)
    if not url:
        raise ConfigError([NO_DATABASE])
    asyncio.run(_created(url))
    return OK


async def _created(url: str) -> None:
    """``create_schema`` on a pool opened for this one command and closed after."""
    pool = await open_pool(url)
    try:
        await create_schema(pool)
    finally:
        await pool.close()
    _log.info(SCHEMA_READY, SCHEMA_VERSION, hash_prefix())

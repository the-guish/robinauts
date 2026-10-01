# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The ``robinauts`` command: what it parses, what it refuses, and how it exits.

A command is the one part of a program nobody writes a test for and everybody
runs, so this is about the three things an operator meets: the arguments, the
exit code, and what is printed when something is wrong. A ``ConfigError``
carries every problem at once and has to reach a terminal as those problems and
not as a traceback, because that promise is the whole reason it carries them.

``start`` is exercised against a **stand-in server**: the command's work is to
turn arguments and a configuration into one ``uvicorn.Config`` and to answer
with an exit code, and binding a port would be testing uvicorn. What that
configuration says -- the address, the proxy headers, the shutdown bound -- is
read off the stand-in.

The environment is set per test with ``monkeypatch``: the command reads the
real one, which is exactly what is being tested about it.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import stat
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from asyncpg import InvalidPasswordError

from robinauts.legacy import cli
from robinauts.legacy.app import (
    CONFIG_VARIABLE,
    DATABASE_URL_VARIABLE,
    NO_DATABASE,
    OFF_LOOPBACK,
)
from robinauts.legacy.datastore import SCHEMA_SHA256, SCHEMA_VERSION
from robinauts.legacy.domain import DatabaseUnreachableError

DATABASE_URL = "postgresql://robinauts@127.0.0.1:5432/robinauts"
"""Never connected to: every test here stops before a loop is running."""

WAITING_SECONDS = 10
"""How long a test waits for a process it has told to stop."""


@pytest.fixture(autouse=True)
def _a_clean_environment(monkeypatch: Any) -> Iterator[None]:
    """No configuration file, and a database that is named and never opened.

    The root logger is put back afterwards as well: ``configure_logging`` is a
    command's business and replaces whatever is there, pytest's own handlers
    included, so a test that runs a command must not leave the rest of the
    session logging somewhere else.
    """
    monkeypatch.delenv(CONFIG_VARIABLE, raising=False)
    monkeypatch.delenv("ROBINAUTS_AUTH_CONFIG", raising=False)
    monkeypatch.setenv(DATABASE_URL_VARIABLE, DATABASE_URL)
    root = logging.getLogger()
    held, level = list(root.handlers), root.level
    try:
        yield
    finally:
        root.handlers[:] = held
        root.setLevel(level)


class StandInServer:
    """``uvicorn.Server``'s shape, and a record of what it was configured with.

    It records the **sockets** it was handed as well: a unix socket is bound by
    the command itself and passed to the server already listening, so what is
    interesting about it -- its path, and the mode that says who may open it --
    is only visible here, while the server is "running".
    """

    made: list[StandInServer] = []

    def __init__(self, config: Any) -> None:
        self.config = config
        self.started = True
        self.ran = False
        self.sockets: list[Any] | None = None
        self.seen: list[tuple[str, int]] = []
        self.accepting: list[bool] = []
        StandInServer.made.append(self)

    def run(self, sockets: list[Any] | None = None) -> None:
        self.ran = True
        self.sockets = sockets
        for one in sockets or []:
            path = one.getsockname()
            self.seen.append((path, stat.S_IMODE(os.stat(path).st_mode)))
            self.accepting.append(bool(one.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)))


@pytest.fixture
def server(monkeypatch: Any) -> type[StandInServer]:
    """``uvicorn.Server``, replaced, and its record emptied for this test."""
    StandInServer.made = []
    monkeypatch.setattr(cli.uvicorn, "Server", StandInServer)
    return StandInServer


# The command line.


def test_start_asks_for_nothing_it_was_not_told(server: type[StandInServer]) -> None:
    # The address and the port are `None` until they are resolved, so that
    # "not given" and "given the default" are told apart -- which is what makes
    # `--uds --host 127.0.0.1` a refusal rather than a silent choice.
    arguments = cli.parser().parse_args(["start"])

    assert (arguments.host, arguments.port, arguments.uds) == (None, None, None)
    assert arguments.dev_no_sign_in is False
    assert arguments.log_level == cli.DEFAULT_LOG_LEVEL


def test_start_binds_the_loopback_interface_unless_it_is_told_otherwise(
    server: type[StandInServer],
) -> None:
    # A deployment is behind a reverse proxy that terminates TLS: an http
    # server carrying session cookies must not be on a network by default.
    cli.run(["start", "--dev-no-sign-in"])

    (only,) = server.made
    assert only.config.host == cli.DEFAULT_HOST == "127.0.0.1"
    assert only.config.port == cli.DEFAULT_PORT


def test_the_arguments_of_start_are_read() -> None:
    arguments = cli.parser().parse_args(
        ["start", "--host", "0.0.0.0", "--port", "9000", "--log-level", "debug"]
    )

    assert (arguments.host, arguments.port, arguments.log_level) == ("0.0.0.0", 9000, "debug")


@pytest.mark.parametrize("argv", [[], ["serve"], ["db"], ["db", "migrate"], ["start", "--port"]])
def test_a_command_line_that_is_not_one_is_a_usage_error(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.parser().parse_args(argv)

    assert raised.value.code == cli.MISUSED


@pytest.mark.parametrize("given", ["0", "65536", "http", "-1", "8000.0"])
def test_a_port_that_is_not_a_port_is_refused_before_a_socket(given: str) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.parser().parse_args(["start", "--port", given])

    assert raised.value.code == cli.MISUSED


def test_an_unknown_log_level_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as raised:
        cli.parser().parse_args(["start", "--log-level", "chatty"])

    assert raised.value.code == cli.MISUSED


# version.


def test_version_says_the_build_and_the_schema_it_wants(capsys: Any) -> None:
    # The two halves of an upgrade. Until the first release the schema version
    # stays 1, so the schema.sql hash is what tells a wheel whose schema
    # changed -- and a database to recreate -- from one whose did not
    # (docs/specs/backend.md).
    code = cli.run(["version"])

    said = capsys.readouterr().out
    assert code == cli.OK
    assert said.startswith(f"{cli.PROGRAM} ")
    assert f"(schema {SCHEMA_VERSION}, schema.sql {SCHEMA_SHA256[:12]})" in said


# start.


def test_start_serves_what_the_arguments_say(server: type[StandInServer], capsys: Any) -> None:
    code = cli.run(["start", "--dev-no-sign-in", "--port", "9123"])

    (only,) = server.made
    assert code == cli.OK
    assert only.ran
    assert (only.config.host, only.config.port) == ("127.0.0.1", 9123)
    # What the reverse proxy in front needs, and what a stream needs of a stop.
    assert only.config.proxy_headers is True
    assert only.config.forwarded_allow_ips == cli.DEFAULT_FORWARDED_ALLOW_IPS
    assert only.config.timeout_graceful_shutdown == cli.GRACEFUL_SHUTDOWN_SECONDS
    # Logging is the command's, so uvicorn installs none of its own.
    assert only.config.log_config is None
    capsys.readouterr()


def test_a_unix_socket_is_bound_by_the_command_and_handed_over_listening(
    server: type[StandInServer], tmp_path: Path
) -> None:
    """And it is bound **here**, which is the whole point of the exercise.

    Uvicorn chmods a socket it created to ``0o666`` -- every account on the
    machine -- so what the server is given is one this command made and
    narrowed first. It never sees a ``uds`` in its configuration at all.
    """
    path = tmp_path / "robinauts.sock"

    code = cli.run(["start", "--dev-no-sign-in", "--uds", str(path)])

    (only,) = server.made
    assert code == cli.OK
    assert only.sockets is not None
    assert len(only.sockets) == 1
    assert only.seen == [(str(path), cli.DEFAULT_SOCKET_MODE)]
    assert cli.DEFAULT_SOCKET_MODE == 0o600
    # Already **listening**, which is what "handed over" means: uvicorn calls
    # `create_server(sock=...)` on it and does not bind, and so does not chmod.
    assert only.accepting == [True]
    assert getattr(only.config, "uds", None) is None


def test_the_socket_is_removed_when_the_server_stops(
    server: type[StandInServer], tmp_path: Path
) -> None:
    # A socket file outlives the process that made it, and the next start is
    # refused by what the last one left.
    path = tmp_path / "robinauts.sock"

    cli.run(["start", "--dev-no-sign-in", "--uds", str(path)])

    assert not path.exists()


def test_the_handlers_are_ours_while_it_serves_and_are_put_back_after(
    tmp_path: Path,
) -> None:
    # The process a test runs in goes on afterwards, so what was there has to
    # come back. The handler's own body is exercised below, in a process that
    # can be killed.
    path = tmp_path / "robinauts.sock"
    path.write_text("as if bound", encoding="utf-8")
    before = {number: signal.getsignal(number) for number in cli.STOPPING_SIGNALS}

    with cli.taken_away(str(path)):
        installed = {number: signal.getsignal(number) for number in cli.STOPPING_SIGNALS}

    assert all(installed[number] is not before[number] for number in cli.STOPPING_SIGNALS)
    assert {number: signal.getsignal(number) for number in cli.STOPPING_SIGNALS} == before
    assert not path.exists()


HOLDING = """
import signal, sys, time
from robinauts.legacy import cli

if len(sys.argv) > 2:
    # As if this had been started by something that ignores the signal, which
    # a process inherits. The re-raise then does nothing at all.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
listening = cli.bound_socket(sys.argv[1], cli.DEFAULT_SOCKET_MODE)
with cli.taken_away(sys.argv[1]):
    print("ready", flush=True)
    while True:
        time.sleep(0.05)
"""
"""A process that binds the socket the way ``start`` does and then waits.

The handler's body cannot be exercised in the process running the tests --
what it does, at the end, is make the process die of the signal -- so it is
exercised in one that may.
"""


@contextmanager
def held(path: Path, *, ignoring: bool = False) -> Iterator[subprocess.Popen[str]]:
    """That process, started and bound, and reaped however the block ends."""
    with subprocess.Popen(
        [sys.executable, "-c", HOLDING, str(path), *(["ignore"] if ignoring else [])],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as holding:
        try:
            assert holding.stdout is not None
            ready = holding.stdout.readline()
            assert ready.strip() == "ready", holding.stderr and holding.stderr.read()
            yield holding
        finally:
            # A test that failed before the signal must not leave it waiting.
            if holding.poll() is None:  # pragma: no cover -- only on a failure
                holding.kill()


@pytest.mark.io
@pytest.mark.parametrize(
    ("ignoring", "code"),
    [
        # Died **of** the signal, which is what `Popen` reports as a negative
        # number and a shell as 143.
        (False, -signal.SIGTERM),
        # Exited with that code of its own accord, because the re-raise did
        # nothing: the handler that was there ignores it.
        (True, cli.SIGNAL_EXIT + signal.SIGTERM),
    ],
    ids=["plain", "with SIGTERM ignored"],
)
def test_a_signal_removes_the_socket_and_the_process_stops(
    tmp_path: Path, ignoring: bool, code: int
) -> None:
    """The handler's own body, in a process that can be killed.

    Uvicorn captures the signal, shuts down, restores the handler that was
    there -- this one -- and re-raises, so this is what really runs at the end
    of every stop. Two ways round: the ordinary one, where the handler before
    was the default and the re-raise kills the process; and the one where it
    was ``SIG_IGN``, where the re-raise does nothing and the process has to
    exit of its own accord, because a server told to terminate terminates.
    Either way the socket file is gone, so the next start is not refused by
    what this one left.
    """
    path = tmp_path / "robinauts.sock"
    with held(path, ignoring=ignoring) as holding:
        assert path.is_socket()
        holding.terminate()
        holding.wait(timeout=WAITING_SECONDS)

    assert holding.returncode == code
    assert abs(code) in (signal.SIGTERM, cli.SIGNAL_EXIT + signal.SIGTERM)
    assert not path.exists()


@pytest.mark.io
def test_a_socket_another_process_bound_is_never_unlinked(tmp_path: Path) -> None:
    """Two stops, or a stop and a start, must not cross.

    The file at the path is compared with the one this process bound -- its
    device, its inode and the moment it was made -- before it is removed, so a
    handler that ran a second time, or a ``finally`` after one that already
    ran, cannot take away the socket of the server that was started in
    between. The moment is in there because a file system hands a freed inode
    number straight back out, which is precisely what this case does.
    """
    path = tmp_path / "robinauts.sock"
    ours = cli.bound_socket(str(path), cli.DEFAULT_SOCKET_MODE)
    ours_is = cli.which_file(str(path))
    ours.close()
    path.unlink()

    # Somebody else's, at the same path.
    theirs = cli.bound_socket(str(path), cli.DEFAULT_SOCKET_MODE)
    try:
        cli.unlink_ours(str(path), ours_is)

        assert path.is_socket()
        assert cli.which_file(str(path)) != ours_is
    finally:
        theirs.close()
        path.unlink(missing_ok=True)


@pytest.mark.io
def test_a_bind_that_lost_a_race_leaves_the_winners_socket_alone(tmp_path: Path) -> None:
    """The check that the path is free and the bind are not one step.

    Between them, another server can bind it -- and then the bind here fails
    with the path **theirs and answering**. Cleaning up after a bind that never
    happened would delete a live socket, which is the one thing worse than
    refusing to start.
    """
    path = tmp_path / "robinauts.sock"
    theirs = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    theirs.bind(str(path))
    theirs.listen(1)
    ours = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        # `bound_socket` refuses before this on a path that exists, so the
        # race is played out on the two steps it is made of.
        with pytest.raises(OSError):
            ours.bind(str(path))

        assert path.is_socket()
        assert cli.bound_socket.__doc__ is not None
    finally:
        ours.close()
        theirs.close()
        path.unlink(missing_ok=True)


@pytest.mark.io
def test_the_command_leaves_a_socket_it_did_not_bind_where_it_is(
    server: type[StandInServer], capsys: Any, tmp_path: Path
) -> None:
    # The whole path through `start`: a socket another process is answering
    # on, and this one refuses and touches nothing.
    path = tmp_path / "robinauts.sock"
    theirs = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    theirs.bind(str(path))
    theirs.listen(1)
    try:
        code = cli.run(["start", "--dev-no-sign-in", "--uds", str(path)])

        said = capsys.readouterr().err
        assert code == cli.FAILED
        assert path.is_socket()
        assert str(path) in said
        assert cli.SOCKET_TAKEN in said
    finally:
        theirs.close()
        path.unlink(missing_ok=True)


def test_who_may_open_the_socket_can_be_widened_on_purpose(
    server: type[StandInServer], tmp_path: Path
) -> None:
    # A proxy running as another user, in a directory only those two can enter.
    path = tmp_path / "robinauts.sock"

    cli.run(["start", "--dev-no-sign-in", "--uds", str(path), "--uds-mode", "660"])

    (only,) = server.made
    assert only.seen == [(str(path), 0o660)]


@pytest.mark.parametrize("given", ["rw-", "999", "-1", "1000"])
def test_a_socket_mode_that_is_not_one_is_a_usage_error(given: str) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.parser().parse_args(["start", "--uds", "/run/r.sock", "--uds-mode", given])

    assert raised.value.code == cli.MISUSED


def test_a_path_that_is_already_there_is_not_written_over(
    server: type[StandInServer], capsys: Any, tmp_path: Path
) -> None:
    # Either a socket this command left behind or one another server is
    # answering on, and taking the second one's callers is not a thing to do
    # by guessing.
    path = tmp_path / "robinauts.sock"
    path.write_text("somebody else's", encoding="utf-8")

    code = cli.run(["start", "--dev-no-sign-in", "--uds", str(path)])

    said = capsys.readouterr().err
    (only,) = server.made  # built, because the configuration was read first
    assert code == cli.FAILED
    assert only.ran is False
    assert "Traceback" not in said
    assert str(path) in said
    assert path.read_text(encoding="utf-8") == "somebody else's"


def test_a_socket_satisfies_the_local_development_modes_loopback_rule(
    server: type[StandInServer], capsys: Any, tmp_path: Path
) -> None:
    # Nothing on another machine can reach a file. Who on *this* machine may
    # is the mode's business, and the default is this user alone.
    code = cli.run(["start", "--dev-no-sign-in", "--uds", str(tmp_path / "r.sock")])

    assert code == cli.OK
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("also", [["--host", "127.0.0.1"], ["--port", "9000"]])
def test_a_socket_and_an_address_together_are_refused(
    server: type[StandInServer], capsys: Any, also: list[str], tmp_path: Path
) -> None:
    # uvicorn would bind the socket and leave the address alone, so a
    # deployment that asked for both would be served somewhere it had not read
    # about -- and, in the local development mode, would have been let past the
    # loopback rule by an address nothing ever bound.
    code = cli.run(["start", "--uds", str(tmp_path / "r.sock"), *also])

    said = capsys.readouterr().err
    assert code == cli.MISUSED
    assert server.made == []
    assert cli.ADDRESS_AND_SOCKET in said


def test_a_socket_path_that_names_nothing_is_refused(
    server: type[StandInServer], capsys: Any
) -> None:
    # `--uds ''` is not "no socket": it is a path that names nothing, and
    # binding it would be an error from the kernel rather than an answer.
    code = cli.run(["start", "--dev-no-sign-in", "--uds", ""])

    said = capsys.readouterr().err
    assert code == cli.MISUSED
    assert server.made == []
    assert cli.NO_SOCKET_PATH in said


@pytest.mark.io
def test_a_socket_really_bound_is_openable_by_this_user_and_nobody_else(
    tmp_path: Path,
) -> None:
    """The kernel's answer, not a stand-in's: what the mode really is on disk.

    This is the finding the whole exercise is about -- uvicorn chmods a socket
    it created to ``0o666``, which on a deployment running without sign-in is
    the API handed to every account on the machine -- so the number is read
    back from the file system rather than from what was asked for.
    """
    path = tmp_path / "robinauts.sock"

    listening = cli.bound_socket(str(path), cli.DEFAULT_SOCKET_MODE)
    try:
        assert stat.S_ISSOCK(os.stat(path).st_mode)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert listening.getsockname() == str(path)
    finally:
        listening.close()
        path.unlink(missing_ok=True)

    assert not path.exists()


@pytest.mark.io
def test_a_socket_that_cannot_be_bound_leaves_nothing_behind(tmp_path: Path) -> None:
    # A directory that is not there. What must not happen is a half-made
    # socket file left where the next start would refuse to write over it.
    path = tmp_path / "nowhere" / "robinauts.sock"

    with pytest.raises(OSError):
        cli.bound_socket(str(path), cli.DEFAULT_SOCKET_MODE)

    assert not path.exists()


def test_the_proxy_whose_headers_are_believed_can_be_named(server: type[StandInServer]) -> None:
    cli.run(["start", "--dev-no-sign-in", "--forwarded-allow-ips", "10.0.0.7"])

    (only,) = server.made
    assert only.config.forwarded_allow_ips == "10.0.0.7"


def test_over_a_socket_the_proxy_is_believed_because_nothing_else_can_connect(
    server: type[StandInServer], tmp_path: Path
) -> None:
    """A connection over a unix socket has no address to compare with anything.

    uvicorn reports the client as ``None``, so the default -- the loopback
    address -- would believe nobody and a deployment behind a proxy on that
    socket would read every request as plain ``http`` from nowhere. Nothing is
    given up: only what the mode lets open the socket can connect at all.
    """
    cli.run(["start", "--dev-no-sign-in", "--uds", str(tmp_path / "r.sock")])

    (only,) = server.made
    assert only.config.forwarded_allow_ips == cli.SOCKET_FORWARDED_ALLOW_IPS == "*"


def test_a_socket_deployment_may_still_name_who_it_believes(
    server: type[StandInServer], tmp_path: Path
) -> None:
    cli.run(
        [
            "start",
            "--dev-no-sign-in",
            "--uds",
            str(tmp_path / "r.sock"),
            "--forwarded-allow-ips",
            "10.0.0.7",
        ]
    )

    (only,) = server.made
    assert only.config.forwarded_allow_ips == "10.0.0.7"


def test_a_server_that_never_started_is_a_failure(server: type[StandInServer]) -> None:
    # A lifespan that raised -- a schema of another version, a database that is
    # not there -- is a process that must not exit as though it had served.
    class NeverStarted(StandInServer):
        def run(self, sockets: list[Any] | None = None) -> None:
            self.started = False

    server.made = []

    with pytest.MonkeyPatch.context() as patching:
        patching.setattr(cli.uvicorn, "Server", NeverStarted)
        code = cli.run(["start", "--dev-no-sign-in"])

    assert code == cli.FAILED


def test_the_local_development_mode_refuses_a_host_that_is_not_loopback(
    server: type[StandInServer], capsys: Any
) -> None:
    # The rule is the composition root's, kept where a bind address is decided;
    # what is tested here is that the command hands it the address it will bind.
    code = cli.run(["start", "--dev-no-sign-in", "--host", "0.0.0.0"])

    said = capsys.readouterr().err
    assert code == cli.MISUSED
    assert server.made == []
    assert OFF_LOOPBACK % ("0.0.0.0",) in said


def test_serving_without_the_mode_and_without_a_configuration_is_refused(
    server: type[StandInServer], capsys: Any
) -> None:
    code = cli.run(["start"])

    said = capsys.readouterr().err
    assert code == cli.MISUSED
    assert server.made == []
    assert CONFIG_VARIABLE in said


def test_every_problem_of_a_configuration_is_a_line_and_none_is_a_traceback(
    monkeypatch: Any, server: type[StandInServer], capsys: Any, tmp_path: Path
) -> None:
    # Two mistakes at once: no database, and a sign-in file in a mode that has
    # no sign-in. An operator with two mistakes fixes two mistakes.
    written = tmp_path / "robinauts.toml"
    written.write_text('public_url = "https://robinauts.example.com"\n', encoding="utf-8")
    monkeypatch.delenv(DATABASE_URL_VARIABLE)
    monkeypatch.setenv(CONFIG_VARIABLE, str(written))

    code = cli.run(["start", "--dev-no-sign-in"])

    said = capsys.readouterr().err
    assert code == cli.MISUSED
    assert "Traceback" not in said
    assert len(said.strip().splitlines()) == 2
    assert all(line.startswith(f"{cli.PROGRAM}: ") for line in said.strip().splitlines())
    assert NO_DATABASE in said


# db init.


def test_a_database_that_will_not_open_is_one_line_and_never_the_password(
    monkeypatch: Any, capsys: Any
) -> None:
    """The driver's refusal, not the driver's traceback -- and not the url.

    A wrong password and a database that is not there are an operator's two
    commonest mistakes, and both arrive as an exception of asyncpg's. The
    translation is ``datastore.open_pool``'s (``test_database_errors.py``);
    what is tested here is that the command prints it as one line.
    """
    with_a_password = "postgresql://robinauts:hunter2@db.example.com/robinauts"

    async def refused(*_: Any, **__: Any) -> Any:
        raise DatabaseUnreachableError.from_driver(
            InvalidPasswordError('password authentication failed for user "robinauts"')
        )

    monkeypatch.setenv(DATABASE_URL_VARIABLE, with_a_password)
    monkeypatch.setattr(cli, "open_pool", refused)

    code = cli.run(["db", "init"])

    said = capsys.readouterr().err
    assert code == cli.FAILED
    assert "Traceback" not in said
    assert len(said.strip().splitlines()) == 1
    assert "password authentication failed" in said
    assert "hunter2" not in said
    assert with_a_password not in said


def test_db_init_without_a_database_says_which_variable_to_set(
    monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.delenv(DATABASE_URL_VARIABLE)

    code = cli.run(["db", "init"])

    said = capsys.readouterr().err
    assert code == cli.MISUSED
    assert NO_DATABASE in said
    assert DATABASE_URL_VARIABLE in said


# Logging.


def test_the_log_goes_to_stdout_at_the_level_that_was_asked_for() -> None:
    # A server's log is its output. Its errors are the operator's, in the same
    # stream, in the same order.
    cli.configure_logging("warning")

    (handler,) = logging.getLogger().handlers
    assert logging.getLogger().level == logging.WARNING
    assert isinstance(handler, logging.StreamHandler)
    assert handler.stream is sys.stdout

    cli.configure_logging("info")

    assert len(logging.getLogger().handlers) == 1


def test_a_logger_that_was_quietened_on_purpose_is_not_made_loud_again() -> None:
    # The engines pin the vendor SDK loggers so that a key never turns up in a
    # debug line (docs/specs/agents.md); the command sets the root logger's
    # level, which is not that.
    quietened = logging.getLogger("robinauts.test.a-vendor-sdk")
    quietened.setLevel(logging.WARNING)

    cli.configure_logging("debug")

    assert quietened.level == logging.WARNING
    assert quietened.getEffectiveLevel() == logging.WARNING


def test_an_authorization_code_never_reaches_the_access_log() -> None:
    # The one place this platform puts a credential in a path: an ordinary ASGI
    # access log would write the code and the state of every sign-in to a file.
    callback = "/auth/callback/google?code=s3cr3t&state=x"
    line = record('%s - "%s %s HTTP/%s" %d', "1.2.3.4:5", "GET", callback, "1.1", 303)

    assert cli.NoQueryStrings().filter(line) is True
    assert "s3cr3t" not in line.getMessage()
    assert "/auth/callback/google" in line.getMessage()


def test_the_rest_of_an_access_line_is_left_as_it_was() -> None:
    line = record('%s - "%s %s HTTP/%s" %d', "1.2.3.4:5", "GET", "/api/conversations", "1.1", 200)

    assert cli.NoQueryStrings().filter(line) is True
    assert line.getMessage() == '1.2.3.4:5 - "GET /api/conversations HTTP/1.1" 200'


@pytest.mark.parametrize("arguments", [(), ("one",), ("one", "two"), (1, 2, 3)])
def test_a_line_that_is_not_an_access_line_passes_through(arguments: tuple[Any, ...]) -> None:
    # The filter is on one logger, but a logger is a name and anything may log
    # to it: a record shaped differently is left alone rather than rewritten.
    line = record("%s", *arguments)
    before = line.args

    assert cli.NoQueryStrings().filter(line) is True
    assert line.args == before


def record(message: str, *arguments: Any) -> logging.LogRecord:
    """One access-log record, shaped as uvicorn's own protocol writes it."""
    return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, message, arguments, None)

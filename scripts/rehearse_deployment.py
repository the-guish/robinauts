#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The live half of the deployment rehearsal: sign in, and everything after it.

A development tool. ruff and black judge it through
``scripts/check-lint.sh``, which reads ``scripts/`` as well as the backend; no
check **runs** it, and nothing imports it. It exists so that
``docs/deployment.md`` can be walked through on a machine with no real
identity provider, no real model key and no domain name, and so that the walk
can be done again rather than remembered.

``scripts/rehearse-deployment.sh`` does the machine-level half -- the wheel, a
virtual environment, a throwaway database, ``robinauts db init`` -- and then
runs this against what it built. What this adds is everything that needs a
browser's worth of behaviour:

- two **stand-in OpenID Connect providers** (``backend/tests/standin``), each
  on a loopback port of its own, standing in for Google and for Okta;
- a **TLS terminator** in front of the server, so that the deployment really
  is ``https`` and the ``__Host-`` cookies really are set. That is what the
  reverse proxy of the guide does, and the one thing a rehearsal on a machine
  with no certificate cannot borrow from somewhere else;
- the start-up refusals, each run as a process of its own and read for what it
  said;
- three people signing in, two of whom may; a conversation each; one asking
  for the other's; a stream dropped and re-attached with ``Last-Event-ID``;
- a grep of the log for every secret this process was given.

What it cannot rehearse it says where it is skipped, and
``docs/working-notes/deployment-rehearsal.md`` gathers those in one place.
"""

from __future__ import annotations

import argparse
import json
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx

BACKEND_HOST = "127.0.0.1"

PROXY_PORT = 8443
"""Where the TLS terminator listens: the deployment's public port."""

BACKEND_PORT = 8000
"""Where the server listens, as ``docs/deployment.md`` has it.

Both are ``--proxy-port`` and ``--backend-port``, because a development
machine may already have something on either -- and a rehearsal that quietly
proxied to whatever else was listening would report on somebody else's server.
``addresses`` is the one place they are set, and nothing binds until
``free`` has said the port is not taken.
"""

PUBLIC_URL = f"https://{BACKEND_HOST}:{PROXY_PORT}"
"""``public_url``: the origin the browser is on, which is the proxy's."""


def addresses(backend_port: int, proxy_port: int) -> None:
    """Put the ports this run was given where everything else reads them."""
    global BACKEND_PORT, PROXY_PORT, PUBLIC_URL
    BACKEND_PORT, PROXY_PORT = backend_port, proxy_port
    PUBLIC_URL = f"https://{BACKEND_HOST}:{PROXY_PORT}"


def free(port: int, what: str) -> None:
    """Refuse to start if something is already listening there."""
    with socket.socket() as probe:
        probe.settimeout(1.0)
        if probe.connect_ex((BACKEND_HOST, port)) == 0:
            raise SystemExit(
                f"{BACKEND_HOST}:{port} is taken, and that is where {what} goes."
                f" Stop it, or give another port."
            )


FAKE_ANTHROPIC_KEY = "sk-ant-rehearsal-0000-never-valid"
FIRST_SECRET = "rehearsal-first-client-secret"
SECOND_SECRET = "rehearsal-second-client-secret"
"""Invented, and distinctive, so that a grep for them means something."""

START_SECONDS = 60.0
STEP_SECONDS = 30.0

RESULTS: list[tuple[bool, str]] = []


def record(ok: bool, what: str) -> bool:
    """Print one outcome and keep it for the summary at the end."""
    print(f"  [{'ok' if ok else 'FAILED'}] {what}", flush=True)
    RESULTS.append((ok, what))
    return ok


def note(what: str) -> None:
    """Something this machine cannot rehearse, said where it is skipped."""
    print(f"  [skip] {what}", flush=True)


ANSWERS: list[str] = []
"""Every answer this driver read, headers and body, for the grep at the end.

"No response" is half of what goal 5 asks (``docs/working-notes/poc-scope.md``)
and a log is the other half, so what crossed the wire is kept as it arrived
rather than judged one route at a time.
"""


def kept(response: httpx.Response) -> httpx.Response:
    """Keep an answer for the grep, and hand it straight back."""
    ANSWERS.append("\n".join(f"{name}: {value}" for name, value in response.headers.items()))
    try:
        ANSWERS.append(response.text)
    except (httpx.ResponseNotRead, UnicodeDecodeError):
        # A stream is kept line by line where it is read (``reattach``).
        pass
    return response


# The TLS terminator: what a reverse proxy does, and no more of it.


class Terminator(socketserver.ThreadingTCPServer):
    """https on ``PROXY_PORT``, plain http to the server, one request a connection.

    Deliberately the smallest thing that is still a real reverse proxy: it
    terminates TLS with the self-signed certificate the shell script made, puts
    the ``X-Forwarded-*`` headers on the request -- which reach uvicorn's
    access log and nothing else, since no layer of the platform reads a
    request's scheme or client address -- and pipes the rest through untouched,
    which is what makes an event stream arrive as it is produced rather than in
    one piece at the end.

    ``Connection: close`` upstream, so one connection carries one request: a
    proxy that kept the connection alive would have to understand the framing
    of every answer to know where the next request began, and framing is not
    what is being rehearsed here.
    """

    daemon_threads = True
    allow_reuse_address = True
    context: ssl.SSLContext

    def get_request(self) -> tuple[Any, Any]:
        raw, address = super().get_request()
        return self.context.wrap_socket(raw, server_side=True), address

    def handle_error(self, request: Any, client_address: Any) -> None:
        """A handshake somebody abandoned is not news; a real fault still is."""
        if isinstance(sys.exception(), ssl.SSLError | ConnectionError | TimeoutError):
            return
        super().handle_error(request, client_address)


class Forwarder(socketserver.BaseRequestHandler):
    """One request: read its head, add the forwarded headers, pipe both ways."""

    def handle(self) -> None:
        head = b""
        while b"\r\n\r\n" not in head:
            piece = self.request.recv(4096)
            if not piece:
                return
            head += piece
        head, _, rest = head.partition(b"\r\n\r\n")
        lines = [
            line
            for line in head.split(b"\r\n")
            if not line.lower().startswith((b"x-forwarded-", b"connection:"))
        ]
        authority = PUBLIC_URL.partition("://")[2].encode("ascii")
        lines[1:1] = [
            b"x-forwarded-for: " + self.client_address[0].encode("ascii"),
            b"x-forwarded-proto: https",
            b"x-forwarded-host: " + authority,
            b"connection: close",
        ]
        with socket.create_connection((BACKEND_HOST, BACKEND_PORT)) as upstream:
            upstream.sendall(b"\r\n".join(lines) + b"\r\n\r\n" + rest)
            forward = threading.Thread(target=pipe, args=(self.request, upstream), daemon=True)
            forward.start()
            pipe(upstream, self.request)


def pipe(source: Any, sink: Any) -> None:
    """Everything from one socket to the other, until either gives up."""
    try:
        while True:
            piece = source.recv(65536)
            if not piece:
                break
            sink.sendall(piece)
    except OSError:
        pass
    finally:
        try:
            sink.shutdown(socket.SHUT_WR)
        except OSError:
            pass


@contextmanager
def terminating(certificate: Path, key: Path) -> Iterator[None]:
    """The terminator, serving, for as long as the block lasts."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    server = Terminator((BACKEND_HOST, PROXY_PORT), Forwarder)
    server.context = context
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()


# The configuration, written around whatever ports the stand-ins were given.


def configuration(issuer_a: str, issuer_b: str, *, engine: str) -> str:
    """The deployment's file: two providers, an allow list, one agent."""
    return f"""\
public_url = "{PUBLIC_URL}"
session_hours = 12

[providers.standin-a]
title = "Stand-in A"
issuer = "{issuer_a}"
client_id = "stand-in-client"
client_secret_env = "ROBINAUTS_FIRST_SECRET"

[providers.standin-b]
title = "Stand-in B"
issuer = "{issuer_b}"
client_id = "stand-in-client"
client_secret_env = "ROBINAUTS_SECOND_SECRET"
scopes = ["openid", "email", "profile", "groups"]
groups_claim = "groups"

[[allow]]
provider = "standin-a"
email = "ada@example.com"

[[allow]]
provider = "standin-b"
group = "robinauts-users"

[model_providers.anthropic]
kind = "anthropic"
api_key_env = "ROBINAUTS_ANTHROPIC_KEY"

[models.sonnet]
provider = "anthropic"
name = "claude-sonnet-5"

[agents.assistant]
title = "Assistant"
model = "sonnet"
engine = "{engine}"
system_prompt = "Play fair."
"""


SECRET_VARIABLES = ("ROBINAUTS_FIRST_SECRET", "ROBINAUTS_SECOND_SECRET", "ROBINAUTS_ANTHROPIC_KEY")


def deployment_environment(work: Path, database_url: str) -> dict[str, str]:
    """What the systemd unit of the guide puts in its ``EnvironmentFile``."""
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(work),
        "ROBINAUTS_CONFIG": str(work / "robinauts.toml"),
        "ROBINAUTS_DATABASE_URL": database_url,
        "ROBINAUTS_FIRST_SECRET": FIRST_SECRET,
        "ROBINAUTS_SECOND_SECRET": SECOND_SECRET,
        "ROBINAUTS_ANTHROPIC_KEY": FAKE_ANTHROPIC_KEY,
    }


def without(environment: dict[str, str], *names: str) -> dict[str, str]:
    """The same environment with those variables unset."""
    return {name: value for name, value in environment.items() if name not in names}


# Start-up: the refusals, and the server that does start.


def refusal(command: list[str], environment: dict[str, str]) -> str:
    """Run a command that must not start, and give back what it said."""
    done = subprocess.run(
        command, env=environment, capture_output=True, text=True, timeout=120, check=False
    )
    return f"exit {done.returncode}\n{done.stdout}{done.stderr}"


@contextmanager
def serving(command: list[str], environment: dict[str, str], log: Path) -> Iterator[None]:
    """The server, running, with its log in a file a grep can read afterwards."""
    free(BACKEND_PORT, "the server")
    with open(log, "ab") as output:
        process = subprocess.Popen(command, env=environment, stdout=output, stderr=output)
    try:
        wait_for_health(process, log)
        yield
    finally:
        process.terminate()
        try:
            process.wait(timeout=STEP_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=STEP_SECONDS)


def wait_for_health(process: subprocess.Popen[bytes], log: Path) -> None:
    """Until ``/health`` answers through the proxy, or give up saying so.

    **The process is watched as well as the port.** A server that refused to
    start leaves the port to whatever else is on this machine, and a rehearsal
    that went on would be reporting on a stranger's deployment.
    """
    deadline = time.monotonic() + START_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit(
                f"the server stopped with {process.returncode} before it served:\n"
                f"{log.read_text(encoding='utf-8', errors='replace')[-2000:]}"
            )
        try:
            with browser() as client:
                if client.get(f"{PUBLIC_URL}/health").json() == {"status": "ok"}:
                    return
        except (httpx.HTTPError, ValueError, ssl.SSLError):
            pass
        time.sleep(0.2)
    raise SystemExit("the server never answered /health through the proxy")


CERTIFICATE = ""
"""The self-signed certificate every browser here trusts, and nothing else."""


def browser(*, follow: bool = True) -> httpx.Client:
    """One person's browser: a cookie jar of its own, and our certificate."""
    return httpx.Client(
        verify=CERTIFICATE, follow_redirects=follow, timeout=STEP_SECONDS, trust_env=False
    )


def sign_in(provider: str) -> tuple[httpx.Client, httpx.Response]:
    """Follow a whole sign-in, as a browser would, and keep the cookies.

    The stand-in's authorization endpoint asks nobody anything: it mints a code
    and redirects straight back, so the chain from ``/auth/login`` to wherever
    it ends is the whole flow -- state, nonce, PKCE, the code exchange, the
    claim checks and the allow list included.
    """
    client = browser()
    landed = client.get(f"{PUBLIC_URL}/auth/login/{provider}", params={"return_to": "/#/"})
    return client, landed


def signed_in(client: httpx.Client) -> str | None:
    """The email of whoever this browser is, or ``None`` if it is nobody."""
    who = kept(client.get(f"{PUBLIC_URL}/auth/session")).json().get("user")
    return who.get("email") if who else None


def error_in(url: str) -> str | None:
    """The sign-in page's fixed error code out of the URL a refusal landed on."""
    fragment = urllib.parse.urlsplit(url).fragment
    return urllib.parse.parse_qs(fragment.partition("?")[2]).get("error", [None])[0]


def write(client: httpx.Client, path: str, body: dict[str, Any]) -> httpx.Response:
    """A write as the interface makes one: JSON, and this deployment's origin."""
    return kept(
        client.post(
            f"{PUBLIC_URL}{path}",
            content=json.dumps(body),
            headers={"content-type": "application/json", "origin": PUBLIC_URL},
        )
    )


ADA = {"sub": "ada", "name": "Ada Lovelace", "email": "ada@example.com", "email_verified": True}
GRACE = {
    "sub": "grace",
    "name": "Grace Hopper",
    "email": "grace@example.com",
    "email_verified": True,
    "groups": ["robinauts-users"],
}
MALLORY = {
    "sub": "mallory",
    "name": "Mallory",
    "email": "mallory@elsewhere.example",
    "email_verified": True,
}


def main(argv: list[str] | None = None) -> int:
    global CERTIFICATE
    parser = argparse.ArgumentParser(description="the live half of the deployment rehearsal")
    parser.add_argument("--work", type=Path, required=True, help="where the rehearsal lives")
    parser.add_argument("--venv", type=Path, required=True, help="the venv the wheel is in")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--standin", type=Path, required=True, help="backend/tests")
    parser.add_argument("--backend-port", type=int, default=BACKEND_PORT)
    parser.add_argument("--proxy-port", type=int, default=PROXY_PORT)
    args = parser.parse_args(argv)

    sys.path.insert(0, str(args.standin))
    from standin.provider import StandInProvider

    addresses(args.backend_port, args.proxy_port)
    free(PROXY_PORT, "the TLS terminator")
    CERTIFICATE = str(args.work / "tls" / "certificate.pem")
    config = args.work / "robinauts.toml"
    log = args.work / "server.log"
    start = [
        str(args.venv / "bin" / "robinauts"),
        "start",
        "--host",
        BACKEND_HOST,
        "--port",
        str(BACKEND_PORT),
        "--forwarded-allow-ips",
        BACKEND_HOST,
    ]

    with (
        StandInProvider(client_secret=FIRST_SECRET) as first,
        StandInProvider(client_secret=SECOND_SECRET) as second,
    ):
        first.person = dict(ADA)
        second.person = dict(GRACE)
        config.write_text(
            configuration(first.issuer, second.issuer, engine="langgraph"), encoding="utf-8"
        )
        environment = deployment_environment(args.work, args.database_url)

        print("\n== start-up refusals ==", flush=True)
        refusals(start, environment)

        with terminating(args.work / "tls" / "certificate.pem", args.work / "tls" / "key.pem"):
            conversation = live(start, environment, log, first)
            print("\n== the engine changed in the file, across a restart ==", flush=True)
            config.write_text(
                configuration(first.issuer, second.issuer, engine="pydantic-ai"), encoding="utf-8"
            )
            after_restart(start, environment, log, first, conversation)

    print("\n== the secrets, nowhere ==", flush=True)
    written = log.read_text(encoding="utf-8", errors="replace")
    answered = "\n".join(ANSWERS)
    rows = every_row(args.database_url)
    secrets = (
        ("the model key", FAKE_ANTHROPIC_KEY),
        ("the first client secret", FIRST_SECRET),
        ("the second client secret", SECOND_SECRET),
    )
    for where, text in (
        ("log line", written),
        (f"response ({len(ANSWERS)} read)", answered),
        ("database row", rows),
    ):
        for name, secret in secrets:
            record(secret not in text, f"{name} is in no {where}")
    # The **password**, and the whole url it lives in. The half after the `@`
    # is the host and the database name, which are not a secret and which a
    # message about a database may perfectly well say; what must never be
    # written down is the credential in front of it (``domain.without_secrets``).
    password = urllib.parse.urlsplit(args.database_url).password or ""
    assert password, "this rehearsal's url has no password, so this proves nothing"
    for where, text in (("log line", written), ("response", answered)):
        record(
            password not in text and args.database_url not in text,
            f"the database password, and the url holding it, are in no {where}",
        )

    failed = [what for ok, what in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} rehearsed", flush=True)
    for what in failed:
        print(f"  FAILED {what}", flush=True)
    return 1 if failed else 0


def every_row(database_url: str) -> str:
    """Every row of every table of the deployment's schema, as text.

    ``to_jsonb`` so that the search is over the **values** rather than over a
    column this happened to name: a key that had got into the database would be
    in it whichever column somebody wrote it to (``docs/specs/agents.md``).
    """
    import asyncio

    import asyncpg

    from robinauts.legacy.datastore.schema import SCHEMA_TABLES

    async def read() -> str:
        connection = await asyncpg.connect(database_url)
        try:
            dumped = []
            for table in sorted(SCHEMA_TABLES):
                rows = await connection.fetch(f'SELECT to_jsonb(t)::text FROM "{table}" AS t')
                dumped.extend(str(row[0]) for row in rows)
            print(f"  read {len(dumped)} row(s) of {len(SCHEMA_TABLES)} table(s)", flush=True)
            return "\n".join(dumped)
        finally:
            await connection.close()

    return asyncio.run(read())


def refusals(start: list[str], environment: dict[str, str]) -> None:
    """Each start-up refusal of the guide's table that this machine can make."""
    said = refusal(start, without(environment, "ROBINAUTS_CONFIG"))
    record(
        "exit 2" in said and "set ROBINAUTS_CONFIG" in said, f"no configuration: {shortened(said)}"
    )
    said = refusal(start, without(environment, "ROBINAUTS_DATABASE_URL"))
    record("exit 2" in said and "ROBINAUTS_DATABASE_URL" in said, f"no database: {shortened(said)}")
    said = refusal(start, without(environment, *SECRET_VARIABLES))
    record(
        "exit 2" in said
        and said.count("which is unset or empty") == len(SECRET_VARIABLES)
        and all(name in said for name in SECRET_VARIABLES),
        "keys removed: every variable named at once, in one refusal",
    )
    print(said, flush=True)


def live(start: list[str], environment: dict[str, str], log: Path, first: Any) -> str:
    """Everything the guide's "Verify" section asks of a running deployment."""
    with serving(start, environment, log):
        print("\n== the interface, out of the wheel ==", flush=True)
        with browser(follow=False) as visitor:
            landing = visitor.get(f"{PUBLIC_URL}/")
            record(
                landing.status_code == 302 and landing.headers["location"] == "/ui/",
                f"the root redirects to /ui/: {landing.status_code} {landing.headers['location']}",
            )
        with browser() as visitor:
            page = kept(visitor.get(f"{PUBLIC_URL}/ui/"))
            record(
                page.status_code == 200 and '<div id="root">' in page.text,
                f"the interface is served at /ui/ ({len(page.content)} bytes)",
            )
            record(
                page.headers.get("content-security-policy", "").startswith("default-src 'none'"),
                "with the platform's own Content-Security-Policy on it",
            )

        print("\n== signing in ==", flush=True)
        with browser() as anybody:
            session = kept(anybody.get(f"{PUBLIC_URL}/auth/session")).json()
            record(
                session["sign_in"] is True
                and sorted(offered["id"] for offered in session["providers"])
                == ["standin-a", "standin-b"],
                "the sign-in page is offered both providers",
            )
        ada, landed = sign_in("standin-a")
        record(
            signed_in(ada) == "ada@example.com",
            f"the first person signed in, allowed by email; landed on {landed.url}",
        )
        record(
            any(cookie.name.startswith("__Host-") for cookie in ada.cookies.jar),
            "the session cookie carries the __Host- prefix, which only https allows",
        )
        grace, landed = sign_in("standin-b")
        record(
            signed_in(grace) == "grace@example.com",
            f"the second person signed in by group; landed on {landed.url}",
        )

        first.person = dict(MALLORY)
        mallory, landed = sign_in("standin-a")
        record(error_in(str(landed.url)) == "not_allowed", f"a third is refused: {landed.url}")
        record(signed_in(mallory) is None, "and is signed in to nothing")
        mallory.close()

        print("\n== conversations, private to their author ==", flush=True)
        agents = kept(ada.get(f"{PUBLIC_URL}/api/agents")).json()["items"]
        record(
            [agent["id"] for agent in agents] == ["assistant"]
            and agents[0]["engine"] == "langgraph",
            f"the agent list is what the file says: {agents}",
        )
        answer = write(ada, "/api/turns", {"agent_id": "assistant", "text": "Hello?"})
        conversation = answer.headers["x-robinauts-conversation-id"]
        run = answer.headers["x-robinauts-run-id"]
        record(answer.status_code == 200, f"a turn begins and streams: run {run}")
        record(
            kept(ada.get(f"{PUBLIC_URL}/api/conversations/{conversation}")).status_code == 200,
            "its author can open it",
        )
        record(
            kept(grace.get(f"{PUBLIC_URL}/api/conversations/{conversation}")).status_code == 404,
            "and the other person gets a 404, not a 403",
        )
        record(
            all(
                held["id"] != conversation
                for held in kept(grace.get(f"{PUBLIC_URL}/api/conversations")).json()["items"]
            ),
            "and does not see it in their history",
        )

        print("\n== the tab closed, and the stream re-attached ==", flush=True)
        reattach(ada, run)
        note(
            "there is no model key on this machine, so the run fails at the vendor"
            " instead of producing an answer: what is rehearsed is the re-attach"
        )
        ada.close()
        grace.close()
    return conversation


def reattach(client: httpx.Client, run: str) -> None:
    """Read a stream, drop it, and ask for the rest by ``Last-Event-ID``."""
    seen = ""
    with client.stream("GET", f"{PUBLIC_URL}/api/runs/{run}/events") as stream:
        for line in stream.iter_lines():
            ANSWERS.append(line)
            if line.startswith("id:"):
                seen = line.partition(":")[2].strip()
                break
    if not record(bool(seen), f"the stream of a started run can be read: first id {seen!r}"):
        return
    rest: list[str] = []
    with client.stream(
        "GET", f"{PUBLIC_URL}/api/runs/{run}/events", headers={"last-event-id": seen}
    ) as stream:
        for line in stream.iter_lines():
            ANSWERS.append(line)
            if line.startswith("id:"):
                rest.append(line.partition(":")[2].strip())
    record(
        bool(rest) and all(int(position) > int(seen) for position in rest),
        f"re-attaching after {seen} replays nothing before it: {rest}",
    )


def after_restart(
    start: list[str], environment: dict[str, str], log: Path, first: Any, conversation: str
) -> None:
    """The same deployment, started again, with one line of the file changed."""
    with serving(start, environment, log):
        first.person = dict(ADA)
        ada, _ = sign_in("standin-a")
        agents = kept(ada.get(f"{PUBLIC_URL}/api/agents")).json()["items"]
        record(
            agents[0]["engine"] == "pydantic-ai",
            f"the agent runs on the other engine after a restart: {agents}",
        )
        record(
            kept(ada.get(f"{PUBLIC_URL}/api/conversations/{conversation}")).status_code == 200,
            "and the conversation begun on the first engine is still there",
        )
        ada.close()
        note("whether its next turn answers on the other engine needs a model key")


def shortened(said: str) -> str:
    """What a refusal printed, on one line, for a record of it."""
    return said.replace("\n", " / ")[:160]


if __name__ == "__main__":
    raise SystemExit(main())

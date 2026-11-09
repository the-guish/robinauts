# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""neorc's manager under ``/neorc/``, for the interface's Flows page
(``docs/architecture/orchestration.md``, "The Flows page"): in the local development mode with
an ``[orchestration]`` table, the server answers for the manager it started, its UI at
``/neorc/ui/`` and its API above it. What the browser cannot show is asserted here: the headers
each answer carries, what the manager is not handed, that an answer streams as it comes, and
that a server without the table has none of it. neorc is the stand-in of
``util/fake_neorc.py``."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from robinauts.web.headers import SECURITY_HEADERS
from util import stack
from util.controller_db import DATABASE_URL, NO_DATABASE

pytestmark = pytest.mark.io

REPO = Path(__file__).resolve().parents[3]
FAKE_NEORC = Path(__file__).resolve().parents[1] / "util" / "fake_neorc.py"

CONFIG = """
[model_providers.echo]
kind = "anthropic"
api_key_env = "ECHO_API_KEY"

[models.echo]
provider = "echo"
name = "echo"
title = "Echo"

[agents.echo]
title = "Echo"
system_prompt = ""
model = "echo"
engine = "echo"

[orchestration]
command = {command}
code_location = "{code}"
port = {port}
"""


class Running:
    def __init__(self, url: str, log: Path) -> None:
        self.url = url
        self.log = log

    def requests(self) -> list[dict[str, Any]]:
        """What the manager was asked, oldest first."""
        lines = self.log.read_text().splitlines()
        return [e for e in map(json.loads, lines) if e.get("step") == "manager-request"]


@pytest.fixture(scope="module")
def with_neorc(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Running]:
    here = tmp_path_factory.mktemp("flows")
    config, log = here / "robinauts.toml", here / "neorc.jsonl"
    command = json.dumps([sys.executable, str(FAKE_NEORC)])
    config.write_text(
        CONFIG.format(command=command, code=REPO / "orchestration", port=stack.free_port())
    )
    environment = {
        **os.environ,
        "ROBINAUTS_CONFIG": str(config),
        "NEORC_DATABASE_URL": "postgresql://127.0.0.1/neorc-unused",
        "ECHO_API_KEY": "not-a-real-key",
        "FAKE_NEORC_LOG": str(log),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    environment.pop("ROBINAUTS_DATABASE_URL", None)
    port = stack.free_port()
    start = [str(stack.ROBINAUTS), "start", "--dev-no-sign-in", "--port", str(port)]
    with subprocess.Popen(start, env=environment, stderr=subprocess.DEVNULL) as running:
        try:
            url = f"http://127.0.0.1:{port}"
            stack.wait_until_up(url, running)
            yield Running(url, log)
        finally:
            running.terminate()
            try:
                running.wait(timeout=60)
            except subprocess.TimeoutExpired:
                running.kill()


@pytest.fixture
def neorc(with_neorc: Running) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=with_neorc.url, timeout=30.0) as client:
        yield client


def test_the_session_says_the_flows_page_is_there(neorc: httpx.Client) -> None:
    session = neorc.get("/auth/session")
    assert session.status_code == 200
    assert session.json()["flows"] is True


def test_the_ui_may_be_framed_by_this_server_alone_and_reaches_only_neorc(
    neorc: httpx.Client, with_neorc: Running
) -> None:
    page = neorc.get("/neorc/ui/")
    assert page.status_code == 200
    assert page.content.startswith(b"<!doctype html><title>neorc</title>")
    host = with_neorc.url.removeprefix("http://")
    # neorc's own policy, with frame-ancestors 'self' and its connections narrowed to /neorc/.
    assert page.headers.get_list("content-security-policy") == [
        "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline';"
        f" img-src 'self' data:; font-src 'self'; connect-src http://{host}/neorc/;"
        " base-uri 'none'; form-action 'self'; frame-ancestors 'self'"
    ]
    assert page.headers.get_list("x-frame-options") == ["SAMEORIGIN"]
    assert page.headers.get_list("x-content-type-options") == ["nosniff"]
    assert page.headers.get_list("referrer-policy") == ["same-origin"]
    assert page.headers["cache-control"] == "no-cache"
    # neorc sets nothing on this server's origin.
    assert "set-cookie" not in page.headers


def test_the_managers_redirect_stays_under_the_prefix(neorc: httpx.Client) -> None:
    moved = neorc.get("/neorc/")
    assert moved.status_code == 307
    assert moved.headers["location"] == "/neorc/ui/"


def test_the_api_answers_carry_this_servers_headers(neorc: httpx.Client) -> None:
    runs = neorc.get("/neorc/runs", params={"limit": "5"})
    assert runs.status_code == 200
    assert runs.json() == {"items": [], "next": None}
    for header, value in SECURITY_HEADERS:
        assert runs.headers.get_list(header) == [value], header
    missing = neorc.get("/neorc/nothing")
    assert missing.status_code == 404
    assert missing.headers["x-frame-options"] == "DENY"


def test_an_answer_streams_as_the_manager_sends_it(neorc: httpx.Client) -> None:
    started = time.monotonic()
    arrived: list[tuple[float, bytes]] = []
    with neorc.stream("GET", "/neorc/events", params={"after": "0", "timeout": "20"}) as events:
        assert events.status_code == 200
        for chunk in events.iter_raw():
            arrived.append((time.monotonic() - started, chunk))
    body = b"".join(chunk for _, chunk in arrived)
    assert json.loads(body) == [{"sequence": 1}, {"sequence": 2}]
    # The first event came before the manager had sent the second.
    first_at = next(at for at, chunk in arrived if b"sequence" in chunk)
    assert first_at < time.monotonic() - started - 0.4, arrived


def test_a_write_reaches_the_manager_without_this_servers_credentials(
    neorc: httpx.Client, with_neorc: Running
) -> None:
    started = neorc.post(
        "/neorc/flows/guess_number/runs",
        json={"inputs": {}},
        headers={
            "origin": with_neorc.url,
            "cookie": "robinauts_session=secret",
            "authorization": "Bearer rbo_secret",
        },
    )
    assert started.status_code == 201, started.text
    assert started.json() == {"id": "run-1", "flow": "guess_number", "inputs": {"inputs": {}}}
    [asked] = [r for r in with_neorc.requests() if r["method"] == "POST"]
    assert asked["path"] == "/flows/guess_number/runs"
    assert json.loads(asked["body"]) == {"inputs": {}}
    assert {"cookie", "authorization", "origin"}.isdisjoint(asked["headers"])
    assert asked["headers"]["host"].startswith("127.0.0.1:")


def test_a_write_from_another_origin_never_reaches_the_manager(
    neorc: httpx.Client, with_neorc: Running
) -> None:
    refused = neorc.post(
        "/neorc/flows/other/runs", json={"inputs": {}}, headers={"origin": "http://evil.example"}
    )
    assert refused.status_code == 403
    assert not [r for r in with_neorc.requests() if r["path"] == "/flows/other/runs"]
    renamed = neorc.get("/neorc/ui/", headers={"host": "evil.example"})
    assert renamed.status_code == 403


@pytest.mark.database
@pytest.mark.skipif(DATABASE_URL is None, reason=NO_DATABASE)
def test_without_orchestration_there_is_no_flows_page(api: httpx.Client) -> None:
    assert api.get("/auth/session").json()["flows"] is False
    for path in ("/neorc/ui/", "/neorc/runs", "/neorc/"):
        assert api.get(path).status_code == 404, path

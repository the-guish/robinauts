# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The worker under the server, as a deployment runs it, without Docker: a server with no
database, in oneberth's local mode, starts this worker's process as its ``[workers.fabric]``
command says; the turns of the agent on that worker run on the ``fabric`` engine, through NeMo
Fabric's Claude adapter and Claude Code, against a stand-in of Anthropic's Messages API on this
machine. The person's conversation, read back from the server's API: two turns, a tool call shown
in the chat, and an edit of the second question, whose answer the model gave knowing the first
turn and not the second. The server stops with its worker.

Helpers after extra-workers/claude-code-worker/tests/test_container.py, whose server it is.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from oneberth.identity import CredentialKind, new_secret

from tests.fake_anthropic import FakeAnthropicServer, system_text

ROBINAUTS = Path(sys.executable).with_name("robinauts")
"""The server, from this project's environment: the same build the worker runs."""

CONFIG = """
[model_providers.local_claude]
kind = "anthropic-compatible"
base_url = "{base_url}"
api_key_env = "LOCAL_CLAUDE_KEY"

[models.local_claude]
provider = "local_claude"
name = "claude-sonnet-5-5"
title = "Local Claude"

[agents.coder]
title = "Claude behind Fabric"
system_prompt = "You are a robinaut."
models = ["local_claude"]
engine = "fabric"
worker = "fabric"

[workers.fabric]
command = [{python}, "-m", "robinauts_fabric_worker"]
"""

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the server's worker on Linux")


@pytest.fixture
def model_server() -> Iterator[FakeAnthropicServer]:
    with FakeAnthropicServer() as running:
        yield running


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def last_event(body: str) -> dict[str, Any]:
    data = [line for line in body.splitlines() if line.startswith("data: ")]
    return json.loads(data[-1].removeprefix("data: "))


def finished(response: httpx.Response) -> None:
    assert response.status_code == 200, response.text
    ended = last_event(response.text)
    assert (ended["type"], ended.get("outcome")) == ("RUN_FINISHED", None), ended


def messages(api: httpx.Client, cid: str) -> list[dict[str, Any]]:
    return api.get(f"/robinauts/api/conversations/{cid}").json()["messages"]


def text_of(message: dict[str, Any]) -> str:
    return "".join(p.get("text", "") for p in message["parts"] if p.get("type", "text") == "text")


def launched(
    command: list[str], environment: dict[str, str], launch_secret: str
) -> subprocess.Popen[bytes]:
    """The server in oneberth's local mode, its launch secret on its standard input as the
    launcher writes it."""
    server = subprocess.Popen(
        command, env=environment, stdin=subprocess.PIPE, stderr=subprocess.PIPE
    )
    assert server.stdin is not None
    server.stdin.write(f"{launch_secret}\n".encode())
    server.stdin.close()
    server.stdin = None
    return server


def signed_in(api: httpx.Client, launch_secret: str) -> None:
    asked = api.post("/auth/tickets", json={}, headers={"authorization": f"Bearer {launch_secret}"})
    assert asked.status_code in (200, 201), asked.text
    opened = api.get(asked.json()["url"])
    assert opened.status_code == 303, opened.text
    api.headers["origin"] = str(api.base_url).rstrip("/")


def wait_until_up(api: httpx.Client, server: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if server.poll() is not None:
            _, said = server.communicate()
            raise RuntimeError(f"the server exited with {server.returncode}: {said.decode()}")
        try:
            if api.get("/health").status_code == 200:
                return
        except httpx.TransportError:
            time.sleep(0.2)
    raise RuntimeError("nothing answered")


def test_the_server_holds_a_conversation_on_claude_behind_fabric(
    model_server: FakeAnthropicServer, tmp_path: Path
) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text(
        CONFIG.format(base_url=model_server.base_url, python=json.dumps(sys.executable))
    )
    environment = {
        **os.environ,
        "ROBINAUTS_CONFIG": str(config),
        "LOCAL_CLAUDE_KEY": "not-a-key",
        "ROBINAUTS_FABRIC_WORKSPACES": str(tmp_path / "workspaces"),
        "ROBINAUTS_FABRIC_STATE": str(tmp_path / "state"),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
        "ADAPTER_PYTHON": sys.executable,
    }
    for variable in ("ROBINAUTS_DATABASE_URL", "GITHUB_TOKEN", "ROBINAUTS_FLOWS_DIR"):
        environment.pop(variable, None)
    port = free_port()
    command = [str(ROBINAUTS), "start", "--local", "--port", str(port)]
    command += ["--worker-port", str(free_port())]
    secret = new_secret(CredentialKind.LAUNCH)
    server = launched(command, environment, secret)
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=180.0) as api:
            wait_until_up(api, server)
            signed_in(api, secret)
            started = api.post("/robinauts/api/turns", json={"agent_id": "coder", "text": "hello"})
            finished(started)
            cid = started.headers["x-robinauts-conversation-id"]
            first_answer = messages(api, cid)[1]["id"]
            finished(
                api.post(
                    f"/robinauts/api/conversations/{cid}/turns",
                    json={"parent_id": first_answer, "text": "bash: echo from-the-tool"},
                )
            )
            thread = messages(api, cid)
            assert [m["role"] for m in thread] == ["user", "assistant", "user", "assistant"]
            assert text_of(thread[1]) == "hello"
            assert "ran: from-the-tool" in text_of(thread[3])
            # The tool call and its result are in the chat, as for any engine.
            shown = json.dumps(thread[3]["parts"])
            assert "Bash" in shown
            assert "from-the-tool" in shown

            # An edit of the second question: a runtime seeded with the first turn alone.
            finished(
                api.post(
                    f"/robinauts/api/conversations/{cid}/turns",
                    json={"parent_id": first_answer, "text": "an edit"},
                )
            )
            assert text_of(messages(api, cid)[-1]) == "an edit"
            seeded = system_text(model_server.conversations()[-1])
            assert "hello" in seeded
            assert "from-the-tool" not in seeded
        server.send_signal(signal.SIGTERM)
        _, said = server.communicate(timeout=60)
    finally:
        if server.poll() is None:
            server.kill()
    assert b"the controller is closed" in said, said.decode(errors="replace")

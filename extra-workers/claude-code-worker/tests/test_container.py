# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The worker in its container holds a conversation on Claude Code: a server with no database
starts it from its image with ``docker run -i``, as its configuration says; the turns of the
agent on that worker run in the container, where Claude Code, the binary the SDK bundles, talks
to a stand-in of Anthropic's Messages API on this machine. The second turn continues the first:
Claude Code resumed the session its transcripts hold, in the container. The worker runs in
deployed mode on a key that is nothing but set, is handed no database, and is stopped with the
container gone.

Skipped without Docker on Linux, where ``--network host`` lets the container reach this
machine's loopback. The image is built here unless ``ROBINAUTS_CLAUDE_CODE_WORKER_IMAGE``
names one already built.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.fake_anthropic import FakeAnthropicServer, history, in_order

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
"""The repository: the image's build context."""

ROBINAUTS = Path(sys.executable).with_name("robinauts")
"""The server, from this project's environment: the same build the image carries."""

IMAGE_VARIABLE = "ROBINAUTS_CLAUDE_CODE_WORKER_IMAGE"
CONTAINER = "robinauts-claude-code-worker-test"

CONFIG = """
# Deployed mode, against a stand-in of the Messages API on this machine: an
# anthropic-compatible provider with its base_url, and a key the worker reads by this name.
[model_providers.local_claude]
kind = "anthropic-compatible"
base_url = "{base_url}"
api_key_env = "LOCAL_CLAUDE_KEY"

[models.local_claude]
provider = "local_claude"
name = "claude-sonnet-5-5"
title = "Local Claude"

[agents.coder]
title = "Coder"
system_prompt = "You are a robinaut."
models = ["local_claude"]
engine = "claude-code"
worker = "claude"

# The worker, from its image: the server's three variables, the configuration at the same path,
# the mode, the key of the provider its agent uses, and nothing of a database.
[workers.claude]
command = [
    "docker", "run", "-i", "--rm", "--network", "host", "--name", "{container}",
    "-e", "ROBINAUTS_CONFIG", "-e", "ROBINAUTS_WORKER_NAME", "-e", "ROBINAUTS_WORKER_BROKER_URL",
    "-e", "ROBINAUTS_CLAUDE_AUTH=deployed", "-e", "LOCAL_CLAUDE_KEY",
    "-v", "{config}:{config}:ro",
    "{image}",
]
"""

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("docker") is None,
    reason="runs the worker's image with Docker, on Linux, where it reaches this machine",
)


def docker_runs() -> bool:
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


@pytest.fixture(scope="session")
def image() -> str:
    """The worker's image: the one named, or the one built from this checkout."""
    if not docker_runs():
        pytest.skip("Docker is not running here")
    if named := os.environ.get(IMAGE_VARIABLE):
        return named
    tag = "robinauts-claude-code-worker:test"
    subprocess.run(
        ["docker", "build", "--quiet", "-f", str(HERE.parent / "Dockerfile"), "-t", tag, str(ROOT)],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return tag


@pytest.fixture
def model_server() -> Iterator[FakeAnthropicServer]:
    with FakeAnthropicServer() as running:
        yield running


def free_port() -> int:
    import socket

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


def thread(api: httpx.Client, cid: str) -> list[tuple[str, str]]:
    messages = api.get(f"/api/conversations/{cid}").json()["messages"]
    return [(m["role"], "".join(p["text"] for p in m["parts"])) for m in messages]


def container_running() -> bool:
    found = subprocess.run(
        ["docker", "ps", "--quiet", "--filter", f"name=^{CONTAINER}$"], capture_output=True
    )
    return bool(found.stdout.strip())


def test_the_worker_in_its_container_holds_a_conversation_on_claude_code(
    image: str, model_server: FakeAnthropicServer, tmp_path: Path
) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text(
        CONFIG.format(
            base_url=model_server.base_url, container=CONTAINER, config=config, image=image
        )
    )
    environment = {**os.environ, "ROBINAUTS_CONFIG": str(config), "LOCAL_CLAUDE_KEY": "not-a-key"}
    environment.pop("ROBINAUTS_DATABASE_URL", None)
    port = free_port()
    command = [str(ROBINAUTS), "start", "--dev-no-sign-in", "--port", str(port)]
    with subprocess.Popen(command, env=environment, stderr=subprocess.PIPE) as server:
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=180.0) as api:
                wait_until_up(api, server)
                assert container_running()
                started = api.post("/api/turns", json={"agent_id": "coder", "text": "hello"})
                finished(started)
                cid = started.headers["x-robinauts-conversation-id"]
                first_answer = api.get(f"/api/conversations/{cid}").json()["messages"][1]["id"]
                finished(
                    api.post(
                        f"/api/conversations/{cid}/turns",
                        json={"parent_id": first_answer, "text": "and again"},
                    )
                )
                assert thread(api, cid) == [
                    ("user", "hello"),
                    ("assistant", "hello"),
                    ("user", "and again"),
                    ("assistant", "and again"),
                ]
                # The second turn carried the first: Claude Code resumed the session whose
                # transcript is in the container, and sent the model the whole of it.
                sent = history(model_server.received[-1]["messages"])
                assert in_order(
                    sent, [("user", "hello"), ("assistant", "hello"), ("user", "and again")]
                ), sent
                assert api.get("/health").json() == {"status": "ok"}
            server.send_signal(signal.SIGTERM)
            _, said = server.communicate(timeout=60)
        finally:
            if server.poll() is None:
                server.kill()
            subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    assert b"the controller is closed" in said, said.decode(errors="replace")
    assert not container_running()


def wait_until_up(api: httpx.Client, server: subprocess.Popen[bytes]) -> None:
    """Until the server answers: a start that pulls nothing still waits for the container."""
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

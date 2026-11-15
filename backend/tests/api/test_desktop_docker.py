# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The desktop with Docker: ``robinauts desktop`` for a configuration with one worker in a
container, on the real Docker daemon, with a stand-in browser in the window's place
(``util/desktop.py``).

oneberth's launcher creates the networks, then runs the backend in a container of Robinauts'
image, the base worker in a container of its own image, and the default worker as a process of
the host. The person lands signed in on the front listener. A turn runs on each worker; the
container's model server is on the host, which it reaches by the host's gateway name. Each
worker's engines keep their state in its own folder of the data directory on the host. Closing
the window leaves no container and no network, and a start again remembers the conversations.
While Docker is down, the desktop refuses to start and names the worker that needs it.

The images are built beforehand, as ``scripts/check-desktop-docker.sh`` builds them, and named
in ``ROBINAUTS_TEST_IMAGE`` and ``ROBINAUTS_BASE_WORKER_IMAGE``.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from util.desktop import Desktop
from util.fake_openai import EchoModel, FakeLocalGPTServer

IMAGE_VARIABLE = "ROBINAUTS_TEST_IMAGE"
WORKER_IMAGE_VARIABLE = "ROBINAUTS_BASE_WORKER_IMAGE"
REQUIRED_VARIABLE = "ROBINAUTS_REQUIRE_DOCKER"

DOCKER = shutil.which("docker")

pytestmark = [
    pytest.mark.io,
    pytest.mark.docker,
    pytest.mark.skipif(
        sys.platform != "linux" or DOCKER is None,
        reason="starts containers with the docker command, on Linux, where the test finds the"
        " desktop's processes",
    ),
]

CONFIG = """
[model_providers.on_host]
kind = "openai-compatible"
base_url = "http://127.0.0.1:{port}/v1"
api_key_env = "LOCAL_GPT_KEY"

# The same model server, as a container reaches the host.
[model_providers.from_container]
kind = "openai-compatible"
base_url = "http://host.docker.internal:{port}/v1"
api_key_env = "LOCAL_GPT_KEY"

[models.on_host]
provider = "on_host"
name = "fake-gpt"

[models.from_container]
provider = "from_container"
name = "fake-gpt"

[agents.on_host]
title = "On the host"
system_prompt = ""
models = ["on_host"]
engine = "langchain"

[agents.in_container]
title = "In a container"
system_prompt = ""
models = ["from_container"]
engine = "pydantic-ai"
worker = "boxed"

[workers.boxed]
image = "{image}"
"""

DOCKER_DOWN = "Docker is not running; it runs the workers 'boxed'. Start Docker, then start again"


def docker(*arguments: str) -> str:
    assert DOCKER is not None
    done = subprocess.run(
        [DOCKER, *arguments], capture_output=True, text=True, timeout=60, check=True
    )
    return done.stdout


def docker_runs() -> bool:
    assert DOCKER is not None
    try:
        found = subprocess.run([DOCKER, "info"], capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        return False
    return found.returncode == 0


def containers_of(data: Path) -> list[str]:
    return docker(
        "ps", "--all", "--format", "{{.Names}}", "--filter", f"label=oneberth.data-dir={data}"
    ).split()


def networks_of(data: Path) -> list[str]:
    return docker(
        "network", "ls", "--format", "{{.Name}}", "--filter", f"label=oneberth.data-dir={data}"
    ).split()


def remove_all(data: Path) -> None:
    """Whatever of the data directory a failed test left in Docker."""
    if containers := containers_of(data):
        docker("rm", "--force", "--volumes", *containers)
    if networks := networks_of(data):
        docker("network", "rm", *networks)


def unmet(reason: str) -> None:
    """A skip, or a failure where ``ROBINAUTS_REQUIRE_DOCKER`` says these tests must run."""
    if os.environ.get(REQUIRED_VARIABLE) == "1":
        pytest.fail(reason)
    pytest.skip(reason)


@pytest.fixture
def images() -> tuple[str, str]:
    """The backend's image and the base worker's, built beforehand."""
    if not docker_runs():
        unmet("Docker is not running here")
    image, worker = os.environ.get(IMAGE_VARIABLE), os.environ.get(WORKER_IMAGE_VARIABLE)
    if not image or not worker:
        unmet(
            f"{IMAGE_VARIABLE} and {WORKER_IMAGE_VARIABLE} name no images:"
            " scripts/check-desktop-docker.sh builds them and runs these tests"
        )
    assert image
    assert worker
    return image, worker


@pytest.fixture
def model_server() -> Iterator[FakeLocalGPTServer]:
    # On every address: the container reaches it at the host's address on Docker's bridge.
    with FakeLocalGPTServer(EchoModel(), host="0.0.0.0") as running:
        yield running


def turn(client: httpx.Client, agent: str, text: str) -> str:
    started = client.post("/robinauts/api/turns", json={"agent_id": agent, "text": text})
    assert '"RUN_FINISHED"' in started.text, started.text
    return started.headers["x-robinauts-conversation-id"]


def thread(client: httpx.Client, conversation: str) -> list[tuple[str, str]]:
    messages = client.get(f"/robinauts/api/conversations/{conversation}").json()["messages"]
    return [(m["role"], "".join(p.get("text", "") for p in m["parts"])) for m in messages]


def test_the_desktop_runs_in_docker_and_leaves_nothing_at_its_stop(
    tmp_path: Path, images: tuple[str, str], model_server: FakeLocalGPTServer
) -> None:
    image, worker_image = images
    config = CONFIG.format(port=model_server.port, image=worker_image)
    desktop = Desktop(
        tmp_path, config, {"LOCAL_GPT_KEY": "not-a-real-key", "ROBINAUTS_IMAGE": image}
    )
    data = desktop.data
    try:
        first = desktop.launch()
        opened = desktop.heard("opened")
        assert (opened["origin"], opened["at"]) == (desktop.front_url, "/ui/")
        prefix = containers_of(data)[0].rsplit("-", 1)[0]
        assert sorted(containers_of(data)) == [f"{prefix}-backend", f"{prefix}-boxed"]
        assert sorted(networks_of(data)) == [f"{prefix}-app", f"{prefix}-sandbox"]
        # The default worker and the window are processes of the host; the backend is not.
        assert sorted(desktop.parts()) == ["default", "window"]

        with desktop.client(opened) as client:
            session = client.get("/auth/session").json()
            assert (session["mode"], session["principal"]["kind"]) == ("local", "user")
            on_host = turn(client, "on_host", "hello")
            in_container = turn(client, "in_container", "hello")
            assert thread(client, in_container) == [("user", "hello"), ("assistant", "hello")]
            assert client.get("/health").json()["status"] == "ok"

        # Each worker's engines keep their state in its folder on the host, the container's
        # as the person's own user.
        assert (data / "workers" / "default" / "langchain.db").is_file()
        boxed = data / "workers" / "boxed" / "pydantic-ai.db"
        assert boxed.is_file()
        assert boxed.stat().st_uid == os.getuid()
        assert (data / "workers" / "boxed" / "robinauts.toml").read_text() == config

        # Closing the window stops the application: no container and no network stay.
        os.kill(int(opened["pid"]), signal.SIGTERM)
        assert desktop.ended(first) == 0
        assert containers_of(data) == []
        assert networks_of(data) == []
        assert desktop.left_behind() == []

        # A start again remembers the conversations, and the container's engine its state.
        again = desktop.launch()
        reopened = desktop.heard("opened")
        assert reopened["origin"] == opened["origin"]
        with desktop.client(reopened) as client:
            listed = client.get("/robinauts/api/conversations").json()["items"]
            assert {c["id"] for c in listed} == {on_host, in_container}
            sent = client.post(
                f"/robinauts/api/conversations/{in_container}/turns",
                json={"parent_id": answer_id(client, in_container), "text": "and again"},
            )
            assert '"RUN_FINISHED"' in sent.text, sent.text
            assert thread(client, in_container) == [
                ("user", "hello"),
                ("assistant", "hello"),
                ("user", "and again"),
                ("assistant", "and again"),
            ]
        again.send_signal(signal.SIGTERM)
        assert desktop.ended(again) == 0
        assert containers_of(data) == []
        assert networks_of(data) == []
        assert desktop.left_behind() == []
    finally:
        desktop.stop_all()
        remove_all(data)


def answer_id(client: httpx.Client, conversation: str) -> str:
    messages = client.get(f"/robinauts/api/conversations/{conversation}").json()["messages"]
    return str(messages[-1]["id"])


def test_while_docker_is_down_the_desktop_refuses_and_names_the_worker(tmp_path: Path) -> None:
    config = CONFIG.format(port=1, image="robinauts-base-worker")
    desktop = Desktop(tmp_path, config, {"DOCKER_HOST": f"unix://{tmp_path / 'no-docker.sock'}"})
    try:
        launched = desktop.launch()
        assert desktop.ended(launched) == 1
        assert DOCKER_DOWN in desktop.log.read_text()
        assert not (desktop.data / "launcher.json").exists()
        assert desktop.left_behind() == []
    finally:
        desktop.stop_all()

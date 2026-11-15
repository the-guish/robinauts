# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The desktop Robinauts describes to oneberth's launcher (``cli/desktop.py``), driven with the
launcher's stand-ins in memory: the parts, their roles and resources, their environment and the
order of the starts and the stops. Also where the data directory is."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from oneberth import launcher
from oneberth.launcher.testing import StandIns

from robinauts.cli.desktop import (
    FRONT_PORT,
    copy_configuration,
    default_data_dir,
    desktop_application,
)
from robinauts.shared_domain import domain as shared
from util.aio import asyncio_test

HOME = Path("/home/ada")

ROBINAUTS = ("/venv/bin/python", "-m", "robinauts.cli")
PYTHON = "/venv/bin/python"
DATA = Path("/data")
CONFIG_FILE = Path("/data/robinauts.toml")
FLOWS = Path("/flows")


def agent(name: str, worker: str) -> shared.AgentConfig:
    return shared.AgentConfig(name, name, "", ("m",), "echo", worker=worker)


CONFIG = shared.Config(
    providers={"acme": shared.ProviderConfig("acme", shared.ProviderKind.ANTHROPIC, "ACME_KEY")},
    models={"m": shared.ModelConfig("m", "acme", "model-1")},
    agents={a.id: a for a in (agent("a", "default"), agent("b", "gpu"), agent("c", "claude"))},
    commands={"claude": ("docker", "run", "-i", "claude-worker")},
)

SHARED = {
    "ROBINAUTS_CONFIG": str(CONFIG_FILE),
    "ROBINAUTS_DATABASE_URL": "",
    "ONEBERTH_CONFIG": "",
}


@pytest.mark.parametrize(
    ("platform", "environ", "expected"),
    [
        ("linux", {}, HOME / ".local/share/robinauts"),
        ("linux", {"XDG_DATA_HOME": "/xdg"}, Path("/xdg/robinauts")),
        ("linux", {"XDG_DATA_HOME": "relative"}, HOME / ".local/share/robinauts"),
        ("linux", {"XDG_DATA_HOME": ""}, HOME / ".local/share/robinauts"),
        ("darwin", {"XDG_DATA_HOME": "/xdg"}, HOME / "Library/Application Support/Robinauts"),
        ("freebsd13", {}, HOME / ".local/share/robinauts"),
    ],
)
def test_the_data_directory_of_each_platform(
    platform: str, environ: dict[str, str], expected: Path
) -> None:
    assert default_data_dir(platform, environ, HOME) == expected


@asyncio_test
async def test_the_desktop_starts_the_backend_then_each_worker_then_the_window() -> None:
    application = desktop_application(
        CONFIG,
        data_dir=DATA,
        config_file=CONFIG_FILE,
        flows=FLOWS,
        robinauts=ROBINAUTS,
        python=PYTHON,
        browser="/usr/bin/chromium",
        arguments=("--headless=new",),
    )
    assert (application.name, application.data, application.front_port) == (
        "robinauts",
        DATA,
        FRONT_PORT,
    )
    assert application.window == launcher.Window("/usr/bin/chromium", ("--headless=new",))
    stand_ins = StandIns()
    running = asyncio.create_task(stand_ins.launcher(application).run())
    window = await stand_ins.process(launcher.WINDOW)

    assert stand_ins.journal == [
        "prepare",
        "start backend",
        "token default robinauts/worker default",
        "token claude robinauts/worker claude",
        "token gpu robinauts/worker gpu",
        "token neorc neorc/worker default",
        "start default",
        "start claude",
        "start gpu",
        "start neorc",
        "ticket",
        "start window",
    ]
    parts = stand_ins.runtime.processes
    flows = {"ROBINAUTS_FLOWS_DIR": str(FLOWS)}
    assert parts["backend"].run == launcher.Process(
        (*ROBINAUTS, "desktop-backend"), {**SHARED, **flows}
    )
    assert parts["backend"].stdin == stand_ins.api.secrets[0]
    assert parts["backend"].environment == {
        "ONEBERTH_DATA_DIR": str(DATA),
        "ONEBERTH_LAUNCHER_PID": "1",
        "ONEBERTH_HOST": "127.0.0.1",
        "ONEBERTH_FRONT_PORT": "8420",
        "ONEBERTH_WORKER_PORT": "8421",
        "ONEBERTH_FRONT_BIND": "127.0.0.1",
        "ONEBERTH_WORKER_BIND": "127.0.0.1",
    }
    module = (PYTHON, "-m", "robinauts.cli.worker_process")
    commands = {
        "default": module,
        "gpu": module,
        "claude": ("docker", "run", "-i", "claude-worker"),
        "neorc": (*ROBINAUTS, "neorc-worker"),
    }
    for name, command in commands.items():
        assert parts[name].run == launcher.Process(command, {**SHARED, **flows}), name
        assert parts[name].stdin == f"token-{name}"
        assert parts[name].environment == {
            "ONEBERTH_DATA_DIR": str(DATA),
            "ONEBERTH_LAUNCHER_PID": "1",
            "ONEBERTH_WORKER_NAME": name,
            "ONEBERTH_WORKER_URL": "http://127.0.0.1:8421",
            "ONEBERTH_FRONT_URL": "http://127.0.0.1:8420",
        }, name
    assert parts["window"].run == launcher.Process(
        ("browser", "--app=http://127.0.0.1:8420/auth/local?ticket=t1")
    )

    # Closing the window stops the workers, then the backend.
    stand_ins.journal.clear()
    window.exit(0)
    outcome = await running
    assert outcome.ending is launcher.Ending.WINDOW_CLOSED
    assert stand_ins.journal[:4] == [
        "stop default",
        "stop claude",
        "stop gpu",
        "stop neorc",
    ]
    assert stand_ins.journal[4:] == ["stop backend", "release"]


def test_without_a_flows_folder_neorc_s_worker_is_not_a_part() -> None:
    application = desktop_application(
        shared.Config(), data_dir=DATA, config_file=CONFIG_FILE, robinauts=ROBINAUTS
    )
    assert [w.name for w in application.workers] == ["default"]
    assert application.backend.run == launcher.Process(
        (*ROBINAUTS, "desktop-backend"), {**SHARED, "ROBINAUTS_FLOWS_DIR": ""}
    )
    assert application.window == launcher.Window()


def test_an_external_worker_is_not_a_part() -> None:
    config = shared.Config(agents=CONFIG.agents, externals=frozenset({"gpu"}))
    application = desktop_application(config, data_dir=DATA, config_file=CONFIG_FILE)
    assert [w.name for w in application.workers] == ["default", "claude"]


def test_a_worker_named_as_a_part_of_the_launcher_is_refused() -> None:
    config = shared.Config(agents={"a": agent("a", "backend")})
    with pytest.raises(launcher.LauncherError, match="'backend'"):
        desktop_application(config, data_dir=DATA, config_file=CONFIG_FILE)


DOCKER_CONFIG = shared.Config(
    providers={
        "acme": shared.ProviderConfig("acme", shared.ProviderKind.ANTHROPIC, "ACME_KEY"),
        "other": shared.ProviderConfig("other", shared.ProviderKind.OPENAI, "OTHER_KEY"),
    },
    models={"m": shared.ModelConfig("m", "acme", "model-1")},
    tool_servers={
        "search": shared.ToolServerConfig("search", "https://search.example", "SEARCH_TOKEN")
    },
    agents={
        "a": agent("a", "default"),
        "b": agent("b", "gpu"),
        "c": shared.AgentConfig("c", "c", "", ("m",), "claude-code", ("search",), worker="claude"),
    },
    commands={"gpu": ("gpu-worker",), "claude": ("docker", "run", "-i", "claude-worker")},
    containers={
        "claude": shared.ContainerConfig(
            "robinauts-claude-code-worker:1", ("CLAUDE_CODE_OAUTH_TOKEN", "ROBINAUTS_CLAUDE_AUTH")
        )
    },
)

ENVIRON = {
    "ACME_KEY": "acme-key",
    "OTHER_KEY": "other-key",
    "SEARCH_TOKEN": "search-token",
    "CLAUDE_CODE_OAUTH_TOKEN": "oauth-token",
    "UNRELATED": "not passed on",
}


def mounts(plan: launcher.ContainerPlan) -> list[tuple[Path, str, bool, bool]]:
    """A container's mounts: the host's directory, where the container sees it, whether it
    is read-only, and whether the launcher makes it before the start."""
    return [(m.source, m.target, m.read_only, m.create) for m in plan.mounts]


@asyncio_test
async def test_a_worker_in_a_container_puts_the_desktop_in_docker(tmp_path: Path) -> None:
    data = tmp_path / "data"
    flows, code = tmp_path / "flows", tmp_path / "handlers"
    application = desktop_application(
        DOCKER_CONFIG,
        data_dir=data,
        config_file=data / "robinauts.toml",
        flows=flows,
        code=code,
        robinauts=ROBINAUTS,
        python=PYTHON,
        image="robinauts:test",
        environ=ENVIRON,
    )
    assert launcher.shape_of(application) is launcher.Shape.DOCKER
    stand_ins = StandIns()
    running = asyncio.create_task(stand_ins.launcher(application).run())
    window = await stand_ins.process(launcher.WINDOW)
    started = list(stand_ins.journal)
    # Closed before anything is checked, so that a failed check leaves no launcher running.
    window.exit(0)
    async with asyncio.timeout(10):
        outcome = await running
    assert outcome.ending is launcher.Ending.WINDOW_CLOSED
    app, sandbox = (launcher.network_name(data, n) for n in (launcher.APP, launcher.SANDBOX))
    assert started == [
        f"network {app}",
        f"network {sandbox}",
        "start backend",
        "token default robinauts/worker default",
        "token claude robinauts/worker claude",
        "token gpu robinauts/worker gpu",
        "token neorc neorc/worker default",
        "start default",
        "start claude",
        "start gpu",
        "start neorc",
        "ticket",
        "start window",
    ]
    assert stand_ins.journal[len(started) :] == [
        "stop default",
        "stop claude",
        "stop gpu",
        "stop neorc",
        "stop backend",
        f"remove networks {app}, {sandbox}",
    ]
    assert stand_ins.docker.containers == {}
    assert stand_ins.docker.networks == {}
    assert not data.exists()

    # The backend, in its container on both networks: the data directory at /data, the flows
    # and the handlers where the host has them, every key of the configuration.
    backend = stand_ins.docker.plans["backend"]
    assert (backend.image, backend.command) == ("robinauts:test", ("robinauts", "desktop-backend"))
    assert backend.environment == {
        "ROBINAUTS_CONFIG": "/data/robinauts.toml",
        "ROBINAUTS_DATABASE_URL": "",
        "ONEBERTH_CONFIG": "",
        "ROBINAUTS_FLOWS_DIR": str(flows),
        "ACME_KEY": "acme-key",
        "OTHER_KEY": "other-key",
        "SEARCH_TOKEN": "search-token",
        "ONEBERTH_DATA_DIR": "/data",
        "ONEBERTH_HOST": "127.0.0.1",
        "ONEBERTH_FRONT_PORT": "8420",
        "ONEBERTH_WORKER_PORT": "8421",
        "ONEBERTH_FRONT_BIND": "front",
        "ONEBERTH_WORKER_BIND": "0.0.0.0",
    }
    assert mounts(backend) == [
        (data, "/data", False, True),
        (flows, str(flows), True, False),
        (code, str(code), True, False),
    ]
    assert [j.network for j in backend.networks] == [app, sandbox]
    assert [(p.address, p.port) for p in backend.published] == [
        ("127.0.0.1", 8420),
        ("127.0.0.1", 8421),
    ]
    assert (backend.gateway, backend.user) == (True, "1000:1000")
    assert stand_ins.runtime.processes["backend"].stdin == stand_ins.api.secrets[0]

    # The container worker, on the network with a way out: its own folder, the copy of the
    # configuration in it, the keys of its agents and the variables its table names.
    claude = stand_ins.docker.plans["claude"]
    assert (claude.image, claude.command) == ("robinauts-claude-code-worker:1", ())
    assert claude.environment == {
        "ROBINAUTS_CONFIG": "/data/workers/claude/robinauts.toml",
        "ROBINAUTS_DATA_DIR": "/data",
        "HOME": "/data/workers/claude",
        "ACME_KEY": "acme-key",
        "SEARCH_TOKEN": "search-token",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-token",
        "ONEBERTH_WORKER_NAME": "claude",
        "ONEBERTH_WORKER_URL": "http://10.0.1.2:8421",
        "ONEBERTH_FRONT_URL": "http://10.0.1.2:8420",
    }
    assert mounts(claude) == [(data / "workers" / "claude", "/data/workers/claude", False, True)]
    assert [j.network for j in claude.networks] == [app]
    assert (claude.published, claude.gateway, claude.user) == ((), True, "1000:1000")
    assert stand_ins.runtime.processes["claude"].stdin == "token-claude"

    # The other workers are processes of the host, on the published worker listener.
    flows_variable = {"ROBINAUTS_FLOWS_DIR": str(flows)}
    commands = {
        "default": (PYTHON, "-m", "robinauts.cli.worker_process"),
        "gpu": ("gpu-worker",),
        "neorc": (*ROBINAUTS, "neorc-worker"),
    }
    for name, command in commands.items():
        part = stand_ins.runtime.processes[name]
        shared_variables = {**SHARED, "ROBINAUTS_CONFIG": str(data / "robinauts.toml")}
        assert part.run == launcher.Process(command, {**shared_variables, **flows_variable}), name
        assert part.environment == {
            "ONEBERTH_DATA_DIR": str(data),
            "ONEBERTH_LAUNCHER_PID": "1",
            "ONEBERTH_WORKER_NAME": name,
            "ONEBERTH_WORKER_URL": "http://127.0.0.1:8421",
            "ONEBERTH_FRONT_URL": "http://127.0.0.1:8420",
        }, name


def test_the_backend_s_container_reads_a_configuration_outside_the_data_directory(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "etc" / "robinauts.toml"
    application = desktop_application(
        DOCKER_CONFIG, data_dir=tmp_path / "data", config_file=config_file, environ={}
    )
    container = application.backend.container
    assert container is not None
    assert container.mounts == (launcher.Mount(config_file, "/etc/robinauts/robinauts.toml", True),)
    assert container.environment["ROBINAUTS_CONFIG"] == "/etc/robinauts/robinauts.toml"
    assert container.image == "robinauts"
    claude = application.workers[1].run
    assert isinstance(claude, launcher.Container)
    assert claude.environment == {
        "ROBINAUTS_CONFIG": "/data/workers/claude/robinauts.toml",
        "ROBINAUTS_DATA_DIR": "/data",
        "HOME": "/data/workers/claude",
    }


def test_each_container_worker_gets_a_copy_of_the_configuration(tmp_path: Path) -> None:
    config_file = tmp_path / "robinauts.toml"
    config_file.write_text("# the configuration\n")
    data = tmp_path / "data"
    assert copy_configuration(DOCKER_CONFIG, config_file, data) == [
        data / "workers" / "claude" / "robinauts.toml"
    ]
    assert (data / "workers" / "claude" / "robinauts.toml").read_text() == "# the configuration\n"
    assert copy_configuration(CONFIG, config_file, data) == []

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

from robinauts.cli.desktop import FRONT_PORT, default_data_dir, desktop_application
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


def test_a_worker_named_as_a_part_of_the_launcher_is_refused() -> None:
    config = shared.Config(agents={"a": agent("a", "backend")})
    with pytest.raises(launcher.LauncherError, match="'backend'"):
        desktop_application(config, data_dir=DATA, config_file=CONFIG_FILE)

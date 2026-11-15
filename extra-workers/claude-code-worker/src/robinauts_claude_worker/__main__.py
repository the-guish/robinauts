# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The worker's process, ``python -m robinauts_claude_worker``: the image's command.

Its supervisor starts it: the server, as it starts every worker whose ``[workers.<name>]`` table
names a command, with its own environment plus the worker's name and the broker's URL; or the
desktop's launcher, as a container of the image its table names, with the launcher's variables
(``ONEBERTH_WORKER_URL``, ``ONEBERTH_WORKER_NAME``). Either way the secret comes on its standard
input, and the line ``ready`` is expected on its standard output. Under the launcher, Claude
Code's transcripts and the workspaces are in the worker's own folder of the data directory.

The worker is the backend's own worker module, ``robinauts.cli.worker_process``, with the
``claude-code`` engine plugged in through its entry point; this module runs in front of it
alone, to check what that module cannot know: that this worker's environment is one of the two
credential modes (CREDENTIALS.md), that the configuration gives it agents on its engine, that no
database was handed to it, and what every extra worker checks (robinauts_worker_common: the
GitHub token it may be handed). What it refuses, it says all at once on its standard error and
exits 2, before the secret is read.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

from robinauts.agent_engines.contract.domain import ModelProviderConfig
from robinauts.cli.composition import DATA_DIR_VARIABLE, load
from robinauts.cli.worker_process import (
    BROKER_URL_VARIABLE,
    LAUNCHED_DATA_DIR_VARIABLE,
    WORKER_URL_VARIABLE,
    broker_url,
    worker_name,
)
from robinauts.cli.worker_process import main as run_worker
from robinauts.controller.contract.domain import ConfigError
from robinauts.shared_domain.domain import Config
from robinauts.worker.engine_settings import engine_settings
from robinauts_claude_worker import engine
from robinauts_claude_worker.credentials import credential_problems
from robinauts_claude_worker.settings import WORKSPACES_VARIABLE, SettingsError, settings_from
from robinauts_worker_common.__main__ import problems_of as shared_problems

CONFIG_VARIABLE = "ROBINAUTS_CONFIG"
"""The deployment's TOML file, the same the server reads: this worker's agents are in it."""


def environment_problems(environ: Mapping[str, str]) -> list[str]:
    """The supervisor's variables the worker module cannot start without."""
    problems = []
    if not environ.get(CONFIG_VARIABLE, "").strip():
        problems.append(
            f"{CONFIG_VARIABLE} is not set: the server sets it when it starts this worker, and the"
            " command passes it on"
        )
    if (
        not environ.get(WORKER_URL_VARIABLE, "").strip()
        and not environ.get(BROKER_URL_VARIABLE, "").strip()
    ):
        problems.append(
            f"{BROKER_URL_VARIABLE} is not set: the server sets it when it starts this worker, and"
            f" the command passes it on; the desktop's launcher sets {WORKER_URL_VARIABLE}"
        )
    return problems


def launched_state(environ: Mapping[str, str]) -> dict[str, str]:
    """Where Claude Code's transcripts and the workspaces go when the desktop's launcher started
    this worker: its own folder of the data directory, which outlives the container. Nothing
    under a server, whose command names the volumes."""
    data = environ.get(LAUNCHED_DATA_DIR_VARIABLE) or environ.get(DATA_DIR_VARIABLE)
    if not environ.get(WORKER_URL_VARIABLE) or not data:
        return {}
    folder = Path(data) / "workers" / worker_name(environ)
    return {
        engine.CONFIG_DIR_VARIABLE: str(folder / "claude"),
        WORKSPACES_VARIABLE: str(folder / "workspaces"),
    }


def configuration_problems(name: str, config: Config) -> list[str]:
    """What the deployment's file says against running this worker here."""
    problems = []
    if name not in config.workers():
        problems.append(f"no agent of the configuration names worker {name!r}")
    for agent in config.for_worker(name).agents.values():
        if agent.engine != engine.ENGINE:
            problems.append(
                f"agent {agent.id!r} runs on engine {agent.engine!r}, and this worker's"
                f" credential checks cover {engine.ENGINE!r} alone: give it another worker"
            )
    return problems


def providers_of(name: str, config: Config) -> list[ModelProviderConfig]:
    """The providers this worker's agents' models are on, as the engines see them."""
    models = engine_settings(config, {}.get).models
    mine = config.for_worker(name)
    agents = mine.agents.values()
    ids = sorted({config.models[m].provider for agent in agents for m in agent.models})
    return [models.providers[provider_id] for provider_id in ids]


def problems_of(environ: Mapping[str, str]) -> list[str]:
    """Everything that keeps this worker from starting, read from its environment and the
    configuration that environment names: one line each, none for a start that may go on."""
    problems = environment_problems(environ) + shared_problems(environ)
    try:
        settings = settings_from(environ)
    except SettingsError as refused:
        problems.extend(str(refused).splitlines())
    if problems:
        return problems
    try:
        config, _ = load(Path(environ[CONFIG_VARIABLE]), environ)
    except (ConfigError, OSError, ValueError) as refused:
        return [f"{CONFIG_VARIABLE}: {refused}"]
    name = worker_name(environ)
    problems = configuration_problems(name, config)
    if problems:
        return problems
    url = broker_url(environ)
    return credential_problems(settings.auth, environ, url, providers_of(name, config))


def main() -> int:
    os.environ.update(launched_state(os.environ))
    problems = problems_of(os.environ)
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 2
    run_worker()
    return 0


if __name__ == "__main__":
    sys.exit(main())

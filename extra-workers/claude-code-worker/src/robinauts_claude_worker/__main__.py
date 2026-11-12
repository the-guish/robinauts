# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The worker's process, ``python -m robinauts_claude_worker``: the image's command.

The server starts it as it starts every worker whose ``[workers.<name>]`` table names a command:
with its own environment plus the worker's name and the broker's URL, the secret on its standard
input, and the line ``ready`` expected on its standard output. The worker is the backend's own
worker module, ``robinauts.cli.worker_process``, with the ``claude-code`` engine plugged in
through its entry point; this module runs in front of it alone, to check what that module cannot
know: that this worker's environment is one of the two credential modes (CREDENTIALS.md), that
the configuration gives it agents on its engine, that no database was handed to it, and what
every extra worker checks (robinauts_worker_common: the GitHub token it may be handed). What it
refuses, it says all at once on its standard error and exits 2, before the secret is read.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

from robinauts.agent_engines.contract.domain import ModelProviderConfig
from robinauts.cli.composition import load
from robinauts.cli.worker_process import BROKER_URL_VARIABLE, WORKER_NAME_VARIABLE
from robinauts.cli.worker_process import main as run_worker
from robinauts.controller.contract.domain import ConfigError
from robinauts.shared_domain.domain import DEFAULT_WORKER, Config
from robinauts.worker.engine_settings import engine_settings
from robinauts_claude_worker import engine
from robinauts_claude_worker.credentials import credential_problems
from robinauts_claude_worker.settings import SettingsError, settings_from
from robinauts_worker_common.__main__ import problems_of as shared_problems

CONFIG_VARIABLE = "ROBINAUTS_CONFIG"
"""The deployment's TOML file, the same the server reads: this worker's agents are in it."""

SERVER_VARIABLES = (CONFIG_VARIABLE, BROKER_URL_VARIABLE)
"""What the server's environment holds when it starts a worker, and the command passes on."""


def environment_problems(environ: Mapping[str, str]) -> list[str]:
    """The server's variables the worker module cannot start without."""
    return [
        f"{variable} is not set: the server sets it when it starts this worker, and the"
        " command passes it on"
        for variable in SERVER_VARIABLES
        if not environ.get(variable, "").strip()
    ]


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
    name = environ.get(WORKER_NAME_VARIABLE) or DEFAULT_WORKER
    problems = configuration_problems(name, config)
    if problems:
        return problems
    url = environ[BROKER_URL_VARIABLE]
    return credential_problems(settings.auth, environ, url, providers_of(name, config))


def main() -> int:
    problems = problems_of(os.environ)
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 2
    run_worker()
    return 0


if __name__ == "__main__":
    sys.exit(main())

# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The worker's process, ``python -m robinauts_fabric_worker``: the image's command.

Its supervisor starts it: the server, as it starts every worker whose ``[workers.<name>]`` table
names a command, with its own environment plus the worker's name and the broker's URL; or the
desktop's launcher, as a container of the image its table names, with the launcher's variables
(``ONEBERTH_WORKER_URL``, ``ONEBERTH_WORKER_NAME``). Either way the secret comes on its standard
input, and the line ``ready`` is expected on its standard output. Under the launcher, the
workspaces, the engine's state and Claude Code's configuration are in the worker's own folder of
the data directory.

The worker is the backend's own worker module, ``robinauts.cli.worker_process``, with the
``fabric`` engine plugged in through its entry point; this module runs in front of it alone, to
check what that module cannot know: that the configuration gives it agents on its engine, on
providers Claude Code reaches, whose keys are set; that no database was handed to it; and what
every extra worker checks of the GitHub token it may be handed (robinauts_worker_common). What it
refuses, it says all at once on its standard error and exits 2, before the secret is read.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

from robinauts.agent_engines.contract.domain import ModelProviderConfig, ProviderKind
from robinauts.cli.composition import DATA_DIR_VARIABLE, load
from robinauts.cli.worker_process import (
    BROKER_URL_VARIABLE,
    LAUNCHED_DATA_DIR_VARIABLE,
    WORKER_URL_VARIABLE,
    worker_name,
)
from robinauts.cli.worker_process import main as run_worker
from robinauts.controller.contract.domain import ConfigError
from robinauts.shared_domain.domain import Config
from robinauts.worker.engine_settings import engine_settings
from robinauts_fabric_worker import engine
from robinauts_fabric_worker.settings import (
    STATE_VARIABLE,
    WORKSPACES_VARIABLE,
    SettingsError,
    settings_from,
)
from robinauts_worker_common.github import token_problems

CONFIG_VARIABLE = "ROBINAUTS_CONFIG"
"""The deployment's TOML file, the same the server reads: this worker's agents are in it."""
CLAUDE_CONFIG_VARIABLE = "CLAUDE_CONFIG_DIR"
OAUTH_TOKEN_VARIABLE = "CLAUDE_CODE_OAUTH_TOKEN"


def environment_problems(environ: Mapping[str, str]) -> list[str]:
    """The supervisor's variables the worker module cannot start without, and the credential
    this worker does not take."""
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
    if environ.get(OAUTH_TOKEN_VARIABLE, "").strip():
        problems.append(
            f"{OAUTH_TOKEN_VARIABLE} is set: this worker runs Claude Code on the provider's API key"
            " alone; a subscription token is the Claude Code worker's personal mode"
        )
    return problems


def launched_state(environ: Mapping[str, str]) -> dict[str, str]:
    """Where the workspaces, the engine's state and Claude Code's configuration go when the
    desktop's launcher started this worker: its own folder of the data directory, which outlives
    the container. Nothing under a server, whose command names the volumes."""
    data = environ.get(LAUNCHED_DATA_DIR_VARIABLE) or environ.get(DATA_DIR_VARIABLE)
    if not environ.get(WORKER_URL_VARIABLE) or not data:
        return {}
    folder = Path(data) / "workers" / worker_name(environ)
    return {
        CLAUDE_CONFIG_VARIABLE: str(folder / "claude"),
        WORKSPACES_VARIABLE: str(folder / "workspaces"),
        STATE_VARIABLE: str(folder / "state"),
    }


def configuration_problems(name: str, config: Config) -> list[str]:
    """What the deployment's file says against running this worker here."""
    problems = []
    if name not in config.workers():
        problems.append(f"no agent of the configuration names worker {name!r}")
    for agent in config.for_worker(name).agents.values():
        if agent.engine != engine.ENGINE:
            problems.append(
                f"agent {agent.id!r} runs on engine {agent.engine!r}, and this worker runs"
                f" {engine.ENGINE!r} alone: give it another worker"
            )
    return problems


def providers_of(name: str, config: Config) -> list[ModelProviderConfig]:
    """The providers this worker's agents' models are on, as the engines see them."""
    models = engine_settings(config, {}.get).models
    agents = config.for_worker(name).agents.values()
    ids = sorted({config.models[m].provider for agent in agents for m in agent.models})
    return [models.providers[provider_id] for provider_id in ids]


def credential_problems(
    environ: Mapping[str, str], providers: list[ModelProviderConfig]
) -> list[str]:
    """Each provider must be one Claude Code reaches, with its key set where its ``api_key_env``
    says. The worker reads names and checks that they are set; it never makes a key."""
    problems = []
    for provider in providers:
        if provider.kind not in (ProviderKind.ANTHROPIC, ProviderKind.ANTHROPIC_COMPATIBLE):
            problems.append(
                f"provider {provider.id!r} is of kind {provider.kind.value!r}, which Claude Code"
                " cannot reach"
            )
        elif not environ.get(provider.api_key_env, "").strip():
            problems.append(
                f"provider {provider.id!r} reads its API key from {provider.api_key_env}, which is"
                " unset or empty"
            )
    return problems


def problems_of(environ: Mapping[str, str]) -> list[str]:
    """Everything that keeps this worker from starting, read from its environment and the
    configuration that environment names: one line each, none for a start that may go on."""
    problems = environment_problems(environ) + token_problems(environ)
    try:
        settings_from(environ)
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
    return credential_problems(environ, providers_of(name, config))


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

# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The worker's process: ``python -m robinauts_claude_worker``.

It reads the deployment's configuration, as the server does, and runs the one worker its
environment names, which the server's ``[workers.<name>]`` table says is external: its agents'
turns, pulled from the server over HTTP with the secret it presents, run on the Claude Code
engine. It is the backend's own worker, composed from the ``robinauts`` package, with one engine
plugged in; what it refuses at its start, it says all at once and exits 2.

``python -m robinauts_claude_worker health`` is the container's health check: 0 while the
worker touched its activity file within ``HEALTHY_WITHIN`` seconds.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time

from robinauts.cli.composition import compose_worker, http_turn_broker, load
from robinauts.cli.lifecycle import HEALTHY_WITHIN
from robinauts.cli.worker_process import QuietRequests
from robinauts.shared_domain.domain import Config, StorageConfig, StorageKind
from robinauts_claude_worker import engine
from robinauts_claude_worker.credentials import credential_problems
from robinauts_claude_worker.settings import Settings, SettingsError, settings_from

_log = logging.getLogger(__name__)


def configuration_problems(settings: Settings, config: Config) -> list[str]:
    """What the deployment's file says against running this worker here."""
    problems = []
    if settings.worker not in config.workers():
        problems.append(f"no agent of the configuration names worker {settings.worker!r}")
    elif not config.is_external(settings.worker):
        problems.append(
            f"worker {settings.worker!r} is not external in the configuration: it needs a"
            f' [workers.{settings.worker}] table with kind = "external"'
        )
    mine = config.for_worker(settings.worker)
    for agent in mine.agents.values():
        if agent.engine != engine.ENGINE:
            problems.append(
                f"agent {agent.id!r} runs on engine {agent.engine!r}, and this worker has"
                f" {engine.ENGINE!r} alone"
            )
    return problems


def providers_of(settings: Settings, config: Config) -> list:
    """The providers this worker's agents' models are on, as the engines see them."""
    from robinauts.worker.engine_settings import engine_settings

    models = engine_settings(config, {}.get).models
    mine = config.for_worker(settings.worker)
    ids = sorted({config.models[a.model].provider for a in mine.agents.values()})
    return [models.providers[provider_id] for provider_id in ids]


async def run(settings: Settings, config: Config, secret_for) -> None:
    broker = http_turn_broker(settings.broker_url, settings.secret)
    worker = compose_worker(
        config,
        storage=StorageConfig(StorageKind.IN_PROCESS),
        secret_for=secret_for,
        broker=broker,
        engines={engine.ENGINE: engine.factory(settings)},
        name=settings.worker,
    )
    worker.activity_file = settings.activity_file
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stopping.set)
    await worker.start()
    _log.info(
        "worker %r runs on Claude Code in %s mode, pulling turns from %s",
        settings.worker,
        settings.auth.value,
        settings.broker_url,
    )
    try:
        await stopping.wait()
    finally:
        await worker.stop()


def health(settings: Settings) -> int:
    try:
        touched = settings.activity_file.stat().st_mtime
    except FileNotFoundError:
        return 1
    return 0 if time.time() - touched <= HEALTHY_WITHIN else 1


def main() -> int:
    environ = os.environ
    try:
        settings = settings_from(environ)
    except SettingsError as refused:
        print(refused, file=sys.stderr)
        return 2
    if sys.argv[1:] == ["health"]:
        return health(settings)
    if sys.argv[1:]:
        print(f"usage: {sys.argv[0]} [health]", file=sys.stderr)
        return 2
    logging.basicConfig(
        level=logging.INFO, format=f"%(levelname)s:worker {settings.worker}:%(name)s:%(message)s"
    )
    try:
        config, secret_for = load(settings.config, environ)
    except Exception as refused:
        print(refused, file=sys.stderr)
        return 2
    problems = configuration_problems(settings, config)
    if not problems:
        problems = credential_problems(
            settings.auth, environ, settings.broker_url, providers_of(settings, config)
        )
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 2
    logging.getLogger("httpx").addFilter(QuietRequests(settings.broker_url))
    try:
        asyncio.run(run(settings, config, secret_for))
    except Exception as failed:
        print(failed, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

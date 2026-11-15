# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""neorc, built: its manager composed into this backend on storage of the backend's kind, and,
with a flows folder, the folder's flows as forms, uploaded and served by a worker of neorc's.
``cli/main.py`` alone imports this module. It is apart from ``cli/composition.py`` because a
worker in a process of its own imports that one, and the extra workers' images leave neorc out:
nothing a worker imports may reach neorc (a contract in ``backend/pyproject.toml``).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from oneberth.launcher import (
    FRONT_URL,
    LAUNCHER_PID,
    WORKER_NAME,
    WORKER_URL,
    read_token,
    say_ready,
)

from robinauts.cli.composition import DATA_DIR_VARIABLE, DATABASE_URL_VARIABLE, load
from robinauts.cli.desktop import NEORC_WORKER
from robinauts.cli.worker_process import end_with_parent
from robinauts.controller.contract.domain import ConfigError
from robinauts.orchestrator.adapters.flows_folder import read_forms, read_settings
from robinauts.orchestrator.adapters.neorc import composed as neorc
from robinauts.orchestrator.application.flows import FlowForms
from robinauts.orchestrator.contract.domain import (
    FLOWS_VARIABLE,
    FormSpec,
    OrchestrationConfig,
    OrchestrationStartError,
    Orchestrator,
)
from robinauts.orchestrator.core.config import parse_flows_settings
from robinauts.orchestrator.core.forms import check_forms, parse_form
from robinauts.shared_domain.domain import Config


def orchestrator_of(
    environ: Mapping[str, str],
    config: Config,
    *,
    default_code: Path,
    public_url: str,
    api_url: str,
    worker_url: str,
    program: tuple[str, ...] = (),
    supervised: bool = False,
) -> Orchestrator:
    """neorc on this server's storage: the PostgreSQL ``ROBINAUTS_DATABASE_URL`` names, the
    file ``neorc.db`` in ``ROBINAUTS_DATA_DIR``, or this process. With a flows folder,
    ``ROBINAUTS_FLOWS_DIR``, as its ``flows.toml`` describes it, the folder's flows as forms,
    with the lists of values ``config``, the agents' configuration, and the folder's
    repositories give. ``public_url`` is the backend's, which its own requests to neorc name.
    neorc's worker starts with ``program``, ``robinauts neorc-worker``, and is told as the
    launcher tells a worker that ``api_url`` is the front listener's address and ``worker_url``
    the worker listener's. A ``supervised`` worker is started by this backend's supervisor, and
    this backend starts none. ``default_code`` is where the handlers are when ``flows.toml``
    names none. ``ConfigError`` names what stops it from starting.
    """
    database_url = environ.get(DATABASE_URL_VARIABLE) or None
    data_dir = environ.get(DATA_DIR_VARIABLE) or None
    storage: dict[str, Any] = {}
    if database_url is not None:
        storage["database_url"] = database_url
    elif data_dir is not None:
        storage["database_file"] = Path(data_dir) / neorc.FILE_NAME
    named = environ.get(FLOWS_VARIABLE)
    if not named:
        return neorc.ComposedNeorc(None, environ, public_url=public_url, **storage)
    folder = Path(named).absolute()
    problems: list[str] = []
    forms: dict[str, FormSpec] = {}
    declared: dict[str, dict[str, str]] = {}
    settings: dict[str, Any] = {}
    if folder.is_dir():
        settings = read_settings(folder)
        for flow, table in read_forms(folder).items():
            form, wrong = parse_form(flow, table)
            problems += wrong
            if form is not None:
                forms[flow] = form
        declared = neorc.declared_inputs(folder)
        problems += check_forms(forms, declared)
    described: OrchestrationConfig | None = None
    try:
        described = parse_flows_settings(
            settings, folder=folder, default_code=default_code, forms=forms
        )
    except ConfigError as refused:
        problems.insert(0, str(refused))
    else:
        command = neorc.neorc_command()
        problems = neorc.check_ready(described, command=command) + problems
    if problems or described is None:
        raise ConfigError("\n".join(problems))
    orchestration = described
    return neorc.ComposedNeorc(
        orchestration,
        environ,
        public_url=public_url,
        program=program,
        supervision={
            WORKER_NAME: NEORC_WORKER,
            WORKER_URL: worker_url,
            FRONT_URL: api_url,
            LAUNCHER_PID: str(os.getpid()),
        },
        supervised=supervised,
        make_flows=lambda engine: FlowForms(engine, orchestration, declared, config),
        **storage,
    )


def handlers_code(folder: Path, *, default_code: Path) -> Path:
    """Where the handlers of the flows folder ``folder`` are: the code location its
    ``flows.toml`` names, or ``default_code``. ``ConfigError`` for settings that are wrong."""
    settings = read_settings(folder) if folder.is_dir() else {}
    described = parse_flows_settings(settings, folder=folder, default_code=default_code)
    return described.code_location


def neorc_worker(environ: Mapping[str, str], *, default_code: Path) -> int:
    """``robinauts neorc-worker``: neorc's worker, as its supervisor starts it, the launcher
    or this backend. It reads its token, a token of ``neorc/worker`` on the handlers' queue,
    from its standard input, and the listeners' addresses from the launcher's variables. It
    asks Robinauts for the handlers' token with it, says it is ready, and becomes neorc's own
    worker, without the secrets of the configuration in its environment. It ends with its
    supervisor."""
    worker_url, front_url = environ.get(WORKER_URL), environ.get(FRONT_URL)
    named = environ.get(FLOWS_VARIABLE)
    if not worker_url or not front_url or not named:
        print(NOT_SUPERVISED, file=sys.stderr)
        return 2
    end_with_parent(environ)
    token = read_token(environ=environ)
    folder = Path(named).absolute()
    try:
        orchestration = parse_flows_settings(
            read_settings(folder), folder=folder, default_code=default_code
        )
        hidden: frozenset[str] = frozenset()
        config_file = environ.get(CONFIG_VARIABLE)
        if config_file:
            config, _ = load(Path(config_file), environ)
            hidden = neorc.hidden_variables(config)
        handlers = neorc.handler_token(worker_url, token)
    except (ConfigError, OrchestrationStartError) as refused:
        print(refused, file=sys.stderr)
        return 1
    say_ready()
    neorc.become_worker(
        orchestration,
        environ=environ,
        hidden=hidden | neorc.SERVER_SECRETS,
        worker_url=worker_url,
        front_url=front_url,
        token=token,
        handlers=handlers,
    )


CONFIG_VARIABLE = "ROBINAUTS_CONFIG"
NOT_SUPERVISED = (
    "neorc's worker is started by its supervisor, the launcher or Robinauts' backend, which"
    f" names the listeners in {WORKER_URL} and {FRONT_URL}, and the flows folder in"
    f" {FLOWS_VARIABLE}"
)

NEORC_ROLES = neorc.ROLES
"""neorc's roles, as its component gives them to oneberth: what the admin scripts need to
mint a token of neorc's."""

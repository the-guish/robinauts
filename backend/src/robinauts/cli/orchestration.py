# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""neorc, built: its manager composed into this backend on storage of the backend's kind, and,
with a flows folder, the folder's flows as forms, uploaded and served by a worker of neorc's.
``cli/main.py`` alone imports this module. It is apart from ``cli/composition.py`` because a
worker in a process of its own imports that one, and the extra workers' images leave neorc out:
nothing a worker imports may reach neorc (a contract in ``backend/pyproject.toml``).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from robinauts.cli.composition import DATA_DIR_VARIABLE, DATABASE_URL_VARIABLE
from robinauts.controller.contract.domain import ConfigError
from robinauts.orchestrator.adapters.flows_folder import read_forms, read_settings
from robinauts.orchestrator.adapters.neorc import composed as neorc
from robinauts.orchestrator.application.flows import FlowForms
from robinauts.orchestrator.contract.domain import (
    FLOWS_VARIABLE,
    FormSpec,
    OrchestrationConfig,
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
) -> Orchestrator:
    """neorc on this server's storage: the PostgreSQL ``ROBINAUTS_DATABASE_URL`` names, the
    file ``neorc.db`` in ``ROBINAUTS_DATA_DIR``, or this process. With a flows folder,
    ``ROBINAUTS_FLOWS_DIR``, as its ``flows.toml`` describes it, the folder's flows as forms,
    with the lists of values ``config``, the agents' configuration, and the folder's
    repositories give. ``public_url`` is the backend's, which its own requests to neorc name;
    ``api_url`` is where the handlers reach its front listener, and ``worker_url`` where
    neorc's worker reaches its worker listener. ``default_code`` is where the handlers
    are when ``flows.toml`` names none. ``ConfigError`` names what stops it from starting.
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
        return neorc.ComposedNeorc(
            None,
            environ,
            hidden=frozenset(),
            public_url=public_url,
            api_url=api_url,
            worker_url=worker_url,
            **storage,
        )
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
        hidden=neorc.hidden_variables(config),
        public_url=public_url,
        api_url=api_url,
        worker_url=worker_url,
        command=command,
        make_flows=lambda engine: FlowForms(engine, orchestration, declared, config),
        **storage,
    )


def neorc_roles() -> object:
    """neorc's roles, as its component gives them to oneberth: what the admin scripts need to
    mint a token of neorc's."""
    return neorc.neorc_roles()

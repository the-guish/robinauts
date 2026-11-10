# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The orchestrator, built: neorc embedded over the flows folder, and the folder's flows as
forms. ``cli/main.py`` alone imports this module. It is apart from ``cli/composition.py``
because a worker in a process of its own imports that one, and the extra workers' images leave
neorc out: nothing a worker imports may reach neorc (a contract in ``backend/pyproject.toml``).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from robinauts.cli.composition import DATABASE_URL_VARIABLE
from robinauts.controller.contract.domain import ConfigError
from robinauts.orchestrator.adapters.flows_folder import read_forms, read_settings
from robinauts.orchestrator.adapters.neorc import embedded as neorc
from robinauts.orchestrator.application.flows import FlowForms
from robinauts.orchestrator.contract.domain import FLOWS_VARIABLE, FormSpec, Orchestration
from robinauts.orchestrator.core.config import parse_flows_settings
from robinauts.orchestrator.core.forms import check_forms, parse_form
from robinauts.shared_domain.domain import Config


def orchestrator_of(
    environ: Mapping[str, str],
    config: Config,
    *,
    default_code: Path,
    api_url: str,
) -> Orchestration | None:
    """neorc over the flows folder ``ROBINAUTS_FLOWS_DIR`` names, as its ``flows.toml``
    describes it, on this server's storage, and where its worker reaches this server; and the
    folder's flows as forms, with the lists of values ``config``, the agents' configuration,
    and the folder's repositories give. ``None`` without the variable. ``default_code`` is
    where the handlers are when ``flows.toml`` names none. ``ConfigError`` names what stops it
    from starting."""
    named = environ.get(FLOWS_VARIABLE)
    if not named:
        return None
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
    try:
        described = parse_flows_settings(
            settings, folder=folder, default_code=default_code, forms=forms
        )
    except ConfigError as refused:
        problems.insert(0, str(refused))
    else:
        command = neorc.neorc_command()
        ui = neorc.built_ui()
        problems = neorc.check_ready(described, command=command, ui=ui) + problems
    if problems:
        raise ConfigError("\n".join(problems))
    embedded = neorc.EmbeddedNeorc(
        described,
        environ,
        hidden=neorc.hidden_variables(config),
        api_url=api_url,
        database_url=environ.get(DATABASE_URL_VARIABLE) or None,
        command=command,
        ui=ui,
    )
    return Orchestration(
        embedded=embedded, flows=FlowForms(embedded.engine, described, declared, config)
    )

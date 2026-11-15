# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The orchestrator's records, errors and the names a shell needs to serve it."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

FLOWS_VARIABLE = "ROBINAUTS_FLOWS_DIR"
"""The variable that names the flows folder and so turns the orchestrator on. The folder is the
flows' configuration, apart from the agents' (``ROBINAUTS_CONFIG``): the flow files, and their
settings in ``SETTINGS_FILE``."""
SETTINGS_FILE = "flows.toml"
"""The flows' settings, in the flows folder; optional."""
FORM_SUFFIX = ".form.toml"
"""A flow's form, in the flows folder beside it: ``<flow name>.form.toml``; optional."""

TABLE = "orchestration"
"""The table the agents' configuration held for the orchestrator before it had a folder of its
own: refused there now, with ``MOVED``."""
MOVED = (
    f"[{TABLE}]: no longer read from this file: the flows have a folder of their own, which"
    f" {FLOWS_VARIABLE} names, with their settings in its {SETTINGS_FILE}"
    " (docs/architecture/orchestration.md)"
)

MOUNT = "/neorc"
"""Where neorc's manager is served, composed into this backend: its component's address."""

HANDLER_QUEUE = "default"
"""The queue of the handlers Robinauts ships, which call its API on the front listener: neorc's
worker serves it, with a token of ``neorc/worker`` bound to it."""
HANDLER_QUEUE_PERMISSION = "neorc/tasks:claim"
"""neorc's permission to take a queue's tasks. A token that holds it on ``HANDLER_QUEUE`` runs
the handlers, and is given what they call Robinauts with: a token of ``robinauts/handler``."""
HANDLER_TOKEN_PATH = "/internal/queues/{queue}/handler-token"
"""Where neorc's worker asks for that token, under Robinauts' component, on either listener."""

TokenMinter = Callable[[str, str, str | None], Awaitable[str]]
"""What mints a service token of this backend's own, for the duration of its start: a name,
a role and the role's resource, if it needs one. It answers the token's secret."""


class LovKind(StrEnum):
    """A list of values an input of a form may take its value from."""

    REPO_BRANCH = "repo_branch"
    """A branch of a repository the flows' settings name: ``<id>@<branch>``, by the repository's
    id there, which is also the folder of its worktree in an extra worker's session."""
    MODEL = "model"
    """A model of the agents' configuration, by its id."""
    AGENT = "agent"
    """An agent of the agents' configuration, by its id; its label names its worker."""


@dataclass(frozen=True, slots=True)
class Repository:
    """A repository the flows may work on, and the branches they may start from."""

    id: str
    url: str
    branches: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InputSpec:
    """How a form shows one input of its flow."""

    label: str
    lov: LovKind | None = None


@dataclass(frozen=True, slots=True)
class FormSpec:
    """A flow's form, as its ``<flow name>.form.toml`` writes it."""

    title: str
    inputs: Mapping[str, InputSpec]


@dataclass(frozen=True, slots=True)
class Option:
    """One value of a list of values, and how a person reads it."""

    value: str
    label: str
    models: tuple[str, ...] | None = None
    """An agent's: the models its turns may run on, in order, its default first."""


@dataclass(frozen=True, slots=True)
class FormInput:
    """One input of a flow's form: its declared type, and the values it may take when it has
    a list of values."""

    name: str
    label: str
    type: str
    lov: LovKind | None = None
    options: tuple[Option, ...] | None = None


@dataclass(frozen=True, slots=True)
class FlowForm:
    """A flow, as a form to start it: every input it declares, in its form's order and then
    the flow's. ``has_form`` is false for a flow with no form file, whose inputs are then
    plain fields."""

    name: str
    title: str
    inputs: tuple[FormInput, ...]
    has_form: bool


class FlowRefused(Exception):
    """A flow the orchestrator would not start."""


class NoSuchFlowError(FlowRefused):
    """A flow the flows folder does not hold."""


class InvalidInputsError(FlowRefused):
    """Inputs that do not fit the flow's form or its declared inputs; the message names every
    problem."""


class Flows(ABC):
    """The flows a person may start, as forms, and the start of one."""

    @abstractmethod
    async def forms(self) -> tuple[FlowForm, ...]:
        """Every flow of the flows folder, by name."""

    @abstractmethod
    async def start(
        self,
        flow: str,
        inputs: Mapping[str, Any],
        *,
        credential: str | None,
    ) -> str:
        """Start a run of ``flow`` with ``inputs``, which are passed on as they are once they
        fit its form, and return the run's id. The start is sent to neorc with ``credential``,
        the person's own, so that neorc records the run as theirs. ``NoSuchFlowError``,
        ``InvalidInputsError``."""


class OrchestrationStartError(RuntimeError):
    """The orchestration engine's storage would not open, its flows were refused, or its worker
    could not be started."""


@dataclass(frozen=True, slots=True)
class OrchestrationConfig:
    """The flows folder and its settings, read: paths are absolute."""

    code_location: Path
    flows: Path
    repositories: tuple[Repository, ...] = ()
    forms: Mapping[str, FormSpec] = field(default_factory=dict)
    """By flow name, the forms of the folder."""


class Orchestrator(ABC):
    """neorc composed into this backend: its manager's component, composed beside Robinauts'
    own, its storage, opened before the component starts and closed after it stops, and what
    runs beside it once the backend serves: the flows' upload and neorc's worker."""

    @property
    @abstractmethod
    def component(self) -> object:
        """neorc's manager, as the component ``compose_app`` composes: named ``neorc``."""

    @abstractmethod
    async def open(self) -> None:
        """Open neorc's storage. ``OrchestrationStartError`` when it will not open."""

    @abstractmethod
    async def close(self) -> None:
        """Close it. Safe to repeat."""

    @abstractmethod
    async def start(self, mint: TokenMinter, transport: object) -> None:
        """Once the backend serves: upload the flows and start neorc's worker, each with a
        token ``mint`` makes. ``transport`` is the httpx transport that reaches this backend in
        its own process. A start that fails part way stops what it had started before it
        raises ``OrchestrationStartError``."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop what ``start`` started. Safe to repeat."""

    @abstractmethod
    async def starter_of(self, run: str) -> uuid.UUID | None:
        """The user id of the person neorc records as the starter of ``run``, wherever it was
        started: through Robinauts, or on neorc's own form. ``None`` for a run neorc does not
        hold, or one a service started. A sub-flow's run has its tree's starter."""

    @abstractmethod
    def flows(self, transport: object) -> Flows | None:
        """The flows folder's flows, started through ``transport``: none without a folder."""

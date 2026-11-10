# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The orchestrator's records, errors and the names a shell needs to serve it."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
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
"""Where the orchestration engine's application is mounted on this server."""
UI_URL = f"{MOUNT}/ui/"
"""Where its UI is, which the interface frames."""

DEV_ONLY = (
    f"{FLOWS_VARIABLE}: neorc is served with no authentication, which only the local"
    f" development mode allows for now: start with --dev-no-sign-in, or unset {FLOWS_VARIABLE}"
)

ASGIApp = Callable[
    [MutableMapping[str, Any], Callable[[], Awaitable[Any]], Callable[[Any], Awaitable[None]]],
    Awaitable[None],
]


class LovKind(StrEnum):
    """A list of values an input of a form may take its value from."""

    REPO_BRANCH = "repo_branch"
    """A branch of a repository the flows' settings name: ``<url>@<branch>``."""
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
    async def start(self, flow: str, inputs: Mapping[str, Any]) -> str:
        """Start a run of ``flow`` with ``inputs``, which are passed on as they are once they
        fit its form, and return the run's id. ``NoSuchFlowError``, ``InvalidInputsError``."""


class OrchestrationStartError(RuntimeError):
    """The orchestration engine's storage would not open, its flows were refused, or its worker
    could not be started."""


@dataclass(frozen=True, slots=True)
class OrchestrationConfig:
    """The flows folder and its settings, read: paths are absolute."""

    code_location: Path
    flows: Path
    ui: bool = True
    repositories: tuple[Repository, ...] = ()
    forms: Mapping[str, FormSpec] = field(default_factory=dict)
    """By flow name, the forms of the folder."""


class Orchestrator(ABC):
    """The orchestration engine embedded in this server: its application, to mount at
    ``MOUNT``, and what runs beside it, started and stopped around the server's own."""

    @property
    @abstractmethod
    def mounted(self) -> ASGIApp:
        """The application to mount at ``MOUNT``."""

    @abstractmethod
    async def start(self) -> None:
        """Start what runs beside the server. A start that fails part way stops what it had
        started before it raises ``OrchestrationStartError``."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop it. Safe to repeat."""


@dataclass(frozen=True, slots=True)
class Orchestration:
    """What the orchestrator gives a shell: the engine to mount, start and stop, and the flows
    to serve."""

    embedded: Orchestrator
    flows: Flows

# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""neorc's manager held to Robinauts' forms (``orchestrator/adapters/neorc/held.py``), on neorc's
real manager with its store in this process: what it asks the forms before a start, with whose
projects, and what it does with their refusal."""

from __future__ import annotations

import ast
import inspect
import textwrap
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from neorc_core import (
    InvalidValueError,
    Manager,
    ManagerUnavailableError,
    QueueKind,
    RunStarter,
    StarterKind,
)
from neorc_core.flows import read_flows
from neorc_core.local import MemoryStore, MemoryTaskNotifier

from robinauts.orchestrator.adapters.neorc.held import HeldManager, person_of
from robinauts.orchestrator.contract.domain import (
    BranchesUnreadableError,
    FlowForm,
    Flows,
    InvalidInputsError,
    Option,
    OptionPage,
    PersonsOptions,
)
from util.aio import asyncio_test

FLOW = """
name: ship
version: 0.1.0
inputs:
  branch: string
  project: string
output: tasks.pick
steps:
  pick:
    queue: default
    handler: tasks:pick
"""
INPUTS = {"branch": "main", "project": "p-1"}
ADA = uuid.UUID("11111111-1111-4111-8111-111111111111")


class Forms(Flows):
    """The forms, as the manager asks them: what each check was asked, and what it answers."""

    def __init__(self, refusal: Exception | None = None) -> None:
        self.refusal = refusal
        self.asked: list[tuple[str, Mapping[str, Any], PersonsOptions | None]] = []

    async def forms(self) -> tuple[FlowForm, ...]:
        return ()

    async def options(
        self, kind: str, *, after: str | None, query: str, limit: int, projects: PersonsOptions
    ) -> OptionPage:
        return OptionPage(items=(), more=False)

    async def check(
        self, flow: str, inputs: Mapping[str, Any], *, projects: PersonsOptions | None
    ) -> None:
        self.asked.append((flow, inputs, projects))
        if self.refusal is not None:
            raise self.refusal

    async def start(self, flow: str, inputs: Mapping[str, Any], *, credential: str | None) -> str:
        raise AssertionError("the manager starts the run itself")


async def projects_of(person: uuid.UUID) -> Sequence[Option]:
    return (Option(value=f"project of {person}", label="Default"),)


async def manager_with_the_flow(tmp_path: Path, *, held: bool) -> HeldManager:
    manager = HeldManager(
        MemoryStore(), tasks=MemoryTaskNotifier(), events=MemoryTaskNotifier(), held=held
    )
    (tmp_path / "ship.yaml").write_text(FLOW)
    await manager.add_queue("default", QueueKind.WORKER)
    await manager.upload_flows(read_flows(tmp_path))
    return manager


@asyncio_test
async def test_a_manager_that_is_not_held_starts_a_run_as_neorc_s_own_does(tmp_path: Path) -> None:
    manager = await manager_with_the_flow(tmp_path, held=False)

    run = await manager.start_run("ship", INPUTS)

    assert [r.id for r in await manager.list_runs()] == [run.id]


@asyncio_test
async def test_a_held_manager_starts_nothing_before_it_has_the_forms(tmp_path: Path) -> None:
    manager = await manager_with_the_flow(tmp_path, held=True)

    with pytest.raises(ManagerUnavailableError, match="forms are not read yet"):
        await manager.start_run("ship", INPUTS)

    assert await manager.list_runs() == []


@pytest.mark.parametrize(
    ("starter", "person"),
    [
        (RunStarter(StarterKind.USER, str(ADA), "Ada"), ADA),
        (RunStarter(StarterKind.SERVICE, str(ADA), "a token"), None),
        (RunStarter(StarterKind.USER, "not an id", "Ada"), None),
        (None, None),
    ],
)
@asyncio_test
async def test_the_form_is_asked_before_the_start_with_the_projects_of_the_person_who_starts(
    tmp_path: Path, starter: RunStarter | None, person: uuid.UUID | None
) -> None:
    manager = await manager_with_the_flow(tmp_path, held=True)
    forms = Forms()
    manager.hold_to(forms, projects_of)

    run = await manager.start_run("ship", INPUTS, started_by=starter)

    [(flow, inputs, projects)] = forms.asked
    assert (flow, inputs) == ("ship", INPUTS)
    assert person_of(starter) == person
    # A project is a person's: a service, or nobody, has none to be checked against.
    assert (projects is None) == (person is None)
    if projects is not None:
        assert [option.value for option in await projects()] == [f"project of {person}"]
    assert run.started_by == starter


@pytest.mark.parametrize(
    ("refusal", "raised", "said"),
    [
        # The inputs are refused, as neorc refuses an input of the wrong type: its route's 422.
        (
            InvalidInputsError("ship: input 'project': 'p-1' is not one of your projects"),
            InvalidValueError,
            r"^ship: input 'project': 'p-1' is not one of your projects$",
        ),
        # The inputs could not be checked, which refuses none of them: its route's 503.
        (
            BranchesUnreadableError("the branches of app could not be read: fatal: no"),
            ManagerUnavailableError,
            r"^ship: the branches of app could not be read: fatal: no$",
        ),
    ],
)
@asyncio_test
async def test_what_the_form_refuses_or_cannot_check_starts_no_run(
    tmp_path: Path, refusal: Exception, raised: type[Exception], said: str
) -> None:
    manager = await manager_with_the_flow(tmp_path, held=True)
    manager.hold_to(Forms(refusal), projects_of)

    with pytest.raises(raised, match=said):
        await manager.start_run(
            "ship", INPUTS, started_by=RunStarter(StarterKind.USER, str(ADA), "Ada")
        )

    assert await manager.list_runs() == []


def methods_that_call(name: str) -> set[str]:
    """The methods of neorc's manager whose body calls an attribute of that name."""
    [manager] = ast.parse(textwrap.dedent(inspect.getsource(Manager))).body
    assert isinstance(manager, ast.ClassDef)
    return {
        method.name
        for method in manager.body
        if isinstance(method, ast.AsyncFunctionDef | ast.FunctionDef)
        and any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == name
            for node in ast.walk(method)
        )
    }


def test_neorc_s_manager_starts_a_run_where_the_form_holds_it_and_for_a_sub_flow_alone() -> None:
    """The hold is on ``start_run``. neorc's manager writes a new run in one private method,
    which ``start_run`` and ``start_sub_run`` alone reach; the sub-flow's start is left out on
    purpose (``held.py``). A neorc that starts a run any other way, a re-run or a schedule,
    would start it past the form: this fails then, and the new start is held or left out by a
    decision, not by an update of the pin."""
    assert methods_that_call("start_run") == {"_start_latest"}  # the store's, which writes it
    assert methods_that_call("_start_latest") == {"start_run", "start_sub_run"}

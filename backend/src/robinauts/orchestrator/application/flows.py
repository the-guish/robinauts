# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The flows a person may start, as forms; the check that holds a start to its flow's form; and
the start of one through Robinauts, handed to the engine as it came, with the person's own
credential. The engine records the run as that person's, and a flow's handler acts for them
(``docs/architecture/orchestration.md``, "Whose a run is"). The engine asks the check before
every start, whichever route the start reached it by. A list of values too long or too changing
to list, a repository's branches or the person's projects, is searched as the person types,
and checked anew at the start.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from robinauts.orchestrator.contract.domain import (
    LIVE_KINDS,
    FlowForm,
    Flows,
    InvalidInputsError,
    LovKind,
    NoSuchFlowError,
    NoSuchListError,
    Option,
    OptionPage,
    OrchestrationConfig,
    PersonsOptions,
    Repository,
)
from robinauts.orchestrator.core.forms import DeclaredInputs, build_forms, check_inputs
from robinauts.orchestrator.core.search import is_branch_name, matches, offered, search
from robinauts.orchestrator.ports.branches import BranchNames
from robinauts.orchestrator.ports.engine import FlowEngine
from robinauts.shared_domain.domain import Config


class FlowForms(Flows):
    """The flows of one flows folder. ``declared`` is what its flow files declare, read at the
    start; ``config`` the agents' configuration, which the lists of models and agents come
    from. Both are fixed for the server's life, so the forms are built once. ``branches`` reads
    the branches of the folder's repositories, which change while it runs."""

    def __init__(
        self,
        engine: FlowEngine,
        orchestration: OrchestrationConfig,
        declared: DeclaredInputs,
        config: Config,
        *,
        branches: BranchNames,
    ) -> None:
        self._engine = engine
        self._branches = branches
        self._repositories = {r.id: r for r in orchestration.repositories}
        self._forms = build_forms(
            declared,
            orchestration.forms,
            repositories=orchestration.repositories,
            config=config,
        )
        self._by_name = {form.name: form for form in self._forms}

    async def forms(self) -> tuple[FlowForm, ...]:
        return self._forms

    async def options(
        self,
        kind: str,
        *,
        after: str | None,
        query: str,
        limit: int,
        projects: PersonsOptions,
    ) -> OptionPage:
        if kind == LovKind.PROJECT:
            return search(await projects(), query, limit)
        if kind == LovKind.BRANCH:
            repository = self._repository(after)
            names = offered(await self._branches.listed(repository), repository.branches)
            return search([Option(value=n, label=n) for n in names], query, limit)
        searched = ", ".join(sorted(LIVE_KINDS))
        raise NoSuchListError(f"no list of values {kind!r} is searched: only {searched}")

    def _repository(self, id_: str | None) -> Repository:
        repository = None if id_ is None else self._repositories.get(id_)
        if repository is None:
            named = ", ".join(self._repositories) or "none"
            raise NoSuchListError(
                f"a branch is of a repository of the flows' settings ({named}), not {id_!r}"
            )
        return repository

    async def check(
        self,
        flow: str,
        inputs: Mapping[str, Any],
        *,
        projects: PersonsOptions | None,
    ) -> None:
        form = self._by_name.get(flow)
        if form is None:
            return
        problems = check_inputs(form, inputs)
        if not problems:
            problems = await self._live_problems(form, inputs, projects)
        if problems:
            raise InvalidInputsError(f"{flow}: " + "; ".join(problems))

    async def start(
        self,
        flow: str,
        inputs: Mapping[str, Any],
        *,
        credential: str | None,
    ) -> str:
        if flow not in self._by_name:
            raise NoSuchFlowError(f"no flow {flow!r}")
        # The manager holds the start to the form, as it holds every other start.
        return await self._engine.start_run(flow, inputs, credential=credential)

    async def _live_problems(
        self, form: FlowForm, inputs: Mapping[str, Any], projects: PersonsOptions | None
    ) -> list[str]:
        """What keeps the values of the lists that are searched from starting the run, checked
        as each list is now: the inputs fit the form otherwise."""
        problems = []
        for field in form.inputs:
            if not field.live:
                continue
            value = inputs[field.name]
            here = f"input {field.name!r}: {value!r}"
            if not isinstance(value, str):
                problems.append(f"{here} is not one of its {field.lov} values")
            elif field.lov is LovKind.PROJECT:
                if projects is None:
                    problems.append(f"{here} is a person's project, and no person starts this run")
                elif value not in [p.value for p in await projects()]:
                    problems.append(f"{here} is not one of your projects")
            else:
                # The form's check put a repository of the settings in the input it is of.
                repository = self._repositories[inputs[field.of or ""]]
                if not (
                    is_branch_name(value)
                    and matches(value, repository.branches)
                    and await self._branches.exists(repository, value)
                ):
                    problems.append(
                        f"{here} is not a branch of {repository.id} that its settings offer"
                    )
        return problems

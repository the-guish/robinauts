# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""neorc's manager, held to Robinauts' forms: before it starts a run, the flow's form checks
the inputs (``Flows.check``). neorc's own route, its Python client and its page all start a run
with ``Manager.start_run``, so a start is checked whichever way it reached the manager, and
Robinauts keeps no route of its own that a start must take to be checked."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from functools import partial
from typing import Any

from neorc_core import (
    InvalidValueError,
    Manager,
    ManagerUnavailableError,
    Run,
    RunStarter,
    StarterKind,
    Store,
    TaskNotifier,
)

from robinauts.orchestrator.contract.domain import (
    BranchesUnreadableError,
    Flows,
    InvalidInputsError,
    ProjectsOf,
)

NOT_HELD_YET = "the flows' forms are not read yet: no run starts before they are"


def person_of(starter: RunStarter | None) -> uuid.UUID | None:
    """The user id of the person who starts a run, as neorc names them; ``None`` for a
    service, or for a start nobody is named for."""
    if starter is None or starter.kind is not StarterKind.USER:
        return None
    try:
        return uuid.UUID(starter.id)
    except ValueError:
        return None


class HeldManager(Manager):
    """neorc's manager, which asks Robinauts' form of a flow before it starts a run of it.

    ``held`` says the server has a flows folder, whose forms ``hold_to`` hands over once they
    are read. Until then a held manager starts nothing: a start that came too early is refused,
    not let through unchecked. A manager that is not held, a server with no flows folder,
    starts runs as neorc's own does.

    A flow that calls another as a sub-flow does not come this way (``start_sub_run``): its
    inputs are what the calling flow worked out, checked by neorc against the declared types,
    and no person chose them on a form.
    """

    def __init__(
        self, store: Store, *, tasks: TaskNotifier, events: TaskNotifier, held: bool = False
    ) -> None:
        super().__init__(store, tasks=tasks, events=events)
        self._held = held
        self._flows: Flows | None = None
        self._projects_of: ProjectsOf | None = None

    def hold_to(self, flows: Flows, projects_of: ProjectsOf) -> None:
        """From now on, every start is held to ``flows``' form of its flow, a project among
        its inputs to the starter's own, read with ``projects_of``."""
        self._flows = flows
        self._projects_of = projects_of

    async def start_run(
        self,
        flow: str,
        inputs: Mapping[str, Any],
        *,
        started_by: RunStarter | None = None,
    ) -> Run:
        """As neorc's, once the flow's form takes the inputs. ``InvalidValueError`` with the
        form's every problem, or with why a repository's branches could not be read: neorc's
        route answers both as it answers an input of the wrong type."""
        if self._held:
            await self._hold(flow, inputs, person_of(started_by))
        return await super().start_run(flow, inputs, started_by=started_by)

    async def _hold(self, flow: str, inputs: Mapping[str, Any], person: uuid.UUID | None) -> None:
        if self._flows is None or self._projects_of is None:
            raise ManagerUnavailableError(NOT_HELD_YET)
        projects = None if person is None else partial(self._projects_of, person)
        try:
            await self._flows.check(flow, inputs, projects=projects)
        except InvalidInputsError as refused:
            raise InvalidValueError(str(refused)) from None
        except BranchesUnreadableError as refused:
            # The start is refused, not let through: the branch could not be checked.
            raise InvalidValueError(f"{flow}: {refused}") from None

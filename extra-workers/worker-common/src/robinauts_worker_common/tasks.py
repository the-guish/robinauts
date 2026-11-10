# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The task payloads the extra workers know (docs/specs/wire.md, "Task payloads"): structured
data a question carries beside its text, for the engine rather than the model. An engine acts
on the ``task_key`` values it knows and runs every other turn on the text alone.

``code_task/1`` names the repositories a task works on, each with the branch it starts from:

    {"task_key": "code_task/1", "repositories": {"<id>": {"branch": "<branch>"}}}

Each id is one of ``[repositories]`` in the flows' settings, and each branch one of that
repository's ``branches`` there: a payload asks for nothing the operator did not list.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from robinauts_worker_common.worktrees import Repository

TASK_KEY = "task_key"
CODE_TASK = "code_task/1"
"""A task on repositories: the worktrees of the ones it names, each from its branch."""


class TaskPayloadError(ValueError):
    """A payload of a task this worker knows, which does not hold what that task needs."""


def code_task_starts(
    payload: Mapping[str, Any] | None, repositories: Sequence[Repository]
) -> dict[str, str] | None:
    """By repository id, the branch each worktree of a ``code_task/1`` starts from; ``None``
    for no payload, or one of another task. ``TaskPayloadError`` names every problem."""
    if payload is None or payload.get(TASK_KEY) != CODE_TASK:
        return None
    named = payload.get("repositories")
    if not isinstance(named, Mapping) or not named:
        raise TaskPayloadError(
            f"{CODE_TASK}: repositories names the ones it works on, by id, each with its branch"
        )
    known = {repository.id: repository for repository in repositories}
    starts: dict[str, str] = {}
    problems = []
    for id_, entry in named.items():
        repository = known.get(id_)
        if repository is None:
            problems.append(f"no repository {id_!r} in this worker's flows settings")
            continue
        branch = entry.get("branch") if isinstance(entry, Mapping) else None
        if not isinstance(branch, str) or branch not in repository.branches:
            listed = ", ".join(repository.branches) or "none"
            problems.append(f"{id_}: branch {branch!r} is not one it lists ({listed})")
            continue
        starts[id_] = branch
    if problems:
        raise TaskPayloadError(f"{CODE_TASK}: " + "; ".join(problems))
    return starts

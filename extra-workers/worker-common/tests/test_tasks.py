# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The task payloads the extra workers know: ``code_task/1``, read against the repositories of
the flows' settings."""

from __future__ import annotations

import re

import pytest

from robinauts_worker_common.tasks import TaskPayloadError, code_task_starts
from robinauts_worker_common.worktrees import Repository

REPOSITORIES = (
    Repository("app", "https://github.com/o/app.git", ("main", "develop")),
    Repository("docs", "https://github.com/o/docs.git", ("main",)),
)


@pytest.mark.parametrize(
    ("payload", "starts"),
    [
        (None, None),
        ({"task_key": "review/1", "repositories": {}}, None),
        ({"task_key": "code_task/2", "repositories": {"app": {}}}, None),
        (
            {"task_key": "code_task/1", "repositories": {"app": {"branch": "develop"}}},
            {"app": "develop"},
        ),
        (
            {
                "task_key": "code_task/1",
                "repositories": {"app": {"branch": "main"}, "docs": {"branch": "main"}},
                "anything": "else",
            },
            {"app": "main", "docs": "main"},
        ),
    ],
)
def test_a_code_task_names_its_repositories_and_branches_and_any_other_names_none(
    payload: dict[str, object] | None, starts: dict[str, str] | None
) -> None:
    assert code_task_starts(payload, REPOSITORIES) == starts


@pytest.mark.parametrize(
    ("repositories", "said"),
    [
        (None, "repositories names the ones it works on"),
        ({}, "repositories names the ones it works on"),
        (["app"], "repositories names the ones it works on"),
        ({"web": {"branch": "main"}}, "no repository 'web' in this worker's flows settings"),
        (
            {"app": {"branch": "feature"}},
            "app: branch 'feature' is not one it lists (main, develop)",
        ),
        ({"app": {}}, "app: branch None is not one it lists"),
        ({"app": "main"}, "app: branch None is not one it lists"),
    ],
)
def test_a_code_task_that_asks_for_what_is_not_listed_is_refused(
    repositories: object, said: str
) -> None:
    payload = {"task_key": "code_task/1", "repositories": repositories}
    with pytest.raises(TaskPayloadError, match=re.escape(said)):
        code_task_starts(payload, REPOSITORIES)


def test_every_problem_is_named_at_once() -> None:
    payload = {
        "task_key": "code_task/1",
        "repositories": {"web": {"branch": "main"}, "app": {"branch": "x"}},
    }
    with pytest.raises(TaskPayloadError) as refused:
        code_task_starts(payload, REPOSITORIES)
    assert str(refused.value).count(";") == 1

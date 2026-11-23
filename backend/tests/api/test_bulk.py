# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Many conversations at once through the API: a selection is ids or a filter, never both.

The interface's search page sends both shapes; what it cannot send is what is refused here.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from util.controller_db import requires_postgres

pytestmark = requires_postgres

BULK = "/robinauts/api/conversations/bulk"


def started(api: httpx.Client, text: str, project: str) -> str:
    answer = api.post(
        "/robinauts/api/turns",
        json={"agent_id": "pydantic_ai", "text": text, "project_id": project},
    )
    assert answer.status_code == 200, answer.text
    return answer.headers["x-robinauts-conversation-id"]


def listed(api: httpx.Client, project: str, *, archived: bool = False) -> list[str]:
    answer = api.get(
        "/robinauts/api/conversations",
        params={"project_id": project, "archived": str(archived).lower()},
    )
    assert answer.status_code == 200, answer.text
    return [c["id"] for c in answer.json()["items"]]


def test_a_filter_less_its_exceptions_is_archived_then_deleted(api: httpx.Client) -> None:
    # A project of this test's own keeps the filter off every other test's conversations.
    project = api.post("/robinauts/api/projects", json={"name": f"bulk {uuid.uuid4()}"}).json()
    pid = project["id"]
    one, two, three = (started(api, text, pid) for text in ("one", "two", "three"))
    assert listed(api, pid) == [three, two, one]

    matching = {"project_id": pid, "archived": False}
    archived = api.post(BULK, json={"action": "archive", "matching": matching, "except_ids": [one]})
    assert archived.status_code == 200, archived.text
    assert archived.json() == {"count": 2}
    assert listed(api, pid) == [one]
    items = api.get(
        "/robinauts/api/conversations", params={"project_id": pid, "archived": "true"}
    ).json()["items"]
    assert {c["id"]: c["archived"] for c in items} == {one: False, two: True, three: True}

    deleted = api.post(BULK, json={"action": "delete", "ids": [two, str(uuid.uuid4())]})
    assert deleted.json() == {"count": 1}
    assert listed(api, pid, archived=True) == [three, one]
    assert api.get(f"/robinauts/api/conversations/{two}").status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"action": "archive"},
        {"action": "archive", "ids": [], "matching": {}},
        {"action": "archive", "ids": [], "except_ids": [str(uuid.uuid4())]},
        {"action": "purge", "ids": []},
        {"action": "delete", "ids": [str(uuid.uuid4())] * 1001},
        {"action": "delete", "matching": {"q": "x" * 201}},
    ],
)
def test_a_selection_that_is_not_one_shape_is_refused(
    api: httpx.Client, body: dict[str, object]
) -> None:
    assert api.post(BULK, json=body).status_code == 422

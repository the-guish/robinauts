# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A form whose lists are searched as the person types, inside neorc
(``docs/architecture/orchestration.md``, "Lists that are searched" and "Where a start is
checked").

A flows folder holds one flow and its form file: a repository of the folder's settings, a
branch of it, and a project of the person's. The branch and the project are not listed: neorc
asks Robinauts for those that match what the person types. The repository has a thousand
branches, and its settings offer some of them.

neorc's page starts the run at neorc's own route. The flow's form holds the start there: what
the person writes as JSON, in the place of the fields, is refused in the form's words when the
form would not have offered it.

neorc is the real one, composed into the backend, on the test's PostgreSQL in its schema
``neorc``, which the test drops when it ends. The repository is on disk, read with the real
``git``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from util import stack
from util.browser import choose_app, sign_in
from util.fake_openai import FakeLocalGPTServer
from util.repositories import bare_repository

pytestmark = [pytest.mark.io, pytest.mark.database]

FLOW = """
name: ship
version: 0.1.0
inputs:
  repo: string
  branch: string
  project: string
output: tasks.pick
steps:
  pick:
    queue: default
    handler: robinauts_tasks:pick_number
"""
FORM = """
title = "Ship"
[inputs.repo]
label = "Repository"
lov = "repository"
[inputs.branch]
label = "Branch"
lov = "branch"
of = "repo"
[inputs.project]
label = "Project"
lov = "project"
"""
STARTS = re.compile(r"/neorc/flows/ship/runs$")
"""neorc's own route, which its page starts a run at."""


@pytest.fixture
def server(local_gpt: FakeLocalGPTServer, tmp_path: Path) -> Iterator[str]:
    """The stack's server, with a flows folder whose one repository is on disk."""
    app = bare_repository(
        tmp_path / "app.git", "main", "other", *(f"feature/{n:04}" for n in range(1000))
    )
    flows = tmp_path / "flows"
    flows.mkdir()
    (flows / "ship.yaml").write_text(FLOW)
    (flows / "ship.form.toml").write_text(FORM)
    (flows / "flows.toml").write_text(
        f'[repositories.app]\nurl = "file://{app}"\nbranches = ["main", "feature/*"]\n'
    )
    config = tmp_path / "robinauts.toml"
    config.write_text(stack.config_for(local_gpt.base_url))
    environment = {**stack.API_KEY, "ROBINAUTS_FLOWS_DIR": str(flows)}
    try:
        with stack.database(config) as url, stack.server(config, url, env=environment) as running:
            yield running
    finally:
        given = os.environ["ROBINAUTS_TEST_DATABASE_URL"]
        asyncio.run(stack.run_statement(given, 'DROP SCHEMA IF EXISTS "neorc" CASCADE'))


def test_a_branch_and_a_project_are_searched_in_the_form_and_the_form_holds_neorc_s_own_start(
    server: stack.Server, page: Page
) -> None:
    sign_in(page, server)

    # 1. neorc, on the rail: the flow's page, with Robinauts' form to start a run.
    choose_app(page, "neorc")
    page.get_by_role("navigation", name="Main").get_by_role("link", name="Flows").click()
    page.get_by_role("main").get_by_role("link", name="ship").first.click()
    form = page.get_by_role("form", name="Start ship")
    expect(form.get_by_role("heading", name="Ship")).to_be_visible()

    # 2. A branch is of a repository: its search waits for one.
    branch = form.get_by_role("combobox", name="Branch")
    expect(branch).to_be_disabled()
    form.get_by_role("group", name="Repository").get_by_role("radio", name="app").check()
    expect(branch).to_be_enabled()

    # 3. Opened, it offers the branch the settings name first; typed in, those that match,
    #    among a thousand. A branch the settings do not offer is not found.
    branch.click()
    expect(form.get_by_role("option").first).to_have_text("main")
    branch.fill("other")
    expect(form.get_by_text("Nothing matches.")).to_be_visible()
    branch.fill("feature/099")
    expect(form.get_by_role("option")).to_have_count(10)
    form.get_by_role("option", name="feature/0997").click()
    expect(branch).to_have_value("feature/0997")

    # 4. A project of the person's own, found by its name.
    project = form.get_by_role("combobox", name="Project")
    project.click()
    project.fill("def")
    form.get_by_role("option", name="Default").click()
    expect(project).to_have_value("Default")

    # 5. The run starts at neorc's own route, with what the form holds, and neorc goes to it.
    with page.expect_response(STARTS) as started:
        form.get_by_role("button", name="Start run").click()
    assert started.value.status == 201, started.value.text()
    sent = json.loads(started.value.request.post_data or "{}")["inputs"]
    assert (sent["repo"], sent["branch"]) == ("app", "feature/0997")
    expect(page).to_have_url(re.compile(r"#/neorc/runs/[0-9a-f-]{36}$"))

    # 6. Written as JSON in the place of the fields, a branch the form would not have offered
    #    starts nothing: neorc's route refuses it in the form's words, and the page stays.
    page.go_back()
    form = page.get_by_role("form", name="Start ship")
    form.get_by_role("button", name="Edit as JSON").click()
    form.get_by_label("Inputs as JSON").fill(json.dumps({**sent, "branch": "other"}))
    with page.expect_response(STARTS) as refused:
        form.get_by_role("button", name="Start run").click()
    assert refused.value.status == 422, refused.value.text()
    expect(form.get_by_role("alert")).to_contain_text(
        "'other' is not a branch of app that its settings offer"
    )
    expect(page).to_have_url(re.compile(r"#/neorc/flows/ship$"))

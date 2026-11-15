# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Starting a flow from Robinauts' form, inside neorc (``docs/architecture/orchestration.md``,
"Flows as forms"; oneberth's ``neorc-in-robinauts.md``, "The form that starts a run").

A flows folder holds one flow and its form file: a repository's branch, a model and an agent
from lists of values, and a task in words. neorc, chosen on the rail, draws Robinauts' form in
the place of its own, with the folder's branches, the configuration's agents and its models.
Filled in, it starts a run, and neorc goes to that run. The flow's handler opens a
conversation for the person who started the run, which Robinauts then lists.

neorc is the real one, composed into the backend, on the test's PostgreSQL in its schema
``neorc``, which the test drops when it ends.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from util import stack
from util.browser import choose_app, history, sign_in
from util.fake_openai import FakeLocalGPTServer

pytestmark = [pytest.mark.io, pytest.mark.database]

FLOW = """
name: code_task
version: 0.1.0
inputs:
  task: string
  repo_branch: string
  model: string
  agent: string
output: tasks.implement
steps:
  implement:
    queue: default
    handler: robinauts_tasks:implement
    params:
      task: inputs.task
      repo_branch: inputs.repo_branch
      model: inputs.model
      agent: inputs.agent
      run: neorc.flow_run_id
"""
FORM = """
title = "Code a task"
[inputs.repo_branch]
label = "Repository"
lov = "repo_branch"
[inputs.agent]
label = "Agent"
lov = "agent"
[inputs.model]
label = "Model"
lov = "model"
[inputs.task]
label = "Task"
"""
SETTINGS = """
[repositories.app]
url = "https://git.example/app"
branches = ["main", "next"]
"""


@pytest.fixture
def server(local_gpt: FakeLocalGPTServer, tmp_path: Path) -> Iterator[str]:
    """The stack's server, with a flows folder."""
    flows = tmp_path / "flows"
    flows.mkdir()
    (flows / "code_task.yaml").write_text(FLOW)
    (flows / "code_task.form.toml").write_text(FORM)
    (flows / "flows.toml").write_text(SETTINGS)
    config = tmp_path / "robinauts.toml"
    config.write_text(stack.config_for(local_gpt.base_url))
    environment = {**stack.API_KEY, "ROBINAUTS_FLOWS_DIR": str(flows)}
    try:
        with stack.database(config) as url, stack.server(config, url, env=environment) as running:
            yield running
    finally:
        given = os.environ["ROBINAUTS_TEST_DATABASE_URL"]
        asyncio.run(stack.run_statement(given, 'DROP SCHEMA IF EXISTS "neorc" CASCADE'))


def test_a_run_started_from_robinauts_form_in_neorc_opens_a_conversation_of_its_starter(
    server: stack.Server, page: Page
) -> None:
    sign_in(page, server)
    # Robinauts' own history starts empty.
    expect(page.get_by_text("No conversations yet.").first).to_be_visible()

    # 1. neorc, on the rail: its flows, and the flow's page with its form to start a run.
    choose_app(page, "neorc")
    page.get_by_role("navigation", name="Main").get_by_role("link", name="Flows").click()
    page.get_by_role("main").get_by_role("link", name="code_task").first.click()
    expect(page).to_have_url(re.compile(r"#/neorc/flows/code_task"))

    # 2. The form is Robinauts': its title, and its lists filled from the folder and the
    #    configuration.
    form = page.get_by_role("form", name="Start code_task")
    expect(form.get_by_role("heading", name="Code a task")).to_be_visible()
    repository = form.get_by_role("group", name="Repository")
    expect(repository.get_by_role("radio")).to_have_count(2)
    repository.get_by_role("radio", name="app · next").check()
    form.get_by_role("group", name="Agent").get_by_role(
        "radio", name=re.compile("^LangChain")
    ).check()
    form.get_by_role("group", name="Model").get_by_role(
        "radio", name="Local GPT", exact=True
    ).check()

    # 3. Started with the task left empty: the form says so, and nothing is sent.
    form.get_by_role("button", name="Start run").click()
    expect(form.get_by_role("alert")).to_contain_text("Task needs a value.")
    form.get_by_label("Task").fill("Fix the bug")
    form.get_by_role("button", name="Start run").click()

    # 4. neorc goes to the run it started.
    expect(page).to_have_url(re.compile(r"#/neorc/runs/[0-9a-f-]{36}$"))
    run_url = page.url

    # 5. Back in Robinauts: the conversation the flow's handler opened is the starter's, listed
    #    in their history, with the agent the form chose.
    choose_app(page, "Robinauts")
    opened = history(page).filter(has_text=re.compile("^Work on the repository app"))
    expect(opened).to_have_count(1, timeout=30_000)
    opened.click()
    expect(page.get_by_text("with LangChain")).to_be_visible()

    # 6. neorc kept where it was: its run.
    choose_app(page, "neorc")
    expect(page).to_have_url(run_url)

# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Starting a flow from its form (``docs/architecture/orchestration.md``, "Flows as forms").

A flows folder holds one flow and its form: a repository's branch, a model and an agent from
lists of values, and a task in words. "New run" in the panel lists the flow; its form offers
the folder's branches, the configuration's agents and the chosen agent's models; filled in, it
starts a run, and the interface goes to that run, in the orchestrator's frame.

neorc is the real one, embedded, on the test's PostgreSQL in its schema ``neorc``, which the
test drops when it ends.
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
output: tasks.pick
steps:
  pick:
    queue: default
    handler: robinauts_tasks:pick_number
"""
FORM = """
title = "Code a task"
[inputs.repo_branch]
label = "Repository"
lov = "repo_branch"
[inputs.model]
label = "Model"
lov = "model"
[inputs.agent]
label = "Agent"
lov = "agent"
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


def test_a_flow_is_started_from_its_form_and_the_interface_goes_to_its_run(
    server: str, page: Page
) -> None:
    page.goto(f"{server}/ui/")
    # The panel opens on the sessions; the orchestrator's navigation is its other tab.
    page.get_by_role("tab", name="Flows").click()
    flows = page.get_by_role("navigation", name="Flows")
    flows.get_by_role("link", name="New run").click()
    page.get_by_role("list", name="Flows to start").get_by_role("link").click()
    expect(page).to_have_url(re.compile(r"#/flows/new/code_task$"))

    form = page.get_by_role("form", name="Start Code a task")
    repository = form.get_by_label("Repository")
    expect(repository.get_by_role("option")).to_have_text(["Choose…", "app · main", "app · next"])
    repository.select_option(label="app · next")
    # The models are the chosen agent's, its default picked; one it also runs on stays.
    model, agent = form.get_by_label("Model"), form.get_by_label("Agent")
    agent.select_option("pinned")
    expect(model.get_by_role("option")).to_have_text(["Choose…", "Local GPT 2"])
    expect(model).to_have_value("local_gpt_2")
    agent.select_option("langchain")
    expect(model.get_by_role("option")).to_have_text(["Choose…", "Local GPT", "Local GPT 2"])
    expect(model).to_have_value("local_gpt_2")
    # Started with a field left empty: the form says so, and nothing is sent.
    form.get_by_role("button", name="Start run").click()
    expect(form.get_by_role("alert")).to_have_text("Task needs a value.")
    form.get_by_label("Task").fill("Fix the bug")
    form.get_by_role("button", name="Start run").click()

    expect(page).to_have_url(re.compile(r"#/flows/runs/[0-9a-f-]{36}$"))
    # The panel's Flows tab carries the same title; the page is the frame.
    frame = page.locator('iframe[title="Flows"]')
    expect(frame).to_have_attribute("src", re.compile(r"/neorc/ui/\?embedded#/runs/"))

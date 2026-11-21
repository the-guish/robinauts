# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Starting a flow from Robinauts' form, inside neorc (``docs/architecture/orchestration.md``,
"Flows as forms"; oneberth's ``neorc-in-robinauts.md``, "The form that starts a run").

A flows folder holds one flow and its form file: a repository's branch, a model and an agent
from lists of values, and a task in words. neorc, chosen on the rail, draws Robinauts' form in
the place of its own, with the folder's branches, the configuration's agents and its models.
Filled in, it starts a run, and neorc goes to that run. The flow's handler opens a
conversation for the person who started the run, which Robinauts then lists.

A second flow of the folder takes the name of a flow, from the list of neorc's flows. The form
holds no value for it: its box searches the flows at neorc as the person types, and starts a
run with the flow picked.

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
RELAY = """
name: relay
version: 0.1.0
inputs:
  target: string
output: tasks.pick
steps:
  pick:
    queue: default
    handler: robinauts_tasks:pick_number
"""
RELAY_FORM = """
title = "Relay"
[inputs.target]
label = "Flow"
lov = "flow"
"""


@pytest.fixture
def server(local_gpt: FakeLocalGPTServer, tmp_path: Path) -> Iterator[str]:
    """The stack's server, with a flows folder of two flows."""
    flows = tmp_path / "flows"
    flows.mkdir()
    (flows / "code_task.yaml").write_text(FLOW)
    (flows / "code_task.form.toml").write_text(FORM)
    (flows / "relay.yaml").write_text(RELAY)
    (flows / "relay.form.toml").write_text(RELAY_FORM)
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


def test_a_form_s_list_of_flows_is_searched_at_neorc_as_the_person_types(
    server: stack.Server, page: Page
) -> None:
    sign_in(page, server)

    # 1. The flow's page in neorc, with Robinauts' form: one box for the flow to name.
    choose_app(page, "neorc")
    page.get_by_role("navigation", name="Main").get_by_role("link", name="Flows").click()
    page.get_by_role("main").get_by_role("link", name="relay").first.click()
    expect(page).to_have_url(re.compile(r"#/neorc/flows/relay"))
    form = page.get_by_role("form", name="Start relay")
    expect(form.get_by_role("heading", name="Relay")).to_be_visible()
    box = form.get_by_role("combobox", name="Flow")
    found = form.get_by_role("listbox", name="Flows for Flow").get_by_role("option")

    # 2. With the focus, before a letter: the flows neorc holds, which are the folder's.
    box.click()
    expect(found).to_have_text(["code_task 0.1.0", "relay 0.1.0"])

    # 3. As the person types, in any case: the box asks neorc, and lists what it found.
    with page.expect_request(lambda asked: "/neorc/flows?search=CODE" in asked.url):
        box.press_sequentially("CODE")
    expect(found).to_have_text(["code_task 0.1.0"])

    # 4. A text that names no flow is no choice: the form says so, and nothing is sent.
    box.fill("nowhere")
    expect(form.get_by_text("No flow matches.")).to_be_visible()
    form.get_by_role("button", name="Start run").click()
    expect(form.get_by_role("alert")).to_contain_text("Choose a flow for Flow.")

    # 5. Enter picks the flow found, and the run starts with its name.
    box.fill("code")
    expect(found).to_have_text(["code_task 0.1.0"])
    box.press("Enter")
    expect(box).to_have_value("code_task")
    form.get_by_role("button", name="Start run").click()
    expect(page).to_have_url(re.compile(r"#/neorc/runs/[0-9a-f-]{36}$"))
    expect(page.locator("pre", has_text='"target"')).to_contain_text('"target": "code_task"')

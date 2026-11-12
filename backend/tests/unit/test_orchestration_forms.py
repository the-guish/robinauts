# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Flows as forms (``orchestrator/core/forms.py``): a form file read and checked against its
flow, the lists of values, and inputs checked against the form before a run starts. And the
repositories of ``flows.toml``."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from robinauts.controller.contract.domain import ConfigError
from robinauts.orchestrator.contract.domain import (
    FormInput,
    FormSpec,
    InputSpec,
    LovKind,
    Option,
    Repository,
)
from robinauts.orchestrator.core.config import parse_flows_settings
from robinauts.orchestrator.core.forms import (
    build_forms,
    check_forms,
    check_inputs,
    options_of,
    parse_form,
)
from robinauts.shared_domain.domain import AgentConfig, Config, ModelConfig

FOLDER = Path("/etc/robinauts/flows")
CODE = Path("/opt/robinauts/orchestration")

REPOSITORIES = (
    Repository("app", "https://git.example/app", ("main", "next")),
    Repository("docs", "git@git.example:docs.git", ("main",)),
)
CONFIG = Config(
    models={
        "opus": ModelConfig("opus", "anthropic", "claude-opus", title="Opus"),
        "local": ModelConfig("local", "openai", "local-model"),
    },
    agents={
        "coder": AgentConfig("coder", "Coder", "", ("opus",), "claude-code", worker="claude"),
        "chat": AgentConfig("chat", "", "", ("local", "opus"), "pydantic_ai"),
    },
)
DECLARED = {
    "code": {"task": "string", "repo_branch": "string", "model": "string", "tries": "number"},
    "plain": {"name": "string"},
}


def test_the_repositories_of_flows_toml_are_read_in_order() -> None:
    settings = {
        "repositories": {
            "app": {"url": "https://git.example/app", "branches": ["main", "next"]},
            "docs": {"url": "git@git.example:docs.git", "branches": ["main"]},
        }
    }
    read = parse_flows_settings(settings, folder=FOLDER, default_code=CODE)
    assert read.repositories == REPOSITORIES


@pytest.mark.parametrize(
    ("repositories", "problem"),
    [
        ([], r"\[repositories\]: a table of repositories by id"),
        ({"app": "x"}, r"\[repositories\]\.app: a table with url and branches"),
        ({"app": {"branches": ["main"]}}, r"\[repositories\]\.app url"),
        ({"app": {"url": "u", "branches": []}}, r"\.app branches: a list of branch names"),
        ({"app": {"url": "u", "branches": ["a@b"]}}, r"\.app branches"),
        ({"app": {"url": "u", "branches": "main"}}, r"\.app branches"),
        ({"app": {"url": "u", "branches": ["main"], "token": "x"}}, r"\.app token: unknown key"),
    ],
)
def test_wrong_repositories_are_refused_with_their_problem(repositories: Any, problem: str) -> None:
    with pytest.raises(ConfigError, match=problem):
        parse_flows_settings({"repositories": repositories}, folder=FOLDER, default_code=CODE)


def test_a_form_file_is_read_with_its_order_and_defaults() -> None:
    table = {
        "title": "Code it",
        "inputs": {"task": {"label": "Task"}, "repo_branch": {"lov": "repo_branch"}},
    }
    form, problems = parse_form("code", table)
    assert problems == []
    assert form == FormSpec(
        title="Code it",
        inputs={
            "task": InputSpec(label="Task"),
            "repo_branch": InputSpec(label="repo_branch", lov=LovKind.REPO_BRANCH),
        },
    )
    assert list(form.inputs) == ["task", "repo_branch"]
    assert parse_form("code", {}) == (FormSpec(title="code", inputs={}), [])


@pytest.mark.parametrize(
    ("table", "problem"),
    [
        ({"steps": 1}, "code.form.toml steps: unknown key"),
        ({"title": ""}, "code.form.toml title: a non-empty string"),
        ({"inputs": []}, r"code.form.toml \[inputs\]: a table of inputs by name"),
        ({"inputs": {"task": "Task"}}, r"\[inputs.task\]: a table with label and lov"),
        ({"inputs": {"task": {"label": 3}}}, r"\[inputs.task\] label: a non-empty string"),
        ({"inputs": {"task": {"lov": "colour"}}}, r"lov: 'colour' is not one of repo_branch"),
        ({"inputs": {"task": {"hint": "x"}}}, r"\[inputs.task\] hint: unknown key"),
    ],
)
def test_a_wrong_form_file_is_refused_with_its_problem(table: dict[str, Any], problem: str) -> None:
    form, problems = parse_form("code", table)
    assert form is None
    assert any(re.search(problem, p) for p in problems), problems


@pytest.mark.parametrize(
    ("forms", "problems"),
    [
        ({"code": FormSpec("Code", {"task": InputSpec("Task")})}, []),
        ({"gone": FormSpec("Gone", {})}, ["gone.form.toml: the flows folder holds no flow 'gone'"]),
        (
            {"code": FormSpec("Code", {"colour": InputSpec("Colour")})},
            ["code.form.toml [inputs.colour]: code declares no input 'colour'"],
        ),
        (
            {"code": FormSpec("Code", {"tries": InputSpec("Tries", LovKind.MODEL)})},
            [
                "code.form.toml [inputs.tries] lov: only a string input takes a list of values,"
                " and tries is a number"
            ],
        ),
    ],
)
def test_a_form_is_checked_against_the_inputs_its_flow_declares(
    forms: dict[str, FormSpec], problems: list[str]
) -> None:
    assert check_forms(forms, DECLARED) == problems


@pytest.mark.parametrize(
    ("kind", "options"),
    [
        (
            LovKind.REPO_BRANCH,
            (
                Option("app@main", "app · main"),
                Option("app@next", "app · next"),
                Option("docs@main", "docs · main"),
            ),
        ),
        (LovKind.MODEL, (Option("opus", "Opus"), Option("local", "local"))),
        (
            LovKind.AGENT,
            (
                Option("coder", "Coder · claude", models=("opus",)),
                Option("chat", "chat · default", models=("local", "opus")),
            ),
        ),
    ],
)
def test_each_list_of_values_comes_from_its_configuration(
    kind: LovKind, options: tuple[Option, ...]
) -> None:
    assert options_of(kind, repositories=REPOSITORIES, config=CONFIG) == options


FORMS = build_forms(
    DECLARED,
    {
        "code": FormSpec(
            "Code it",
            {
                "repo_branch": InputSpec("Repository", LovKind.REPO_BRANCH),
                "model": InputSpec("Model", LovKind.MODEL),
            },
        )
    },
    repositories=REPOSITORIES,
    config=CONFIG,
)


def test_every_flow_is_a_form_with_its_form_s_inputs_first() -> None:
    code, plain = FORMS
    assert (code.name, code.title, code.has_form) == ("code", "Code it", True)
    assert [field.name for field in code.inputs] == ["repo_branch", "model", "task", "tries"]
    assert code.inputs[2] == FormInput(name="task", label="task", type="string")
    assert code.inputs[1].options == (Option("opus", "Opus"), Option("local", "local"))
    assert (plain.title, plain.has_form) == ("plain", False)
    assert plain.inputs == (FormInput(name="name", label="name", type="string"),)


VALID = {
    "repo_branch": "app@next",
    "model": "opus",
    "task": "Fix the bug",
    "tries": 3,
}


@pytest.mark.parametrize(
    ("change", "problems"),
    [
        ({}, []),
        ({"model": "gpt"}, ["input 'model': 'gpt' is not one of its model values"]),
        ({"model": ["opus"]}, ["input 'model': ['opus'] is not one of its model values"]),
        ({"repo_branch": "app@dev"}, ["input 'repo_branch': 'app@dev' is not one of its"]),
        ({"colour": "red"}, ["code has no input 'colour'"]),
        ({"task": None}, ["missing input 'task'"]),
    ],
)
def test_inputs_are_checked_against_the_form(change: dict[str, Any], problems: list[str]) -> None:
    inputs = {**VALID, **change}
    if change.get("task", "") is None:
        del inputs["task"]
    found = check_inputs(FORMS[0], inputs)
    assert len(found) == len(problems)
    for got, expected in zip(found, problems, strict=True):
        assert got.startswith(expected)


PAIRED = build_forms(
    {"pair": {"agent": "string", "model": "string"}},
    {
        "pair": FormSpec(
            "Pair",
            {
                "agent": InputSpec("Agent", LovKind.AGENT),
                "model": InputSpec("Model", LovKind.MODEL),
            },
        )
    },
    repositories=(),
    config=CONFIG,
)[0]


@pytest.mark.parametrize(
    ("agent", "model", "problems"),
    [
        ("coder", "opus", []),
        ("coder", "local", ["model 'local' is not one of agent 'coder''s models"]),
        ("chat", "local", []),
        ("chat", "opus", []),
        ("nobody", "opus", ["input 'agent': 'nobody' is not one of its agent values"]),
    ],
)
def test_a_model_is_checked_against_the_agent_chosen_beside_it(
    agent: str, model: str, problems: list[str]
) -> None:
    assert check_inputs(PAIRED, {"agent": agent, "model": model}) == problems

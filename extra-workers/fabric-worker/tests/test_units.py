# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The pure parts: a Fabric result as events, the turn log's chains and seed, a runtime's
configuration, the worker's settings and what keeps it from starting."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    CheckpointNotFoundError,
    ModelProviderConfig,
    ProviderKind,
    ReasoningDelta,
    SessionExistsError,
    SessionNotFoundError,
    TextDelta,
    ToolCall,
    ToolResult,
)
from robinauts_fabric_worker import __main__ as start
from robinauts_fabric_worker import events, fabric_config
from robinauts_fabric_worker.history import SEED_OPENING, TurnLog, seed
from robinauts_fabric_worker.settings import (
    DEFAULT_ALLOWED_TOOLS,
    Settings,
    SettingsError,
    SystemPromptMode,
    settings_from,
)
from tests.conftest import KEY, MODEL, TOOL_SECRET, engine_settings


def entry(kind: str, *content: dict[str, Any], **message: Any) -> dict[str, Any]:
    return {"type": kind, "message": {"content": list(content), **message}}


@pytest.mark.parametrize(
    ("output", "expected", "text"),
    [
        (
            {
                "response": "the end",
                "events": [
                    entry("SystemMessage"),
                    entry(
                        "AssistantMessage",
                        {"thinking": "hm", "signature": "s"},
                        {"text": "first"},
                        {"id": "c1", "name": "Read", "input": {"path": "a"}},
                    ),
                    entry(
                        "UserMessage",
                        {
                            "tool_use_id": "c1",
                            "content": [{"type": "text", "text": "x"}, {"type": "image"}],
                            "is_error": True,
                        },
                    ),
                    entry("AssistantMessage", {"text": "inner"}, parent_tool_use_id="c1"),
                    entry("UserMessage", {"tool_use_id": "c9", "content": None}),
                    entry("AssistantMessage", {"text": "the end"}),
                ],
            },
            [
                ReasoningDelta("hm"),
                TextDelta("first"),
                ToolCall("c1", "Read", {"path": "a"}),
                ToolResult("c1", "Read", 'x\n{"type": "image"}', True),
                ToolResult("c9", "?", "", False),
                TextDelta("the end"),
            ],
            "the end",
        ),
        (
            {"events": [entry("AssistantMessage", {"text": "a"}, {"text": "b"}), "junk"]},
            [TextDelta("a"), TextDelta("b")],
            "ab",
        ),
        ({}, [], ""),
    ],
)
def test_translate(output: dict[str, Any], expected: list[Any], text: str) -> None:
    assert events.translate({"status": "succeeded", "output": output}) == (expected, text)


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"status": "succeeded", "output": {}}, None),
        ({"status": "succeeded", "output": {"stop_reason": "refusal"}}, "declined"),
        ({"status": "failed", "error": {"code": "timeout", "message": "slow"}}, "slow (timeout)"),
        ({"status": "failed", "output": {"subtype": "error_max_turns"}}, "error_max_turns"),
    ],
)
def test_failure(result: dict[str, Any], expected: str | None) -> None:
    said = events.failure(result)
    assert said is None if expected is None else expected in said


def test_credential_refused() -> None:
    assert events.credential_refused({"error": {"metadata": {"api_error_status": 403}}})
    assert not events.credential_refused({"error": {"metadata": {"api_error_status": 500}}})
    assert not events.credential_refused({})


@pytest.mark.parametrize(
    "event",
    [
        TextDelta("a"),
        ReasoningDelta("b"),
        ToolCall("1", "Bash", {"c": 1}),
        ToolResult("1", "Bash", "out", True),
    ],
)
def test_encode_decode(event: Any) -> None:
    assert events.decode(events.encode(event)) == event


def test_the_log_holds_a_tree_of_turns(tmp_path: Path) -> None:
    log = TurnLog(tmp_path / "sessions")
    log.setup()
    session = uuid.uuid4()
    with pytest.raises(SessionNotFoundError):
        log.load(session)
    log.create(session)
    with pytest.raises(SessionExistsError):
        log.create(session)
    one = log.add(session, None, "q1", "a1", [])
    two = log.add(session, one.id, "q2", "a2", [{"kind": "text", "text": "a2"}])
    edit = log.add(session, one.id, "q2'", "a2'", [])
    loaded = log.load(session)
    assert loaded.latest == edit.id
    assert [t.prompt for t in loaded.chain(two.id)] == ["q1", "q2"]
    assert [t.prompt for t in loaded.chain(edit.id)] == ["q1", "q2'"]
    assert loaded.chain(None) == []
    assert loaded.turns[two.id].events == ({"kind": "text", "text": "a2"},)
    with pytest.raises(CheckpointNotFoundError):
        loaded.chain("nope")
    log.forget(session)
    log.forget(session)
    assert not log.exists(session)


def test_the_seed_says_every_turn_in_order(tmp_path: Path) -> None:
    log = TurnLog(tmp_path)
    log.setup()
    session = uuid.uuid4()
    log.create(session)
    one = log.add(session, None, "q1", "a1", [])
    two = log.add(session, one.id, "q2", "a2", [])
    text = seed(log.load(session).chain(two.id))
    assert text.startswith(SEED_OPENING)
    assert text.index("q1") < text.index("a1") < text.index("q2") < text.index("a2")
    assert seed([]) == ""


def build(worker: Settings, environ: dict[str, str], **kwargs: Any) -> fabric_config.Runtime:
    agent = kwargs.pop("agent", AgentDefinition("prompt", tools=("docs", "wiki", "open")))
    return fabric_config.runtime(
        agent=agent,
        model_id=kwargs.pop("model_id", MODEL),
        settings=engine_settings("http://127.0.0.1:1/v1"),
        worker=worker,
        workspace=Path("/w"),
        artifacts=Path("/a"),
        chain=[],
        environ=environ,
        **kwargs,
    )


def test_the_config_names_its_secrets_by_variable(worker: Settings) -> None:
    environ: dict[str, str] = {}
    planned = build(worker, environ)
    dumped = planned.config.model_dump_json()
    assert KEY not in dumped
    assert TOOL_SECRET not in dumped
    model = planned.config.models["default"]
    assert model.provider == "anthropic-compatible"
    assert environ[model.api_key_env] == KEY
    servers = planned.config.mcp.servers
    bearer = servers["docs"].custom_headers["Authorization"]
    assert bearer.startswith("${")
    assert environ[bearer[2:-1]] == f"Bearer {TOOL_SECRET}"
    assert environ[servers["wiki"].custom_headers["Authorization"][2:-1]].startswith("Basic ")
    assert servers["open"].custom_headers == {}
    assert planned.config.environment.env == fabric_config.QUIET
    assert planned.config.tools.blocked == ["AskUserQuestion", "ExitPlanMode", "mcp__docs__delete"]
    assert planned.config.instructions.system.mode == "append"


def test_the_signature_follows_what_starts_a_runtime(worker: Settings) -> None:
    same = build(worker, {}).signature
    assert build(worker, {}).signature == same
    assert build(worker, {}, model_id="other").signature != same
    assert build(worker, {}, agent=AgentDefinition("prompt")).signature != same


def test_a_custom_prompt_replaces_claude_codes(worker: Settings) -> None:
    from dataclasses import replace

    custom = replace(worker, system_prompt=SystemPromptMode.CUSTOM, effort="high")
    planned = build(custom, {})
    assert planned.config.instructions.system.mode == "replace"
    assert planned.config.harness.settings["reasoning_effort"] == "high"


def test_secret_variables_stay_apart() -> None:
    assert fabric_config.secret_variable("TOOL", "a-b") != fabric_config.secret_variable(
        "TOOL", "a_b"
    )


def test_default_settings() -> None:
    settings = settings_from({})
    assert settings.allowed_tools == DEFAULT_ALLOWED_TOOLS
    assert settings.permission_mode == "dontAsk"
    assert settings.max_runtimes == 4


def test_settings_say_every_problem() -> None:
    with pytest.raises(SettingsError) as refused:
        settings_from(
            {
                "ROBINAUTS_DATABASE_URL": "postgresql://x",
                "ROBINAUTS_FLOWS_DIR": "/f",
                "ROBINAUTS_FABRIC_PERMISSION_MODE": "yolo",
                "ROBINAUTS_FABRIC_SYSTEM_PROMPT": "mine",
                "ROBINAUTS_FABRIC_MAX_TURNS": "0",
                "ROBINAUTS_FABRIC_MAX_BUDGET_USD": "free",
                "ROBINAUTS_FABRIC_EFFORT": "huge",
                "ROBINAUTS_FABRIC_MAX_RUNTIMES": "-1",
                "ROBINAUTS_FABRIC_IDLE_SECONDS": "x",
            }
        )
    assert len(str(refused.value).splitlines()) == 9


def test_the_environment_the_worker_refuses() -> None:
    problems = start.environment_problems({"CLAUDE_CODE_OAUTH_TOKEN": "t"})
    assert len(problems) == 3
    assert (
        start.environment_problems(
            {"ROBINAUTS_CONFIG": "/c", "ROBINAUTS_WORKER_BROKER_URL": "http://h"}
        )
        == []
    )


def test_credential_problems() -> None:
    anthropic = ModelProviderConfig("a", ProviderKind.ANTHROPIC, "A_KEY")
    openai = ModelProviderConfig("o", ProviderKind.OPENAI, "O_KEY")
    assert start.credential_problems({"A_KEY": "k"}, [anthropic]) == []
    problems = start.credential_problems({}, [anthropic, openai])
    assert "A_KEY" in problems[0]
    assert "cannot reach" in problems[1]


def test_the_launcher_gives_the_worker_its_folder() -> None:
    state = start.launched_state(
        {
            "ONEBERTH_WORKER_URL": "http://h",
            "ONEBERTH_WORKER_NAME": "fab",
            "ONEBERTH_DATA_DIR": "/d",
        }
    )
    assert state == {
        "CLAUDE_CONFIG_DIR": "/d/workers/fab/claude",
        "ROBINAUTS_FABRIC_WORKSPACES": "/d/workers/fab/workspaces",
        "ROBINAUTS_FABRIC_STATE": "/d/workers/fab/state",
    }
    assert start.launched_state({}) == {}

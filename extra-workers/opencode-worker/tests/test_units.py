# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The pure parts, as rows of inputs and what they give: the checkpoints, the settings, the
credential checks, OpenCode's configuration and the translation of its events."""

from __future__ import annotations

from typing import Any

import pytest

from robinauts.agent_engines.contract.domain import (
    CheckpointNotFoundError,
    MissingSecretError,
    ModelConfig,
    ModelProviderConfig,
    ModelsConfig,
    ProviderKind,
    ReasoningDelta,
    TextDelta,
    ToolCall,
    ToolResult,
    ToolServerAuth,
    ToolServerConfig,
)
from robinauts.agent_engines.contract.ports import ProviderKeyLookup, ToolSecretLookup
from robinauts_opencode_worker import checkpoints, config
from robinauts_opencode_worker.credentials import credential_problems
from robinauts_opencode_worker.events import (
    Translation,
    answer_text,
    final_message,
    refuses_credential,
)
from robinauts_opencode_worker.settings import SettingsError, settings_from

CLAUDE = ModelProviderConfig("claude", ProviderKind.ANTHROPIC, "ANTHROPIC_API_KEY")
PROXY = ModelProviderConfig(
    "proxy", ProviderKind.ANTHROPIC_COMPATIBLE, "PROXY_KEY", base_url="http://proxy:9000/"
)
OPENAI = ModelProviderConfig("openai", ProviderKind.OPENAI, "OPENAI_API_KEY")
LOCAL = ModelProviderConfig(
    "local", ProviderKind.OPENAI_COMPATIBLE, "LOCAL_KEY", base_url="http://llm:8000/v1"
)
NO_URL = ModelProviderConfig("bare", ProviderKind.OPENAI_COMPATIBLE, "BARE_KEY")

MODELS = ModelsConfig(
    providers={p.id: p for p in (CLAUDE, PROXY, OPENAI, LOCAL)},
    models={
        "opus": ModelConfig("opus", "claude", "claude-opus-5-5", title="Opus"),
        "qwen": ModelConfig(
            "qwen", "local", "qwen3", context_window=32_000, max_output_tokens=4_000
        ),
    },
    tool_servers={
        "kb": ToolServerConfig("kb", "https://kb/mcp", "KB", exclude=("drop.me",)),
        "my.git": ToolServerConfig("my.git", "https://git/mcp", auth=ToolServerAuth.NONE),
        "locked": ToolServerConfig("locked", "https://locked/mcp", "LOCKED"),
    },
)


class Keys(ProviderKeyLookup):
    def key_for(self, provider_id: str) -> str:
        if provider_id == "openai":
            raise MissingSecretError(provider_id)
        return f"key-of-{provider_id}"


class Secrets(ToolSecretLookup):
    def secret_for(self, server_id: str) -> str:
        if server_id == "locked":
            raise MissingSecretError(server_id)
        return f"secret-of-{server_id}"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ses_abc:msg_def", checkpoints.Checkpoint("ses_abc", "msg_def")),
        ("ses_abc:prt_def", None),
        ("abc:msg_def", None),
        ("ses_abc", None),
        ("ses_a:msg_b:msg_c", None),
    ],
)
def test_checkpoints(text: str, expected: checkpoints.Checkpoint | None) -> None:
    if expected is None:
        with pytest.raises(CheckpointNotFoundError):
            checkpoints.decode(text)
    else:
        assert checkpoints.decode(text) == expected
        assert checkpoints.encode(expected) == text


@pytest.mark.parametrize(
    ("environ", "problem"),
    [
        ({}, None),
        ({"ROBINAUTS_OPENCODE_MAX_STEPS": "40"}, None),
        ({"ROBINAUTS_OPENCODE_MAX_STEPS": "0"}, "MAX_STEPS"),
        ({"ROBINAUTS_OPENCODE_START_SECONDS": "soon"}, "START_SECONDS"),
        ({"ROBINAUTS_DATABASE_URL": "postgresql://x"}, "ROBINAUTS_DATABASE_URL is set"),
        ({"OPENCODE_AUTH_CONTENT": "{}"}, "OPENCODE_AUTH_CONTENT is set"),
    ],
)
def test_settings(environ: dict[str, str], problem: str | None) -> None:
    if problem is None:
        assert settings_from(environ).cli_path == "opencode"
    else:
        with pytest.raises(SettingsError, match=problem):
            settings_from(environ)


@pytest.mark.parametrize(
    ("environ", "providers", "problems"),
    [
        ({"ANTHROPIC_API_KEY": "k", "LOCAL_KEY": "k"}, [CLAUDE, LOCAL], []),
        ({}, [CLAUDE], ["ANTHROPIC_API_KEY, which is unset"]),
        ({"BARE_KEY": "k"}, [NO_URL], ["names no base_url"]),
    ],
)
def test_credential_problems(
    environ: dict[str, str], providers: list[ModelProviderConfig], problems: list[str]
) -> None:
    found = credential_problems(environ, providers)
    assert len(found) == len(problems)
    for line, expected in zip(found, problems, strict=True):
        assert expected in line


@pytest.mark.parametrize(
    ("provider", "url"),
    [
        (CLAUDE, None),
        (PROXY, "http://proxy:9000/v1"),
        (LOCAL, "http://llm:8000/v1"),
        (
            ModelProviderConfig(
                "a", ProviderKind.ANTHROPIC_COMPATIBLE, "K", base_url="http://a/v1"
            ),
            "http://a/v1",
        ),
    ],
)
def test_base_url(provider: ModelProviderConfig, url: str | None) -> None:
    assert config.base_url(provider) == url


def test_the_configuration_holds_the_providers_this_process_has_keys_for() -> None:
    whole = config.configuration(MODELS, Keys(), Secrets(), max_steps=12)
    assert whole["enabled_providers"] == ["robinauts-claude", "robinauts-local", "robinauts-proxy"]
    local = whole["provider"]["robinauts-local"]
    assert local == {
        "npm": "@ai-sdk/openai-compatible",
        "name": "local",
        "options": {"apiKey": "key-of-local", "baseURL": "http://llm:8000/v1"},
        "models": {
            "qwen3": {
                "name": "qwen3",
                "tool_call": True,
                "limit": {"context": 32_000, "output": 4_000},
            }
        },
    }
    assert whole["provider"]["robinauts-claude"]["models"]["claude-opus-5-5"]["name"] == "Opus"
    assert whole["provider"]["robinauts-proxy"]["models"] == {}
    assert set(whole["mcp"]) == {"kb", "my.git"}
    assert whole["mcp"]["kb"]["headers"] == {"Authorization": "Bearer secret-of-kb"}
    assert whole["mcp"]["my.git"]["headers"] == {}
    assert whole["agent"][config.AGENT]["steps"] == 12
    assert whole["permission"]["question"] == "deny"
    assert list(whole["permission"])[0] == "*"
    assert whole["share"] == "disabled"


@pytest.mark.parametrize(
    ("allowed", "functions", "expected"),
    [
        (
            ("kb",),
            False,
            [
                ("kb_*", "allow"),
                ("my_git_*", "deny"),
                ("locked_*", "deny"),
                ("kb_drop_me", "deny"),
                ("robinauts_*", "deny"),
            ],
        ),
        (
            (),
            True,
            [
                ("kb_*", "deny"),
                ("my_git_*", "deny"),
                ("locked_*", "deny"),
                ("robinauts_*", "allow"),
            ],
        ),
    ],
)
def test_tool_rules(
    allowed: tuple[str, ...], functions: bool, expected: list[tuple[str, str]]
) -> None:
    rules = config.tool_rules(allowed, MODELS, functions)
    assert [(rule["permission"], rule["action"]) for rule in rules] == expected
    assert {rule["pattern"] for rule in rules} == {"*"}


@pytest.mark.parametrize(
    ("name", "shown"),
    [
        ("kb_search", "search"),
        ("my_git_log", "log"),
        ("robinauts_search_sessions", "search_sessions"),
        ("bash", "bash"),
        ("kb_", "kb_"),
    ],
)
def test_tool_name(name: str, shown: str) -> None:
    assert config.tool_name(name, MODELS) == shown


def _part(message: str, part: str, kind: str, **fields: Any) -> tuple[str, dict[str, Any]]:
    body = {"id": part, "messageID": message, "sessionID": "ses_1", "type": kind, **fields}
    return "message.part.updated", {"sessionID": "ses_1", "part": body}


def _message(message: str, role: str, **fields: Any) -> tuple[str, dict[str, Any]]:
    info = {"id": message, "sessionID": "ses_1", "role": role, **fields}
    return "message.updated", {"sessionID": "ses_1", "info": info}


def _delta(message: str, part: str, text: str) -> tuple[str, dict[str, Any]]:
    properties = {"sessionID": "ses_1", "messageID": message, "partID": part, "field": "text"}
    return "message.part.delta", {**properties, "delta": text}


TURN = [
    ("session.idle", {"sessionID": "ses_1"}),  # before the turn started: not its end
    _message("msg_old", "assistant"),
    _delta("msg_old", "prt_old", "an earlier turn's"),
    _message("msg_u", "user"),
    _part("msg_u", "prt_u", "text", text="hello", time={"end": 1}),
    ("session.status", {"sessionID": "ses_1", "status": {"type": "busy"}}),
    _part("msg_a1", "prt_r", "reasoning", text="", time={"start": 1}),  # before its message
    _message("msg_a1", "assistant", parentID="msg_u"),
    _delta("msg_a1", "prt_r", "pondering"),
    _part(
        "msg_a1",
        "prt_t1",
        "tool",
        callID="c1",
        tool="kb_search",
        state={"status": "pending", "input": {}},
    ),
    _part(
        "msg_a1",
        "prt_t1",
        "tool",
        callID="c1",
        tool="kb_search",
        state={"status": "running", "input": {"q": "x"}},
    ),
    _part(
        "msg_a1",
        "prt_t1",
        "tool",
        callID="c1",
        tool="kb_search",
        state={"status": "running", "input": {"q": "x"}, "metadata": {"n": 1}},
    ),
    _part(
        "msg_a1",
        "prt_t1",
        "tool",
        callID="c1",
        tool="kb_search",
        state={"status": "completed", "input": {"q": "x"}, "output": "hit"},
    ),
    _part(
        "msg_a1",
        "prt_t2",
        "tool",
        callID="c2",
        tool="bash",
        state={"status": "error", "input": {"command": "false"}, "error": "exit 1"},
    ),
    _part(
        "msg_a1",
        "prt_t3",
        "tool",
        callID="c3",
        tool="bash",
        state={"status": "running", "input": {"command": "sleep 9"}},
    ),
    (
        "message.part.delta",
        {"sessionID": "ses_child", "messageID": "msg_x", "partID": "p", "delta": "a subagent's"},
    ),
    _message("msg_a2", "assistant", parentID="msg_u"),
    _part("msg_a2", "prt_t", "text", text="", time={"start": 1}),
    _delta("msg_a2", "prt_t", "Hel"),
    _delta("msg_a2", "prt_t", "lo"),
    _part("msg_a2", "prt_t", "text", text="Hello", time={"start": 1, "end": 2}),
    _part("msg_a2", "prt_w", "text", text=" whole", time={"start": 1, "end": 2}),
    ("session.idle", {"sessionID": "ses_1"}),
    _delta("msg_a2", "prt_t", "after the end"),
]


def test_a_turn_translates_to_the_contract_events() -> None:
    translation = Translation(
        "ses_1", frozenset({"msg_old"}), name=lambda tool: config.tool_name(tool, MODELS)
    )
    events = [e for kind, props in TURN for e in translation.translate(kind, props)]
    assert events == [
        ReasoningDelta("pondering"),
        ToolCall("c1", "search", {"q": "x"}),
        ToolResult("c1", "search", "hit", False),
        ToolCall("c2", "bash", {"command": "false"}),
        ToolResult("c2", "bash", "exit 1", True),
        ToolCall("c3", "bash", {"command": "sleep 9"}),
        TextDelta("Hel"),
        TextDelta("lo"),
        TextDelta(" whole"),
        ToolResult("c3", "bash", "the call did not finish before the turn ended", True),
    ]
    assert translation.ended
    assert translation.user_message == "msg_u"
    assert translation.streamed() == "Hello whole"


def test_the_answer_is_the_last_assistant_message_of_the_turn() -> None:
    messages = [
        {"info": {"id": "msg_old", "role": "assistant"}, "parts": []},
        {"info": {"id": "msg_u", "role": "user"}, "parts": [{"type": "text", "text": "hi"}]},
        {"info": {"id": "msg_a1", "role": "assistant", "parentID": "msg_u"}, "parts": []},
        {
            "info": {"id": "msg_a2", "role": "assistant", "parentID": "msg_u"},
            "parts": [
                {"type": "step-start"},
                {"type": "text", "text": "Hello"},
                {"type": "text", "text": " note", "synthetic": True},
                {"type": "text", "text": " there"},
            ],
        },
    ]
    final = final_message(messages, "msg_u", frozenset({"msg_old"}))
    assert final is not None
    assert final["info"]["id"] == "msg_a2"
    assert answer_text(final) == "Hello there"
    assert final_message(messages[:2], "msg_u", frozenset({"msg_old"})) is None


@pytest.mark.parametrize(
    ("error", "refused"),
    [
        ({"name": "ProviderAuthError", "data": {"message": "bad key"}}, True),
        ({"name": "APIError", "data": {"message": "no", "statusCode": 401}}, True),
        ({"name": "APIError", "data": {"message": "busy", "statusCode": 429}}, False),
        ({"name": "MessageAbortedError", "data": {}}, False),
        (None, False),
    ],
)
def test_refuses_credential(error: dict[str, Any] | None, refused: bool) -> None:
    assert refuses_credential(error) is refused

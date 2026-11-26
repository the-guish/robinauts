# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The pure parts, as rows of inputs and what they give: the checkpoints, the settings, the
credential checks, Codex's command line and the translation of its notifications."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    CheckpointNotFoundError,
    MissingSecretError,
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
from robinauts_codex_worker import checkpoints, config
from robinauts_codex_worker.credentials import credential_problems, subprocess_environment
from robinauts_codex_worker.events import Translation, call_of, http_status, result_of
from robinauts_codex_worker.settings import AuthMode, SettingsError, settings_from

OPENAI = ModelProviderConfig("openai", ProviderKind.OPENAI, "OPENAI_API_KEY")
GATEWAY = ModelProviderConfig(
    "gw", ProviderKind.OPENAI_COMPATIBLE, "GW_KEY", base_url="http://gw:8000/v1"
)
CLAUDE = ModelProviderConfig("claude", ProviderKind.ANTHROPIC, "ANTHROPIC_API_KEY")
LOCAL = "http://127.0.0.1:8001/robinauts"
REMOTE = "https://robinauts.example.com/robinauts"


class Keys(ProviderKeyLookup):
    def key_for(self, provider_id: str) -> str:
        return f"key-of-{provider_id}"


class Secrets(ToolSecretLookup):
    def secret_for(self, server_id: str) -> str:
        if server_id == "none":
            raise MissingSecretError(server_id)
        return f"secret-of-{server_id}"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("019a-t1:019a-u1", checkpoints.Checkpoint("019a-t1", "019a-u1")),
        ("t:u", checkpoints.Checkpoint("t", "u")),
        ("t", None),
        ("t:", None),
        (":u", None),
        ("t:u:v", None),
        ("t u:v", None),
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
        ({"ROBINAUTS_CODEX_AUTH": "deployed"}, None),
        ({"ROBINAUTS_CODEX_AUTH": "personal", "ROBINAUTS_CODEX_EFFORT": "high"}, None),
        ({}, "ROBINAUTS_CODEX_AUTH must be"),
        ({"ROBINAUTS_CODEX_AUTH": "both"}, "ROBINAUTS_CODEX_AUTH must be"),
        (
            {"ROBINAUTS_CODEX_AUTH": "deployed", "ROBINAUTS_DATABASE_URL": "postgresql://x"},
            "ROBINAUTS_DATABASE_URL is set",
        ),
        ({"ROBINAUTS_CODEX_AUTH": "deployed", "ROBINAUTS_CODEX_SANDBOX": "none"}, "SANDBOX"),
        ({"ROBINAUTS_CODEX_AUTH": "deployed", "ROBINAUTS_CODEX_EFFORT": "max"}, "EFFORT"),
        (
            {"ROBINAUTS_CODEX_AUTH": "deployed", "ROBINAUTS_CODEX_REASONING_SUMMARY": "x"},
            "REASONING_SUMMARY",
        ),
        ({"ROBINAUTS_CODEX_AUTH": "deployed", "ROBINAUTS_CODEX_SYSTEM_PROMPT": "x"}, "PROMPT"),
    ],
)
def test_settings(environ: dict[str, str], problem: str | None) -> None:
    if problem is None:
        settings = settings_from({**environ, "CODEX_HOME": "/var/lib/codex"})
        assert settings.home == Path("/var/lib/codex")
        assert settings.sandbox == "danger-full-access"
    else:
        with pytest.raises(SettingsError, match=problem):
            settings_from(environ)


def test_the_settings_say_every_problem_at_once() -> None:
    with pytest.raises(SettingsError) as refused:
        settings_from({"ROBINAUTS_CODEX_SANDBOX": "none", "ROBINAUTS_CODEX_EFFORT": "max"})
    assert len(str(refused.value).splitlines()) == 3


@pytest.mark.parametrize(
    ("mode", "environ", "login", "url", "providers", "problems"),
    [
        (AuthMode.PERSONAL, {}, True, LOCAL, [OPENAI], []),
        (AuthMode.PERSONAL, {}, False, LOCAL, [OPENAI], ["needs"]),
        (AuthMode.PERSONAL, {}, True, REMOTE, [OPENAI], ["not on this machine"]),
        (AuthMode.PERSONAL, {}, True, LOCAL, [GATEWAY], ["refuses provider 'gw'"]),
        (AuthMode.PERSONAL, {"CODEX_ACCESS_TOKEN": "t"}, True, LOCAL, [OPENAI], ["not supported"]),
        (
            AuthMode.DEPLOYED,
            {"OPENAI_API_KEY": "k", "GW_KEY": "k"},
            False,
            REMOTE,
            [OPENAI, GATEWAY],
            [],
        ),
        (AuthMode.DEPLOYED, {"OPENAI_API_KEY": "k"}, True, REMOTE, [OPENAI], ["refuses"]),
        (AuthMode.DEPLOYED, {}, False, REMOTE, [OPENAI], ["OPENAI_API_KEY, which is unset"]),
        (AuthMode.DEPLOYED, {"ANTHROPIC_API_KEY": "k"}, False, REMOTE, [CLAUDE], ["cannot reach"]),
    ],
)
def test_credential_problems(
    tmp_path: Path,
    mode: AuthMode,
    environ: dict[str, str],
    login: bool,
    url: str,
    providers: list[ModelProviderConfig],
    problems: list[str],
) -> None:
    if login:
        (tmp_path / "auth.json").write_text("{}")
    found = credential_problems(mode, environ, tmp_path, url, providers)
    assert len(found) == len(problems)
    for line, expected in zip(found, problems, strict=True):
        assert expected in line


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (AuthMode.PERSONAL, {}),
        (AuthMode.DEPLOYED, {"GW_KEY": "key-of-gw"}),
    ],
)
def test_the_subprocess_is_handed_the_key_of_its_mode(
    mode: AuthMode, expected: dict[str, str]
) -> None:
    assert subprocess_environment(mode, GATEWAY, Keys()) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, "true"),
        (3, "3"),
        (2.5, "2.5"),
        ('a "q"', '"a \\"q\\""'),
        (["a", 1], '["a", 1]'),
        ({"k.x": {"n": False}}, '{"k.x" = {"n" = false}}'),
        ({}, "{}"),
    ],
)
def test_toml(value: Any, expected: str) -> None:
    assert config.toml(value) == expected
    assert tomllib.loads(f"v = {config.toml(value)}")["v"] == value


MODELS = ModelsConfig(
    providers={"openai": OPENAI, "gw": GATEWAY, "claude": CLAUDE},
    tool_servers={
        "kb": ToolServerConfig("kb", "https://kb/mcp", "KB", exclude=("drop",)),
        "open": ToolServerConfig("open", "https://open/mcp", auth=ToolServerAuth.NONE),
        "head": ToolServerConfig(
            "head", "https://head/mcp", "H", auth=ToolServerAuth.HEADER, header="X-Key"
        ),
    },
)


def _overrides(mode: AuthMode, tools: tuple[str, ...]) -> tuple[dict[str, Any], dict[str, str]]:
    arguments, environment = config.overrides(
        mode, AgentDefinition("p", tools=tools), MODELS, Secrets()
    )
    assert arguments[::2] == ["-c"] * (len(arguments) // 2)
    parsed: dict[str, Any] = {}
    for argument in arguments[1::2]:
        key, _, value = argument.partition("=")
        parsed[key] = tomllib.loads(f"v = {value}")["v"]
    return parsed, environment


def test_the_command_line_holds_no_secret_and_phones_nowhere() -> None:
    parsed, environment = _overrides(AuthMode.DEPLOYED, ("kb", "head", "open"))
    assert parsed["analytics.enabled"] is False
    assert parsed["otel.exporter"] == "none"
    assert parsed["tools.experimental_request_user_input.enabled"] is False
    assert set(parsed["model_providers"]) == {"robinauts_openai", "robinauts_gw"}
    assert parsed["model_providers"]["robinauts_gw"] == {
        "name": "gw",
        "base_url": "http://gw:8000/v1",
        "env_key": "GW_KEY",
        "wire_api": "responses",
        "requires_openai_auth": False,
    }
    assert parsed["model_providers"]["robinauts_openai"]["base_url"] == config.OPENAI_URL
    kb = parsed["mcp_servers"]["kb"]
    assert kb["disabled_tools"] == ["drop"]
    assert kb["default_tools_approval_mode"] == "approve"
    assert "env_http_headers" not in parsed["mcp_servers"]["open"]
    headers = {**kb["env_http_headers"], **parsed["mcp_servers"]["head"]["env_http_headers"]}
    assert {header: environment[v] for header, v in headers.items()} == {
        "Authorization": "Bearer secret-of-kb",
        "X-Key": "secret-of-head",
    }
    assert "secret-of" not in repr(parsed)


def test_personal_mode_runs_on_codex_own_provider() -> None:
    parsed, environment = _overrides(AuthMode.PERSONAL, ())
    assert "model_providers" not in parsed
    assert parsed["forced_login_method"] == "chatgpt"
    assert parsed["mcp_servers"] == {}
    assert environment == {}
    assert config.provider_id(AuthMode.PERSONAL, OPENAI) == "openai"
    assert config.provider_id(AuthMode.DEPLOYED, OPENAI) == "robinauts_openai"


@pytest.mark.parametrize(
    ("item", "call", "result"),
    [
        (
            {
                "type": "commandExecution",
                "command": "ls",
                "cwd": "/w",
                "status": "completed",
                "aggregatedOutput": "a\n",
                "exitCode": 0,
            },
            ("shell", {"command": "ls", "cwd": "/w"}),
            ("a\n", False, None),
        ),
        (
            {
                "type": "commandExecution",
                "command": "false",
                "cwd": "/w",
                "status": "failed",
                "aggregatedOutput": None,
                "exitCode": 1,
            },
            ("shell", {"command": "false", "cwd": "/w"}),
            ("", True, None),
        ),
        (
            {
                "type": "fileChange",
                "status": "completed",
                "changes": [{"path": "a.py", "kind": {"type": "add"}, "diff": "+x"}],
            },
            ("apply_patch", {"changes": [{"path": "a.py", "kind": "add"}]}),
            ("+x", False, None),
        ),
        (
            {
                "type": "mcpToolCall",
                "server": "kb",
                "tool": "search",
                "status": "completed",
                "arguments": {"q": "x"},
                "error": None,
                "result": {
                    "content": [{"type": "text", "text": "hit"}],
                    "structuredContent": {"n": 1},
                },
            },
            ("search", {"q": "x"}),
            ("hit", False, {"n": 1}),
        ),
        (
            {
                "type": "mcpToolCall",
                "server": "kb",
                "tool": "search",
                "status": "failed",
                "arguments": "raw",
                "error": {"message": "down"},
                "result": None,
            },
            ("search", {"arguments": "raw"}),
            ("down", True, None),
        ),
        (
            {
                "type": "dynamicToolCall",
                "tool": "find",
                "arguments": {},
                "status": "completed",
                "success": False,
                "contentItems": [{"type": "inputText", "text": "no"}],
            },
            ("find", {}),
            ("no", True, None),
        ),
        (
            {"type": "webSearch", "query": "q", "results": None},
            ("web_search", {"query": "q"}),
            ("", False, None),
        ),
        ({"type": "agentMessage", "text": "hi"}, None, ("", False, None)),
        ({"type": "reasoning"}, None, ("", False, None)),
    ],
)
def test_items(
    item: dict[str, Any], call: tuple[str, dict[str, Any]] | None, result: tuple[Any, ...]
) -> None:
    assert call_of(item) == call
    assert result_of(item) == result


@pytest.mark.parametrize(
    ("error", "status"),
    [
        ({"codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 401}}}, 401),
        ({"codexErrorInfo": {"responseTooManyFailedAttempts": {"httpStatusCode": 503}}}, 503),
        ({"codexErrorInfo": "unauthorized"}, None),
        ({"codexErrorInfo": None}, None),
        (None, None),
    ],
)
def test_http_status(error: dict[str, Any] | None, status: int | None) -> None:
    assert http_status(error) == status


def _n(method: str, turn: str = "u1", thread: str = "t1", **params: Any) -> tuple[str, dict]:
    return method, {"threadId": thread, "turnId": turn, **params}


TURN = [
    _n("item/started", item={"type": "userMessage", "id": "i0"}),
    _n("item/reasoning/summaryTextDelta", itemId="r1", delta="Thinking"),
    _n("item/reasoning/summaryPartAdded", itemId="r1", summaryIndex=1),
    _n("item/reasoning/textDelta", itemId="r1", delta="raw, not shown"),
    _n("item/reasoning/textDelta", itemId="r2", delta="raw alone"),
    _n(
        "item/started",
        item={"type": "mcpToolCall", "id": "c1", "tool": "search", "arguments": {"q": "x"}},
    ),
    _n("item/agentMessage/delta", turn="u0", itemId="m0", delta="another turn's"),
    _n("item/agentMessage/delta", thread="sub", itemId="m0", delta="a subagent's"),
    _n(
        "item/completed",
        item={
            "type": "mcpToolCall",
            "id": "c1",
            "tool": "search",
            "status": "completed",
            "result": {"content": "hit"},
        },
    ),
    _n("item/started", item={"type": "commandExecution", "id": "c2", "command": "ls"}),
    _n("item/agentMessage/delta", itemId="m1", delta="Hel"),
    _n("item/agentMessage/delta", itemId="m1", delta="lo"),
    _n("item/completed", item={"type": "agentMessage", "id": "m1", "text": "Hello"}),
    _n("item/completed", item={"type": "agentMessage", "id": "m2", "text": " whole"}),
    (
        "turn/completed",
        {
            "threadId": "t1",
            "turn": {
                "id": "u1",
                "status": "completed",
                "items": [{"type": "agentMessage", "text": "Hello whole"}],
            },
        },
    ),
]


def test_a_turn_translates_to_the_contract_events() -> None:
    translation = Translation("t1", "u1")
    events = [e for method, params in TURN for e in translation.translate(method, params)]
    assert events == [
        ReasoningDelta("Thinking"),
        ReasoningDelta("\n\n"),
        ReasoningDelta("raw alone"),
        ToolCall("c1", "search", {"q": "x"}),
        ToolResult("c1", "search", "hit", False, None),
        ToolCall("c2", "shell", {"command": "ls", "cwd": None}),
        TextDelta("Hel"),
        TextDelta("lo"),
        TextDelta(" whole"),
        ToolResult("c2", "shell", "the call did not finish before the turn ended", True),
    ]
    assert translation.ended
    assert translation.failure() is None
    assert translation.done("t1:u1").text == "Hello whole"


@pytest.mark.parametrize(
    ("notifications", "failure", "refused"),
    [
        (
            [
                _n("error", willRetry=True, error={"message": "retrying"}),
                ("turn/completed", {"threadId": "t1", "turn": {"id": "u1", "status": "completed"}}),
            ],
            None,
            False,
        ),
        (
            [
                _n(
                    "error",
                    willRetry=False,
                    error={
                        "message": "401 Unauthorized",
                        "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 401}},
                    },
                ),
                ("turn/completed", {"threadId": "t1", "turn": {"id": "u1", "status": "failed"}}),
            ],
            "401 Unauthorized",
            True,
        ),
        (
            [("turn/completed", {"threadId": "t1", "turn": {"id": "u1", "status": "interrupted"}})],
            "Codex ended the turn interrupted",
            False,
        ),
        ([], "Codex ended without completing the turn", False),
    ],
)
def test_how_a_turn_ends(
    notifications: list[tuple[str, dict]], failure: str | None, refused: bool
) -> None:
    translation = Translation("t1", "u1")
    for method, params in notifications:
        translation.translate(method, params)
    assert translation.failure() == failure
    assert translation.credential_refused() is refused

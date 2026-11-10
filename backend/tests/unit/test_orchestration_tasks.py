# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The handlers of the flows Robinauts ships (``orchestration/robinauts_tasks.py``): what their
requests carry. The module is loaded from its file, with no bytecode written, since
``orchestration/`` is no package of the backend's and is left as it is in the checkout."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

TASKS = Path(__file__).resolve().parents[3] / "orchestration" / "robinauts_tasks.py"


@pytest.fixture(scope="module")
def tasks() -> ModuleType:
    spec = importlib.util.spec_from_file_location("robinauts_tasks", TASKS)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    writing = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = writing
    return module


@pytest.mark.parametrize(
    ("environ", "headers"),
    [
        ({}, {}),
        ({"ROBINAUTS_API_TOKEN": ""}, {}),
        ({"ROBINAUTS_API_TOKEN": "rbt_minted"}, {"Authorization": "Bearer rbt_minted"}),
    ],
)
def test_a_request_carries_a_bearer_only_when_the_worker_was_given_an_api_token(
    tasks: ModuleType, environ: dict[str, str], headers: dict[str, str]
) -> None:
    assert tasks.api_headers(environ) == headers


MODELS = [
    {"id": "echo", "title": "Echo"},
    {"id": "claude-haiku-4-5", "title": "Claude Haiku 4.5"},
    {"id": "fast", "title": "Haiku, through OpenRouter"},
]


@pytest.mark.parametrize(
    ("name", "model"),
    [("haiku", "claude-haiku-4-5"), ("HAIKU", "claude-haiku-4-5"), ("openrouter", "fast")],
)
def test_the_game_s_model_is_the_first_whose_id_or_title_holds_the_name(
    tasks: ModuleType, name: str, model: str
) -> None:
    assert tasks.model_named(MODELS, name) == model


def test_a_server_without_the_model_is_refused_with_what_it_offers(tasks: ModuleType) -> None:
    with pytest.raises(RuntimeError, match="no sonnet model .it offers: echo, claude-haiku"):
        tasks.model_named(MODELS, "sonnet")


def message(role: str, *texts: str, failed: bool = False) -> dict[str, object]:
    parts: list[dict[str, object]] = [{"kind": "tool_call", "call_id": "1", "name": "t"}]
    parts += [{"kind": "text", "text": text} for text in texts]
    return {"id": f"{role}-id", "role": role, "parts": parts, "failed": failed}


def opened(*messages: dict[str, object]) -> dict[str, object]:
    return {
        "conversation": {"id": "c1"},
        "messages": list(messages),
        "ended_badly": {"state": "failed"},
    }


def test_an_answer_is_carried_as_it_is_its_text_parts_joined(tasks: ModuleType) -> None:
    conversation = opened(message("user", "hi"), message("assistant", "TOO ", "LOW"))
    assert tasks.answer_of(conversation) == ("assistant-id", "TOO LOW")


@pytest.mark.parametrize(
    "messages",
    [
        (),
        (message("user", "here is the guess: 4"),),
        (message("user", "hi"), message("assistant", "TOO", failed=True)),
    ],
)
def test_a_conversation_whose_last_turn_gave_no_answer_has_nothing_to_carry(
    tasks: ModuleType, messages: tuple[dict[str, object], ...]
) -> None:
    with pytest.raises(RuntimeError, match="no answer to carry: its last turn ended failed"):
        tasks.answer_of(opened(*messages))


@pytest.mark.parametrize(
    ("verdict", "hit"),
    [
        ("HIT", True),
        ("Hit!", True),
        ("HIT - it was 7", True),
        ("TOO LOW", False),
        ("TOO HIGH", False),
        ("this is not a match", False),
    ],
)
def test_only_a_hit_ends_the_game(tasks: ModuleType, verdict: str, hit: bool) -> None:
    assert tasks.is_hit(verdict) is hit


@pytest.mark.parametrize(
    ("hits", "loop_count", "over"),
    [
        ([False], 1, False),
        ([False, True], 2, True),
        ([False] * 9, 9, False),
        ([False] * 10, 10, True),
    ],
)
def test_the_game_is_over_on_a_hit_or_on_the_last_try(
    tasks: ModuleType, hits: list[bool], loop_count: int, over: bool
) -> None:
    judged = [{"guess": "5", "verdict": "x", "hit": hit} for hit in hits]
    assert tasks.game_over(judged, loop_count, 10) is over


def test_once_the_game_is_over_the_guesser_is_told_nothing(tasks: ModuleType) -> None:
    judged = [{"guess": "7", "verdict": "HIT", "hit": True}]
    assert tasks.relay_verdict({"conversation_id": "c2"}, judged, [True]) is None


def test_the_run_says_whether_the_guesser_won_and_in_how_many_tries(tasks: ModuleType) -> None:
    judged = [
        {"guess": "5", "verdict": "TOO LOW", "hit": False},
        {"guess": "8", "verdict": "HIT", "hit": True},
    ]
    assert tasks.summarize(9, judged) == {"number": 9, "won": True, "tries": 2, "rounds": judged}


def test_the_number_is_a_whole_number_between_1_and_10(tasks: ModuleType) -> None:
    assert {tasks.pick_number() for _ in range(1000)} == set(range(1, 11))


@pytest.mark.parametrize(("number", "told"), [(1, "number is 1."), (10, "number is 10.")])
def test_the_game_is_told_the_number_it_keeps_secret(
    tasks: ModuleType, number: int, told: str
) -> None:
    assert told in tasks.game_prompt(number)


@pytest.mark.parametrize(
    ("value", "split"),
    [
        ("app@main", ("app", "main")),
        ("app@feature/x", ("app", "feature/x")),
    ],
)
def test_a_repository_and_its_branch_are_split_at_the_last_at(
    tasks: ModuleType, value: str, split: tuple[str, str]
) -> None:
    assert tasks.split_repo_branch(value) == split


@pytest.mark.parametrize("value", ["app", "@main", "app@"])
def test_a_value_that_is_not_a_repository_and_a_branch_is_refused(
    tasks: ModuleType, value: str
) -> None:
    with pytest.raises(RuntimeError, match="is not a repository and a branch"):
        tasks.split_repo_branch(value)


@pytest.mark.parametrize(
    ("answer", "result"),
    [
        ('Done.\nRESULT {"pr_url": "u"}', {"pr_url": "u"}),
        ('RESULT {"a": 1}\nmore\n`RESULT {"a": 2}`\n', {"a": 2}),
        ('  RESULT   {"verdict": "approve"}  ', {"verdict": "approve"}),
    ],
)
def test_the_result_is_the_json_of_the_last_result_line(
    tasks: ModuleType, answer: str, result: dict[str, object]
) -> None:
    assert tasks.result_of(answer) == result


@pytest.mark.parametrize(
    "answer",
    ["Done, no line.", "RESULT not json", 'RESULT ["a list"]', 'RESULT {"a": 1}\nRESULT oops'],
)
def test_an_answer_without_a_result_line_of_json_is_refused(tasks: ModuleType, answer: str) -> None:
    with pytest.raises(RuntimeError, match="no RESULT line of JSON"):
        tasks.result_of(answer)


@pytest.mark.parametrize(
    ("url", "is_one"),
    [
        ("https://github.com/o/r/pull/12", True),
        ("https://ghe.example/o/r/pull/3", True),
        ("<the pull request's URL>", False),
        ("https://github.com/o/r/issues/12", False),
        ("https://github.com/o/r/pull/12/files", False),
    ],
)
def test_a_pull_request_is_told_by_its_url(tasks: ModuleType, url: str, is_one: bool) -> None:
    assert bool(tasks.PULL_REQUEST.fullmatch(url)) is is_one


def test_the_prompts_carry_the_task_and_ask_for_their_result_line(tasks: ModuleType) -> None:
    task = "Fix {the} bug"
    implement = tasks.IMPLEMENT_PROMPT.format(
        repo="app", base="main", task=task, result=tasks.RESULT
    )
    assert "Fix {the} bug" in implement
    assert "the folder `app`" in implement
    assert "git reset --hard origin/main" in implement
    assert implement.rstrip().endswith(
        'RESULT {"pr_url": "<the pull request\'s URL>", "branch": "<the branch you pushed>"}'
    )
    review = tasks.REVIEW_PROMPT.format(
        pr_url="https://github.com/o/r/pull/1",
        repo="app",
        task=task,
        branch="rb/1234",
        base="main",
        result=tasks.RESULT,
    )
    assert "git diff origin/main...origin/rb/1234" in review
    assert review.rstrip().endswith(
        'RESULT {"verdict": "approve" or "request_changes", "summary": "<two or three sentences>"}'
    )

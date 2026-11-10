# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""neorc's handlers for the flows in ``flows/``: plain functions that reach the Robinauts API.

They run in a neorc worker, which is handed the server's URL in ``ROBINAUTS_API_URL``. The
worker ``robinauts start`` starts beside itself in the local development mode
(``docs/architecture/orchestration.md``) is handed nothing else: that mode has no sign-in, and
every request is its one user's, so what a flow starts is the developer's. A worker started on
its own, against a server with sign-in on, is given an API token a signed-in person minted
(``docs/specs/sign-in.md``, "API tokens") in ``ROBINAUTS_API_TOKEN``, and the handlers send it
as a bearer: what they start is then that person's.

They import neither neorc nor Robinauts, and nothing but the standard library, so that the
worker needs nothing installed beside neorc itself.

The guessing game (``flows/guess_number.yaml``) is played between two conversations:

- the **game**, on a Haiku model, which is told the secret number and judges every guess;
- the **guesser**, which is asked to guess, and is told each verdict.

``pick_number`` draws the secret number before either conversation opens. neorc keeps it as
that task's output, so the number exists outside the model.

The handlers carry each answer from one conversation to the other as it is. The one thing
they add is ``"here is the guess: "`` before a guess they hand the game. A turn is waited for
by reading its stream to the end; its answer is then read from the conversation.

The code and review (``flows/code_and_review.yaml``) is two conversations on the agent and
the model the run was started with:

- **implement**: the agent clones the repository at the chosen branch, does the task on a
  branch of its own, pushes it and opens a pull request against the chosen branch;
- **review**: a second conversation reviews that pull request against the task, and changes
  nothing.

Each ends its answer with one ``RESULT`` line of JSON, which the handler reads: the pull
request's URL, then the review's verdict. An answer without one, or with one that is not what
was asked, fails the task with what it said. The agent reaches GitHub with the token its
worker was given (``extra-workers/worker-common/README.md``): the handlers hold none.

neorc delivers a task at least once: a task that runs again sends its message again. For the
code and review, that is a second pull request.
"""

from __future__ import annotations

import json
import os
import random
import re
import urllib.request
from collections.abc import Mapping
from typing import Any

API_URL_VARIABLE = "ROBINAUTS_API_URL"
API_TOKEN_VARIABLE = "ROBINAUTS_API_TOKEN"

GAME_PROMPT = (
    "Let's play a guessing game. Your secret number is {number}. Keep it secret: do not write"
    " it until the game is over. Another player will try to guess"
    ' it. Each of my next messages carries their reply, after "here is the guess:". Answer'
    " each one with the verdict alone: HIT if the guess is your number, TOO LOW if it is lower"
    " than your number, TOO HIGH if it is higher. Once they hit it, the game is over. Reply"
    " READY when you are set."
)
"""What opens the game's conversation: the secret number, and how to judge the guesses."""

GUESSING_PROMPT = "guess wich number I'm thinking  between 1 and 10 inclusive"
"""What opens the guesser's conversation, as it was asked for."""

GUESS_PREFIX = "here is the guess: "
"""What a guess is wrapped in before it reaches the game."""

HIT = re.compile(r"\bHIT\b", re.IGNORECASE)
"""How a verdict that ends the game is told apart from TOO LOW and TOO HIGH."""

RESULT = "RESULT"
"""What the line an answer ends with starts with, before its JSON."""

PULL_REQUEST = re.compile(r"https://[^/\s]+/[^/\s]+/[^/\s]+/pull/[0-9]+")
"""A pull request's URL, on GitHub or a GitHub Enterprise Server."""

VERDICTS = frozenset({"approve", "request_changes"})
"""What a review may conclude."""

IMPLEMENT_PROMPT = """\
Work on the repository {url}, starting from its branch {base}.

The task:

{task}

Do it from start to finish, in your workspace:

1. Clone it: `python -m robinauts_worker_common.clone {url} repo --branch {base}`.
2. Make a branch of your own from {base}, named `robinauts/<a few words of the task>`, and do
   the task there. Run the project's own checks for what you changed, and fix what fails.
3. Commit, push the branch, and open a pull request against {base}, with a title and a
   description that say what changed and why. Use the GitHub token in `GITHUB_TOKEN`: as git's
   basic authentication, user `x-access-token`, for the push; as a bearer for GitHub's REST
   API, which opens the pull request.

End your answer with exactly one line, and nothing after it:

{result} {{"pr_url": "<the pull request's URL>", "branch": "<the branch you pushed>"}}
"""
"""What opens the implementing conversation."""

REVIEW_PROMPT = """\
Review the pull request {pr_url}, which was opened to do this task on {url}:

{task}

Clone the repository (`python -m robinauts_worker_common.clone {url} repo`), check out the pull
request's branch, {branch}, and read its changes against {base}. Judge whether it does the
task, whether it is correct, and whether it is safe to merge. Change nothing, push nothing,
and post nothing.

End your answer with exactly one line, and nothing after it:

{result} {{"verdict": "approve" or "request_changes", "summary": "<two or three sentences>"}}
"""
"""What opens the reviewing conversation."""

TIMEOUT_SECONDS = 30.0
"""Per read: a turn's stream says something at least every few seconds while it runs."""


def api_headers(environ: Mapping[str, str]) -> dict[str, str]:
    """What every request carries: the API token in ``ROBINAUTS_API_TOKEN`` as a bearer when the
    worker was given one, and nothing when it was not, as in the local development mode."""
    token = environ.get(API_TOKEN_VARIABLE)
    return {"Authorization": f"Bearer {token}"} if token else {}


def _request(path: str, body: dict[str, Any] | None = None) -> urllib.request.Request:
    url = os.environ[API_URL_VARIABLE].rstrip("/") + path
    headers = api_headers(os.environ)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    return urllib.request.Request(url, data=data, headers=headers)


def _get(path: str) -> Any:
    with urllib.request.urlopen(_request(path), timeout=TIMEOUT_SECONDS) as answer:
        return json.load(answer)


def split_repo_branch(value: str) -> tuple[str, str]:
    """A repository and its branch, as a form's list of values gives them: ``<url>@<branch>``.
    A URL may hold an ``@`` of its own (``git@host:...``); a branch name may not."""
    url, at, branch = value.rpartition("@")
    if not at or not url or not branch:
        raise RuntimeError(f"{value!r} is not a repository and a branch, <url>@<branch>")
    return url, branch


def result_of(answer: str) -> dict[str, Any]:
    """The JSON object of the last line of ``answer`` that starts with ``RESULT``."""
    for line in reversed(answer.splitlines()):
        stripped = line.strip().strip("`").strip()
        if not stripped.startswith(RESULT):
            continue
        try:
            found = json.loads(stripped.removeprefix(RESULT).strip())
        except ValueError:
            break
        if isinstance(found, dict):
            return found
        break
    raise RuntimeError(f"the answer ends with no {RESULT} line of JSON: {answer[-300:]!r}")


def _first_agent() -> str:
    """The first agent the server offers: the one the workflow talks to, until it names one."""
    agents = _get("/api/agents")["items"]
    if not agents:
        raise RuntimeError("the Robinauts server offers no agent")
    return str(agents[0]["id"])


def model_named(models: list[dict[str, str]], name: str) -> str:
    """The id of the first model whose id or title holds ``name``, in any case."""
    for model in models:
        if name.lower() in model["id"].lower() or name.lower() in model["title"].lower():
            return model["id"]
    offered = ", ".join(m["id"] for m in models) or "none"
    raise RuntimeError(f"the Robinauts server offers no {name} model (it offers: {offered})")


def _run_turn(path: str, body: dict[str, Any]) -> str:
    """Start a turn, wait for its end by reading its stream to the last byte, and return the
    id of its conversation."""
    with urllib.request.urlopen(_request(path, body), timeout=TIMEOUT_SECONDS) as answer:
        conversation = str(answer.headers["X-Robinauts-Conversation-Id"])
        while answer.read(65536):
            pass
    return conversation


def answer_of(opened: dict[str, Any]) -> tuple[str, str]:
    """The id and the text of a conversation's last message, which must be an answer that
    did not fail."""
    messages = opened["messages"]
    last = messages[-1] if messages else None
    if last is None or last["role"] != "assistant" or last.get("failed"):
        ended = opened.get("ended_badly") or {}
        raise RuntimeError(
            f"conversation {opened['conversation']['id']} has no answer to carry: its last turn"
            f" ended {ended.get('state', 'without one')}"
        )
    text = "".join(part["text"] for part in last["parts"] if part["kind"] == "text")
    return str(last["id"]), text


def _latest_answer(conversation: str) -> tuple[str, str]:
    return answer_of(_get(f"/api/conversations/{conversation}"))


def _say(conversation: str, text: str) -> str:
    """Send ``text`` after the conversation's last answer, wait for the reply, and return it."""
    parent, _ = _latest_answer(conversation)
    _run_turn(f"/api/conversations/{conversation}/turns", {"text": text, "parent_id": parent})
    return _latest_answer(conversation)[1]


def _open(text: str, *, model: str | None = None, agent: str | None = None) -> dict[str, str]:
    """Start a conversation with ``agent``, else the first agent, and ``text``, and wait for its
    first answer."""
    agent = agent or _first_agent()
    body: dict[str, Any] = {"agent_id": agent, "text": text}
    if model is not None:
        body["model_id"] = model
    conversation = _run_turn("/api/turns", body)
    return {
        "agent_id": agent,
        "conversation_id": conversation,
        "answer": _latest_answer(conversation)[1],
    }


def pick_number() -> int:
    """The secret number of one game, between 1 and 10 inclusive."""
    return random.randint(1, 10)


def game_prompt(number: int) -> str:
    """What opens the game's conversation, with its secret number."""
    return GAME_PROMPT.format(number=number)


def open_game(model: str, number: int) -> dict[str, str]:
    """The game's conversation, on the first model whose id or title holds ``model``: told
    the secret number, and how to judge the guesses."""
    return _open(game_prompt(number), model=model_named(_get("/api/models")["items"], model))


def open_guesser() -> dict[str, str]:
    """The guesser's conversation, asked to guess: its first answer is the first guess."""
    return _open(GUESSING_PROMPT)


def judge_guess(game: dict[str, str], guesser: dict[str, str]) -> dict[str, Any]:
    """Carry the guesser's latest answer to the game, after ``GUESS_PREFIX``, and return the
    guess, the game's verdict, and whether it is a hit."""
    _, guess = _latest_answer(guesser["conversation_id"])
    verdict = _say(game["conversation_id"], GUESS_PREFIX + guess)
    return {"guess": guess, "verdict": verdict, "hit": is_hit(verdict)}


def is_hit(verdict: str) -> bool:
    """Whether the game's verdict ends it."""
    return bool(HIT.search(verdict))


def game_over(judged: list[dict[str, Any]], loop_count: int, max_tries: int) -> bool:
    """The loop's exit condition: the last guess hit, or it was the last try."""
    return bool(judged[-1]["hit"]) or loop_count >= max_tries


def relay_verdict(
    guesser: dict[str, str], judged: list[dict[str, Any]], over: list[bool]
) -> str | None:
    """Carry the game's verdict to the guesser as it is, and return its next guess; nothing,
    and send nothing, once the game is over."""
    if over[-1]:
        return None
    return _say(guesser["conversation_id"], judged[-1]["verdict"])


def summarize(number: int, judged: list[dict[str, Any]]) -> dict[str, Any]:
    """What the run returns: the secret number, whether the guesser won, in how many tries,
    and every round."""
    return {
        "number": number,
        "won": bool(judged[-1]["hit"]),
        "tries": len(judged),
        "rounds": judged,
    }


def implement(task: str, repo_branch: str, model: str, agent: str) -> dict[str, Any]:
    """Have ``agent``, on ``model``, do ``task`` on the repository and open a pull request
    against the branch; return the pull request, its branch and the conversation."""
    url, base = split_repo_branch(repo_branch)
    prompt = IMPLEMENT_PROMPT.format(url=url, base=base, task=task, result=RESULT)
    opened = _open(prompt, model=model, agent=agent)
    found = result_of(opened["answer"])
    pr_url = found.get("pr_url")
    if not isinstance(pr_url, str) or not PULL_REQUEST.fullmatch(pr_url):
        raise RuntimeError(
            f"conversation {opened['conversation_id']} names no pull request: {found!r}"
        )
    branch = found.get("branch")
    return {
        "task": task,
        "repository": url,
        "base": base,
        "pr_url": pr_url,
        "branch": branch if isinstance(branch, str) else "",
        "conversation_id": opened["conversation_id"],
    }


def review(implemented: dict[str, Any], model: str, agent: str) -> dict[str, Any]:
    """Have ``agent``, on ``model``, review the pull request ``implement`` opened, against the
    task; return its verdict and summary, with both conversations."""
    prompt = REVIEW_PROMPT.format(
        pr_url=implemented["pr_url"],
        url=implemented["repository"],
        task=implemented["task"],
        branch=implemented["branch"] or "the pull request's head",
        base=implemented["base"],
        result=RESULT,
    )
    opened = _open(prompt, model=model, agent=agent)
    found = result_of(opened["answer"])
    verdict = found.get("verdict")
    if verdict not in VERDICTS:
        raise RuntimeError(f"conversation {opened['conversation_id']} gives no verdict: {found!r}")
    summary = found.get("summary")
    return {
        "pr_url": implemented["pr_url"],
        "verdict": verdict,
        "summary": summary if isinstance(summary, str) else "",
        "implemented_in": implemented["conversation_id"],
        "reviewed_in": opened["conversation_id"],
    }

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

neorc delivers a task at least once: a task that runs again sends its message again.
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


def _open(text: str, *, model: str | None = None) -> dict[str, str]:
    """Start a conversation with the first agent and ``text``, and wait for its first answer."""
    agent = _first_agent()
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

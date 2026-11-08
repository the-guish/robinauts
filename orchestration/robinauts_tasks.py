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
"""

from __future__ import annotations

import json
import os
import urllib.request
from collections.abc import Mapping
from typing import Any

API_URL_VARIABLE = "ROBINAUTS_API_URL"
API_TOKEN_VARIABLE = "ROBINAUTS_API_TOKEN"

GUESSING_PROMPT = "guess wich number I'm thinking  between 1 and 10 inclusive"
"""The fixed prompt the guessing workflow opens with, as it was asked for."""

TIMEOUT_SECONDS = 30.0


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


def _first_agent() -> str:
    """The first agent the server offers: the one the workflow talks to, until it names one."""
    with urllib.request.urlopen(_request("/api/agents"), timeout=TIMEOUT_SECONDS) as answer:
        agents = json.load(answer)["items"]
    if not agents:
        raise RuntimeError("the Robinauts server offers no agent")
    return str(agents[0]["id"])


def start_guessing_session() -> dict[str, str]:
    """Start a new conversation with ``GUESSING_PROMPT``, and return its id and its turn's.

    The turn runs on the server whether or not anybody watches it: the stream is closed as
    soon as the server has said which conversation and which turn it started."""
    agent = _first_agent()
    started = _request("/api/turns", {"agent_id": agent, "text": GUESSING_PROMPT})
    with urllib.request.urlopen(started, timeout=TIMEOUT_SECONDS) as answer:
        return {
            "agent_id": agent,
            "conversation_id": answer.headers["X-Robinauts-Conversation-Id"],
            "run_id": answer.headers["X-Robinauts-Run-Id"],
        }

# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""neorc's handlers for the flows in ``flows/``: plain functions that reach the Robinauts API.

They run in the neorc worker ``robinauts start`` starts beside itself
(``docs/architecture/orchestration.md``), which hands it the server's URL in
``ROBINAUTS_API_URL`` and the secret it minted for the orchestrator in
``ROBINAUTS_API_TOKEN``. They import neither neorc nor Robinauts, and nothing but the standard
library, so that the worker needs nothing installed beside neorc itself.
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

API_URL_VARIABLE = "ROBINAUTS_API_URL"
API_TOKEN_VARIABLE = "ROBINAUTS_API_TOKEN"

GUESSING_PROMPT = "guess wich number I'm thinking  between 1 and 10 inclusive"
"""The fixed prompt the guessing workflow opens with, as it was asked for."""

TIMEOUT_SECONDS = 30.0


def _request(path: str, body: dict[str, Any] | None = None) -> urllib.request.Request:
    url = os.environ[API_URL_VARIABLE].rstrip("/") + path
    headers = {"Authorization": f"Bearer {os.environ[API_TOKEN_VARIABLE]}"}
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

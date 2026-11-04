# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""``search_sessions``: the earlier sessions whose messages hold every word asked.

Only what the person and the agent wrote is searched: not reasoning, and not tool calls or their
results, which are large and are where text from outside the session comes in. The
session the turn is in is left out, since the model has it already.

The search reads the sessions, newest first, up to ``MAX_SESSIONS`` of them. That bounds
what one call costs; an index is for when that is not enough.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from robinauts.agent_engines.contract.domain import FunctionTool, FunctionToolError
from robinauts.controller.contract.domain import Message, Role, Session, TextPart
from robinauts.tools.contract.ports import TurnScope

NAME = "search_sessions"

DESCRIPTION = (
    "Search the user's earlier sessions (conversations) with you, this same assistant, for "
    "messages that contain every word of the query, case aside. Only the user's own sessions "
    "with this assistant are searched, and not the current one. Returns the most recently "
    "updated sessions that match, each with its title and short excerpts of the matching "
    "messages."
)

MAX_QUERY = 200
"""Characters: the longest query taken."""

MAX_RESULTS = 10
DEFAULT_RESULTS = 5

MATCHES_PER_SESSION = 3
"""How many matching messages of one session are quoted."""

MAX_SESSIONS = 100
"""How many of the newest sessions one call reads at most."""

PAGE = 20

EXCERPT = 240
"""Characters: how long an excerpt is, around the first word found."""

INPUT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "The words to look for; a message matches when it has all of them.",
            "maxLength": MAX_QUERY,
        },
        "limit": {
            "type": "integer",
            "description": (f"How many sessions to return at most, {DEFAULT_RESULTS} if left out."),
            "minimum": 1,
            "maximum": MAX_RESULTS,
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}

_SPACE = re.compile(r"\s+")


def search_tool(scope: TurnScope) -> FunctionTool:
    async def call(arguments: Mapping[str, Any]) -> str:
        query, limit = _read(arguments)
        return json.dumps(await search(scope, query, limit), ensure_ascii=False)

    return FunctionTool(NAME, DESCRIPTION, INPUT_SCHEMA, call)


def _read(arguments: Mapping[str, Any]) -> tuple[str, int]:
    """The arguments, checked here: the frameworks hand them over as the model sent them."""
    if unknown := sorted(set(arguments) - {"query", "limit"}):
        raise FunctionToolError(f"Unknown argument(s): {', '.join(unknown)}.")
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        raise FunctionToolError("`query` must be a string with at least one word.")
    if len(query) > MAX_QUERY:
        raise FunctionToolError(f"`query` must be at most {MAX_QUERY} characters.")
    limit = arguments.get("limit", DEFAULT_RESULTS)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RESULTS:
        raise FunctionToolError(f"`limit` must be a whole number from 1 to {MAX_RESULTS}.")
    return query, limit


@dataclass(frozen=True, slots=True)
class _Match:
    role: str
    created_at: str
    excerpt: str


async def search(scope: TurnScope, query: str, limit: int) -> dict[str, Any]:
    words = query.casefold().split()
    found: list[dict[str, Any]] = []
    read = 0
    after: Session | None = None
    while len(found) < limit and read < MAX_SESSIONS:
        page = await scope.data.sessions(min(PAGE, MAX_SESSIONS - read), after)
        if not page:
            break
        after = page[-1]
        read += len(page)
        for session in page:
            if session.id == scope.session_id:
                continue
            matches = [
                match
                for message in await scope.data.messages(session.id)
                if (match := _match(message, words)) is not None
            ]
            if matches or _holds(session.title, words):
                found.append(_found(session, matches[:MATCHES_PER_SESSION]))
                if len(found) == limit:
                    break
    return {"query": query, "sessions_read": read, "sessions": found}


def _match(message: Message, words: list[str]) -> _Match | None:
    if message.role not in (Role.USER, Role.ASSISTANT):
        return None
    text = _SPACE.sub(" ", " ".join(p.text for p in message.parts if isinstance(p, TextPart)))
    if not _holds(text, words):
        return None
    return _Match(message.role.value, message.created_at.isoformat(), _excerpt(text, words))


def _holds(text: str, words: list[str]) -> bool:
    folded = text.casefold()
    return all(word in folded for word in words)


def _excerpt(text: str, words: list[str]) -> str:
    """Up to ``EXCERPT`` characters of the text, around the first of the words in it."""
    if len(text) <= EXCERPT:
        return text
    first = min(text.casefold().find(word) for word in words)
    start = max(0, min(first - EXCERPT // 3, len(text) - EXCERPT))
    end = start + EXCERPT
    return ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")


def _found(session: Session, matches: list[_Match]) -> dict[str, Any]:
    return {
        "session_id": str(session.id),
        "title": session.title,
        "updated_at": session.updated_at.isoformat(),
        "matches": [
            {"role": m.role, "created_at": m.created_at, "excerpt": m.excerpt} for m in matches
        ],
    }

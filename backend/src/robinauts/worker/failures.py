# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What the model is told about answers that did not finish, when the conversation goes on.

The engine remembers nothing of a turn that failed or that somebody stopped: the next turn
continues from the last answer that finished. So the prompt carries what it missed
(docs/specs/ui.md): every question since then whose answer failed or was stopped, with what
that answer did, best-effort; then the new question; and, on a retry, that the person pressed
retry.

The note goes in the user's message, so what it quotes from the unfinished answer -- tool
output, which comes from arbitrary MCP servers, and the model's own text -- is quoted as a
JSON string and labelled. Escaped, it cannot start a line of its own and fake the note's
framing ("The user's new message:") with the user's authority.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from robinauts.shared_domain.domain import (
    Message,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)

LONGEST = 500
"""How much of a tool result, or of the text written, the note quotes."""


def prompt_after_failures(
    question: str,
    earlier: Sequence[tuple[str, Message]] = (),
    retried: Message | None = None,
) -> str:
    """The prompt for ``question``: after the unfinished exchanges ``earlier`` (each a question
    and its failed or stopped answer, oldest first), and with a note on ``retried`` if it is a
    retry."""
    lines = []
    if earlier:
        lines += ["Earlier messages in this conversation got no finished answer:"]
        for asked, unfinished in earlier:
            lines += ["", f"Message: {asked}"]
            if unfinished.stopped:
                lines += ["The user stopped your answer to it before it finished."]
                lines += _what_it_did(unfinished)
            else:
                lines += ["Your answer to it failed before it finished."]
                lines += [*_what_it_did(unfinished), "The user was told it failed."]
        lines += ["", "---", "The user's new message:", ""]
    lines.append(question)
    if retried is not None:
        lines += ["", "---", "Your previous answer to this message failed before it finished."]
        lines += [*_what_it_did(retried)]
        lines.append("The user was told it failed, and pressed the retry button.")
    return "\n".join(lines)


def _what_it_did(unfinished: Message) -> list[str]:
    results = {p.call_id: p for p in unfinished.parts if isinstance(p, ToolResultPart)}
    calls = [p for p in unfinished.parts if isinstance(p, ToolCallPart)]
    before = "Before it was stopped" if unfinished.stopped else "Before it failed"
    lines = [f"{before}, it made these tool calls:"] if calls else []
    for call in calls:
        result = results.get(call.call_id)
        if result is None:
            outcome = "no result"
        else:
            said = "error" if result.is_error else "result"
            outcome = f"{said}, tool output: {_quoted(result.text)}"
        lines.append(f"- {call.name}({json.dumps(dict(call.arguments))}) -> {outcome}")
    written = "".join(p.text for p in unfinished.parts if isinstance(p, TextPart))
    if written:
        lines.append(f"It had written: {_quoted(written)}")
    return lines


def _quoted(text: str) -> str:
    """Cut to ``LONGEST``, then a JSON string: its newlines and quotes are escaped."""
    return json.dumps(text[:LONGEST], ensure_ascii=False)

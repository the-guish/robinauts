# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Models that answer with the last thing the user said."""

from __future__ import annotations

import threading
from typing import Any

from util.fake_openai.server import CallTool


def text_of(message: dict[str, Any]) -> str:
    """A message's text, whether its content is a string or a list of parts."""
    content = message["content"]
    if isinstance(content, str):
        return content
    return "".join(part.get("text", "") for part in content)


def last_question(messages: list[dict[str, Any]]) -> str:
    return text_of(next(m for m in reversed(messages) if m["role"] == "user"))


LANGCHAIN_SUMMARY = "<role>\nContext Extraction Assistant"
"""How LangChain's summarisation prompt begins, the last message of the request for a summary."""

KATALOOP_SUMMARY = "The user's message is a conversation between a user and you, the assistant."
"""The first sentence of kataloop's summary prompt, the system prompt of its summary call."""


def asks_for_a_summary(messages: list[dict[str, Any]]) -> bool:
    """Whether the request is an engine asking for a summary of the older conversation."""
    if LANGCHAIN_SUMMARY in text_of(messages[-1]):
        return True
    first = messages[0]
    return first["role"] == "system" and text_of(first).startswith(KATALOOP_SUMMARY)


class EchoModel:
    def reply(self, messages: list[dict[str, Any]]) -> str:
        return last_question(messages)


class HoldingEchoModel(EchoModel):
    """An echo that holds the question ``hold`` until ``release`` is set."""

    def __init__(self) -> None:
        self.asked = threading.Event()
        self.release = threading.Event()

    def reply(self, messages: list[dict[str, Any]]) -> str:
        if last_question(messages) == "hold":
            self.asked.set()
            self.release.wait(30)
        return super().reply(messages)


class PoisonEchoModel(EchoModel):
    """An echo that fails a message starting with ``poison``, as the echo engine does.

    A retry is sent with the question first, so it fails again; a later message is sent after a
    note on the failed one, so it does not.
    """

    def reply(self, messages: list[dict[str, Any]]) -> str:
        text = super().reply(messages)
        if text.startswith("poison"):
            raise RuntimeError("the message is poisoned")
        return text


class ToolEchoModel:
    """The echo engine's turn, through a real tool: call ``tool`` with the question as ``text``,
    then answer "The tool said: <its result>".

    A question starting with ``poison`` fails after the tool round, when the model is sent the
    result: a turn that failed with a tool call made.
    """

    def __init__(self, tool: str) -> None:
        self.tool = tool

    def reply(self, messages: list[dict[str, Any]]) -> str | CallTool:
        question = last_question(messages)
        if messages[-1]["role"] != "tool":
            return CallTool(self.tool, {"text": question})
        if question.startswith("poison"):
            raise RuntimeError("the message is poisoned")
        return f"The tool said: {text_of(messages[-1])}"

# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Which thinking goes back to an Anthropic-protocol model: the signed blocks alone.

Pydantic AI sends a thinking part back as a thinking block only when Anthropic signed it. It
sends any other as text in ``<thinking>`` tags. A gateway serving another vendor, such as
OpenRouter serving Gemini, answers with an unsigned summary. Sent back as text, it shows the
model its own reasoning written as answers, and the model then writes its reasoning into its
answers. The signed and redacted blocks still go back, and they carry the model's real state.
"""

from __future__ import annotations

import dataclasses

from pydantic_ai.messages import ModelMessage, ModelResponse, ThinkingPart

ANTHROPIC = "anthropic"
"""The provider name Pydantic AI gives the parts of an Anthropic-protocol answer."""


def signed_thinking_only(messages: list[ModelMessage]) -> list[ModelMessage]:
    """The history without the thinking parts Pydantic AI would send back as text."""
    return [_without_unsigned(m) for m in messages]


def _without_unsigned(message: ModelMessage) -> ModelMessage:
    if not isinstance(message, ModelResponse):
        return message
    parts = [p for p in message.parts if not _unsigned(p)]
    if len(parts) == len(message.parts):
        return message
    return dataclasses.replace(message, parts=parts)


def _unsigned(part: object) -> bool:
    return isinstance(part, ThinkingPart) and not (
        part.provider_name == ANTHROPIC and part.signature
    )

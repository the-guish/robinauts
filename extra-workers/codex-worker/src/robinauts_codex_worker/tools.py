# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The tools run in this process, such as the search of a person's conversations, as Codex's
dynamic tools: declared on the thread, and called back over the app server's own channel, an
``item/tool/call`` request this worker answers. The configured tool servers are Codex's own MCP
servers (config.py)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from robinauts.agent_engines.contract.domain import FunctionTool, FunctionToolError


def specs(functions: Sequence[FunctionTool]) -> list[dict[str, Any]]:
    """The ``dynamicTools`` of a thread for these tools."""
    return [
        {
            "type": "function",
            "name": function.name,
            "description": function.description,
            "inputSchema": dict(function.input_schema),
        }
        for function in functions
    ]


def _answer(text: str, success: bool) -> dict[str, Any]:
    return {"contentItems": [{"type": "inputText", "text": text}], "success": success}


async def call(functions: Sequence[FunctionTool], params: Mapping[str, Any]) -> dict[str, Any]:
    """The answer to an ``item/tool/call``: the tool's output, or a failure the model reads. A
    failure the tool means for the model is its message; anything else its kind alone."""
    name = params.get("tool")
    function = next((f for f in functions if f.name == name), None)
    if function is None:
        return _answer(f"no tool {name!r} in this turn", False)
    arguments = params.get("arguments")
    try:
        text = await function.call(dict(arguments) if isinstance(arguments, Mapping) else {})
    except FunctionToolError as failed:
        return _answer(str(failed), False)
    except Exception as error:
        return _answer(type(error).__name__, False)
    return _answer(text, True)

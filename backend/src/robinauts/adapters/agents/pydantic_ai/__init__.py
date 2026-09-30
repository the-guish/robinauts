# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The Pydantic AI engine, and the only place Pydantic AI is named.

One of the two agent adapters (``docs/layout.md``). Nothing else in the
platform imports the framework, the two adapters do not import each other, and
both are held to one contract suite (``backend/tests/contracts/agents.py``).

Importing this imports Pydantic AI, so ``robinauts.adapters`` deliberately does
not: the composition root names this sub-package in the one line that builds
the engine, which is the line the discard test says must be the only casualty
of deleting it.
"""

from robinauts.adapters.agents.pydantic_ai.engine import (
    ANTHROPIC_ENDPOINT,
    ANTHROPIC_KEY_HEADER,
    CHARS_PER_TOKEN,
    CLIENT_VARIABLES_REMOVED,
    DEFAULT_ANTHROPIC_OUTPUT_TOKENS,
    DEFAULT_CONTEXT_WINDOW,
    MAX_RETRIES,
    MAX_TOOL_ROUNDS,
    QUIET_CLIENT_LEVEL,
    QUIET_CLIENT_LOGGERS,
    REQUEST_LIMIT,
    TRACING_VARIABLES_REMOVED,
    TRIM_AT,
    ChatModelFactory,
    HistoryProcessor,
    PydanticAIAgent,
    ToolsetsFactory,
    chat_model,
    clear_client_overrides,
    context_window,
    endpoint_of,
    force_tracing_off,
    mcp_toolset,
    mcp_toolsets,
    quiet_client_logging,
    read_state,
    runner_for,
    settings,
    within,
    write_state,
)

__all__ = [
    "ANTHROPIC_ENDPOINT",
    "ANTHROPIC_KEY_HEADER",
    "CHARS_PER_TOKEN",
    "CLIENT_VARIABLES_REMOVED",
    "DEFAULT_ANTHROPIC_OUTPUT_TOKENS",
    "DEFAULT_CONTEXT_WINDOW",
    "MAX_RETRIES",
    "MAX_TOOL_ROUNDS",
    "QUIET_CLIENT_LEVEL",
    "QUIET_CLIENT_LOGGERS",
    "REQUEST_LIMIT",
    "TRACING_VARIABLES_REMOVED",
    "TRIM_AT",
    "ChatModelFactory",
    "HistoryProcessor",
    "PydanticAIAgent",
    "ToolsetsFactory",
    "chat_model",
    "clear_client_overrides",
    "context_window",
    "endpoint_of",
    "force_tracing_off",
    "mcp_toolset",
    "mcp_toolsets",
    "quiet_client_logging",
    "read_state",
    "runner_for",
    "settings",
    "within",
    "write_state",
]

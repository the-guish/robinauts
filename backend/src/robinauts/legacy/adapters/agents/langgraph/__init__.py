# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The LangGraph engine, and the only place LangGraph and LangChain are named.

One of the two agent adapters (``docs/layout.md``). Nothing else in the
platform imports the framework, the two adapters do not import each other, and
both are held to one contract suite (``backend/tests/contracts/agents.py``).

Importing this imports LangGraph, so ``robinauts.legacy.adapters`` deliberately does
not: the composition root names this sub-package in the one line that builds
the engine, which is the line the discard test says must be the only casualty
of deleting it.
"""

from robinauts.legacy.adapters.agents.langgraph.engine import (
    ANTHROPIC_ENDPOINT,
    ANTHROPIC_KEY_HEADER,
    ANTHROPIC_KINDS,
    CEILING_FIELDS,
    CLIENT_VARIABLES_REMOVED,
    DEFAULT_ANTHROPIC_OUTPUT_TOKENS,
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_OPENAI_OUTPUT_TOKENS,
    KEEP_MESSAGES,
    MAX_RETRIES,
    MAX_TOOL_ROUNDS,
    MODEL_NODE,
    OPENAI_ENDPOINT,
    OPENAI_KEY_HEADER,
    OPENAI_KINDS,
    OUTPUT_VERSION,
    QUIET_CLIENT_LEVEL,
    QUIET_CLIENT_LOGGERS,
    RECURSION_LIMIT,
    STEPS_PER_ROUND,
    SUMMARIZE_AT,
    TOOLS_NODE,
    TRACING_VARIABLES_REMOVED,
    ChatModelFactory,
    LangGraphAgent,
    ToolsFactory,
    chat_model,
    clear_client_overrides,
    context_window,
    endpoint_of,
    force_tracing_off,
    mcp_connection,
    mcp_tools,
    middleware,
    quiet_client_logging,
    read_state,
    signed_blocks_only,
    write_state,
)

__all__ = [
    "ANTHROPIC_ENDPOINT",
    "ANTHROPIC_KEY_HEADER",
    "ANTHROPIC_KINDS",
    "CEILING_FIELDS",
    "CLIENT_VARIABLES_REMOVED",
    "DEFAULT_ANTHROPIC_OUTPUT_TOKENS",
    "DEFAULT_CONTEXT_WINDOW",
    "DEFAULT_OPENAI_OUTPUT_TOKENS",
    "KEEP_MESSAGES",
    "MAX_RETRIES",
    "MAX_TOOL_ROUNDS",
    "MODEL_NODE",
    "OPENAI_ENDPOINT",
    "OPENAI_KEY_HEADER",
    "OPENAI_KINDS",
    "OUTPUT_VERSION",
    "QUIET_CLIENT_LEVEL",
    "QUIET_CLIENT_LOGGERS",
    "RECURSION_LIMIT",
    "STEPS_PER_ROUND",
    "SUMMARIZE_AT",
    "TOOLS_NODE",
    "TRACING_VARIABLES_REMOVED",
    "ChatModelFactory",
    "LangGraphAgent",
    "ToolsFactory",
    "chat_model",
    "clear_client_overrides",
    "context_window",
    "endpoint_of",
    "force_tracing_off",
    "mcp_connection",
    "mcp_tools",
    "middleware",
    "quiet_client_logging",
    "read_state",
    "signed_blocks_only",
    "write_state",
]

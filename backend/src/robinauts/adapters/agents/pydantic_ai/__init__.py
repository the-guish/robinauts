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
    BLOCK_NOT_REPLAYED,
    BLOCKS_LEFT_OUT,
    CLIENT_VARIABLES_REMOVED,
    DEFAULT_ANTHROPIC_OUTPUT_TOKENS,
    MAX_RETRIES,
    QUIET_CLIENT_LEVEL,
    QUIET_CLIENT_LOGGERS,
    REDACTED,
    TRACING_VARIABLES_REMOVED,
    VENDOR,
    ChatModelFactory,
    PydanticAIAgent,
    chat_model,
    clear_client_overrides,
    endpoint_of,
    force_tracing_off,
    quiet_client_logging,
)

__all__ = [
    "ANTHROPIC_ENDPOINT",
    "ANTHROPIC_KEY_HEADER",
    "BLOCKS_LEFT_OUT",
    "BLOCK_NOT_REPLAYED",
    "CLIENT_VARIABLES_REMOVED",
    "DEFAULT_ANTHROPIC_OUTPUT_TOKENS",
    "MAX_RETRIES",
    "QUIET_CLIENT_LEVEL",
    "QUIET_CLIENT_LOGGERS",
    "REDACTED",
    "TRACING_VARIABLES_REMOVED",
    "VENDOR",
    "ChatModelFactory",
    "PydanticAIAgent",
    "chat_model",
    "clear_client_overrides",
    "endpoint_of",
    "force_tracing_off",
    "quiet_client_logging",
]

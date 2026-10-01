# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The outside world: what the platform talks to, and what it reads.

Every implementation of a port that is not owned state lives here -- the
identity provider over HTTP, the machine's clock, the operating system's
randomness, the configuration file and the environment, and the two seams a
run in the background goes through: the executor that carries its work and
the signals its watchers wait on. The stores are next door in
``robinauts.legacy.datastore``, which is the same kind of thing for the one database
the deployment owns.

It depends on ``robinauts.legacy.ports`` and ``robinauts.legacy.domain`` and on nothing else
inside the package (``docs/layout.md``). In particular **not on ``core``**: an
adapter that validated something would be a second place where a rule lives,
and the rule in ``core`` would stop being the rule. So the reader returns raw
tables for ``core.parse_sign_in_config`` to judge, and the identity provider
returns raw mappings for the application to check.

Third-party code confined to this layer: ``httpx``, and the agent frameworks
under ``agents/``, each to its own sub-package. The contracts that enforce
that are in ``backend/pyproject.toml`` and are run by the test suite.
"""

from robinauts.legacy.adapters.clock import SystemClock
from robinauts.legacy.adapters.config_file import (
    ProviderKeys,
    SecretLookup,
    ToolServerSecrets,
    check_api_keys,
    check_client_secrets,
    check_tool_secrets,
    credential_header,
    environment,
    read_toml,
)
from robinauts.legacy.adapters.identity_provider import (
    CONNECT_TIMEOUT_SECONDS,
    DISCOVERY_PATH,
    MAX_RESPONSE_BYTES,
    READ_TIMEOUT_SECONDS,
    REQUEST_HEADERS,
    RETRYABLE_STATUSES,
    TOTAL_TIMEOUT_SECONDS,
    USER_AGENT,
    HttpIdentityProvider,
    open_client,
    ssl_context,
)
from robinauts.legacy.adapters.ids import OsIdSource
from robinauts.legacy.adapters.run_executor import (
    CLOSED,
    DEFAULT_SHUTDOWN_SECONDS,
    AsyncioRunExecutor,
)
from robinauts.legacy.adapters.run_signals import (
    REMEMBERED_RUNS,
    REMEMBERED_SECONDS,
    MemoryRunSignals,
)
from robinauts.legacy.adapters.secrets import SECRET_BYTES, OsSecretSource

__all__ = [
    "CLOSED",
    "CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_SHUTDOWN_SECONDS",
    "DISCOVERY_PATH",
    "MAX_RESPONSE_BYTES",
    "READ_TIMEOUT_SECONDS",
    "REMEMBERED_RUNS",
    "REMEMBERED_SECONDS",
    "REQUEST_HEADERS",
    "RETRYABLE_STATUSES",
    "SECRET_BYTES",
    "TOTAL_TIMEOUT_SECONDS",
    "USER_AGENT",
    "AsyncioRunExecutor",
    "HttpIdentityProvider",
    "MemoryRunSignals",
    "OsIdSource",
    "OsSecretSource",
    "ProviderKeys",
    "SecretLookup",
    "SystemClock",
    "ToolServerSecrets",
    "check_api_keys",
    "check_client_secrets",
    "check_tool_secrets",
    "credential_header",
    "environment",
    "open_client",
    "read_toml",
    "ssl_context",
]

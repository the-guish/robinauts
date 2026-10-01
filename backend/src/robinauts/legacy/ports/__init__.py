# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What the application needs from the outside world, as abstract base classes.

Depends on ``robinauts.legacy.domain`` and on nothing else inside the package
(``docs/layout.md``). The application is handed an implementation of each and
constructs none; the fakes under ``backend/tests/fakes/`` implement these same
classes, and the contract suites say what an implementation must do.
"""

from robinauts.legacy.ports.agents import Agent
from robinauts.legacy.ports.clock import Clock
from robinauts.legacy.ports.conversations import (
    MAX_PAGE,
    MAX_SWEPT,
    ConversationPage,
    ConversationStore,
    Document,
    Snapshot,
)
from robinauts.legacy.ports.credentials import CredentialStore
from robinauts.legacy.ports.identity_provider import IdentityProvider
from robinauts.legacy.ports.ids import IdSource
from robinauts.legacy.ports.run_executor import RunExecutor, RunReport, RunWork
from robinauts.legacy.ports.run_signals import RunSignals
from robinauts.legacy.ports.secrets import SECRET_BITS, SecretSource

__all__ = [
    "MAX_PAGE",
    "MAX_SWEPT",
    "SECRET_BITS",
    "Agent",
    "Clock",
    "ConversationPage",
    "ConversationStore",
    "CredentialStore",
    "Document",
    "IdSource",
    "IdentityProvider",
    "RunExecutor",
    "RunReport",
    "RunSignals",
    "RunWork",
    "SecretSource",
    "Snapshot",
]

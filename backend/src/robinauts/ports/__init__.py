# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What the application needs from the outside world, as abstract base classes.

Depends on ``robinauts.domain`` and on nothing else inside the package
(``docs/layout.md``). The application is handed an implementation of each and
constructs none; the fakes under ``backend/tests/fakes/`` implement these same
classes, and the contract suites say what an implementation must do.
"""

from robinauts.ports.agents import Agent
from robinauts.ports.clock import Clock
from robinauts.ports.conversations import (
    MAX_PAGE,
    MAX_SWEPT,
    ConversationPage,
    ConversationStore,
    Document,
    Snapshot,
)
from robinauts.ports.credentials import CredentialStore
from robinauts.ports.identity_provider import IdentityProvider
from robinauts.ports.ids import IdSource
from robinauts.ports.run_executor import RunExecutor, RunReport, RunWork
from robinauts.ports.run_signals import RunSignals
from robinauts.ports.secrets import SECRET_BITS, SecretSource

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

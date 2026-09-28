# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""In-memory implementations of every port, for the tests of the application.

They are the ports' own abstract base classes, implemented
(``docs/layout.md``, "Conventions"): a fake that drifted from a port would
fail to instantiate. The credential store and the conversation store pass
the same contract suites as the real ones will (``tests/contracts/``).
"""

from fakes.agents import Asked, Gate, Raise, ScriptedAgent, Step, calls, says
from fakes.clock import START, FakeClock
from fakes.conversations import MemoryConversationStore
from fakes.credentials import MemoryCredentialStore
from fakes.identity_provider import Answer, ScriptedIdentityProvider, discovery_for
from fakes.ids import CountingIdSource
from fakes.secrets import LENGTH, CountingSecretSource, StuntedSecretSource
from fakes.tool_servers import MemoryToolServers

__all__ = [
    "LENGTH",
    "START",
    "Answer",
    "Asked",
    "CountingIdSource",
    "CountingSecretSource",
    "FakeClock",
    "Gate",
    "MemoryConversationStore",
    "MemoryCredentialStore",
    "MemoryToolServers",
    "Raise",
    "ScriptedAgent",
    "ScriptedIdentityProvider",
    "Step",
    "StuntedSecretSource",
    "calls",
    "discovery_for",
    "says",
]

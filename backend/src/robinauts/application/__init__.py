# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Control flow and business rules: what the platform does, in order.

Depends on ``ports``, ``core`` and ``domain`` (``docs/layout.md``). It is
handed every port implementation and constructs none.

What it keeps in the process is only what ``docs/layout.md`` allows it to: a
``SignIn`` holds the discovery documents it has checked and the one fetch of
each that is in flight, the reading of the monotonic clock at which it last
swept expired rows, and a count of the sweeps that failed. A cache, a
scheduling hint and a diagnostic. Losing any of them costs one fetch, one
extra sweep, or a number nobody read; nothing a sign-in is decided by is in
them, and a second process may disagree about all three without a single
answer changing.

``MAX_PAGE`` is re-exported from here, and is the one thing in this list that
is not the application's own: it is the store's bound
(``ports.MAX_PAGE``), and ``api`` -- which puts it in the OpenAPI document, so
that a client can hold itself to it instead of finding out by being refused --
may not import ``ports`` (``docs/layout.md``). A door, not a second
definition.

The rules the flow applies are ``core``'s, and so is everything in it that is
input and output alone: the allow list, the claims, the URLs, what a secret
must be, and the authorization request itself.
"""

from robinauts.application.conversations import (
    DEFAULT_PAGE,
    MAX_PAGE,
    Conversations,
    OpenedConversation,
    owner_of,
)
from robinauts.application.local import LocalAccess
from robinauts.application.sign_in import (
    PENDING_LOGIN_LIFE,
    SWEEP_SECONDS,
    BegunSignIn,
    OpenedSession,
    ProviderEndpoints,
    SignIn,
)
from robinauts.application.turns import (
    DEFAULT_TURN_SECONDS,
    ENDING_BUDGET_SECONDS,
    NO_ANSWER,
    NO_CONTENT,
    TIMED_OUT,
    UNFINISHED_ANSWER,
    StartedTurn,
    Turns,
)
from robinauts.application.watch import (
    DEFAULT_QUIET_SECONDS,
    DEFAULT_WAIT_SECONDS,
    Watch,
)

__all__ = [
    "DEFAULT_PAGE",
    "DEFAULT_QUIET_SECONDS",
    "DEFAULT_TURN_SECONDS",
    "DEFAULT_WAIT_SECONDS",
    "ENDING_BUDGET_SECONDS",
    "MAX_PAGE",
    "NO_ANSWER",
    "NO_CONTENT",
    "PENDING_LOGIN_LIFE",
    "SWEEP_SECONDS",
    "TIMED_OUT",
    "UNFINISHED_ANSWER",
    "BegunSignIn",
    "Conversations",
    "LocalAccess",
    "OpenedConversation",
    "OpenedSession",
    "ProviderEndpoints",
    "SignIn",
    "StartedTurn",
    "Turns",
    "Watch",
    "owner_of",
]

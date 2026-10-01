# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What a route needs of whoever is asking: the permission levels.

``docs/specs/sign-in.md`` asks that every route declare the permission it
needs and that a test assert every route declares one. Roles are deferred
(``docs/working-notes/poc-scope.md``, "Out"), so there is nothing to map yet:
a route is reached by anybody, or by somebody signed in, and those are the two
values here.

They live in ``domain`` rather than in ``api`` because they are vocabulary,
not routing: when roles arrive, the permissions grow and the table that says
which role may do which goes in ``core``, where ``api`` cannot reach it and
would not want to. The declaration itself stays where the routes are
(``robinauts.legacy.api.access``).
"""

from __future__ import annotations

from enum import StrEnum


class Permission(StrEnum):
    """What a route asks of the person making the request."""

    PUBLIC = "public"
    """Anybody, signed in or not. The route holds nobody's data."""
    SIGNED_IN = "signed_in"
    """Somebody with a session. Whose data it is, is the application's question."""

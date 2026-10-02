# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A real OpenID Connect provider on a loopback port, for the tests.

Not a fake and not a stubbed transport: an HTTP server that accepts a
connection, parses a request and writes an answer, so that what is exercised
is the adapter's own HTTP -- its timeouts, its refusal to follow a redirect,
the bound on what it reads, and the form the token endpoint really receives.

Later steps reuse it: the auth routes (step 6) and the browser test sign in
through this same server, so its interface is meant to be lived with.
"""

from standin.provider import (
    Misbehaviour,
    Received,
    StandInProvider,
    redirect_from,
    unsigned_jwt,
)

__all__ = [
    "Misbehaviour",
    "Received",
    "StandInProvider",
    "redirect_from",
    "unsigned_jwt",
]

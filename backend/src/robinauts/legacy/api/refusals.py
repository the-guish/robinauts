# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What each route can refuse with, as the OpenAPI document says it.

One shape crosses for every refusal there is -- ``ErrorResponse``, naming the
class and one sentence (``robinauts.legacy.api.errors``) -- and this is where a route
says which statuses it can answer with it. It is **not** decoration: the
document is what the interface's typed client is generated from, so a status
that is not described is a status the client has no branch for, and -- since
FastAPI would otherwise describe the 422 of a route with parameters in a
shape of **its** own -- a body the client would fail to read.

Its own module because both route modules want it and neither is the other's:
``agent_routes`` is about the deployment's agents and models and
``conversation_routes`` about one person's conversations, and a shared table is
not a reason for one to import the other.
"""

from __future__ import annotations

from typing import Any

from robinauts.legacy.api.schemas import ErrorResponse

# Written as numbers, as ``errors.STATUS_OF`` is: these say what each route can
# refuse with, and the one status whose framework constant was renamed under us
# is not worth two spellings of.
NO_SESSION = 401
"""No session, or one that stands for nobody. Every route of the API needs one."""

NOT_OURS = 403
"""A request another site's page sent, refused before a route is reached.

On a **read** as well, and deliberately: in the local development mode the
request protection judges every request on the host it was addressed to (the
defence against DNS rebinding, ``robinauts.legacy.api.protection``), so a GET can be
refused there too.
"""

NOT_THERE = 404
"""Nothing of that id -- or nothing of that id **that is this person's**."""

STILL_ANSWERING = 409
"""A conversation with a run going, which is not deleted while it is."""

TOO_LARGE = 413
"""A body larger than this deployment reads (``robinauts.legacy.api.protection``).

Its own entry rather than the fallback: it is a client's mistake, it names a
bound a client can hold itself to, and a generated client that knew about it
can say so instead of retrying a request that cannot succeed.
"""

NOT_JSON = 415
"""A write sent as something other than JSON."""

UNREADABLE = 422
"""A path, a query or a body the platform does not take, named and not quoted."""

ANYTHING_ELSE = "any other refusal, in the same shape: 400, 405, 500"
"""The description of the ``default`` entry every route here carries.

A request the server itself could not read -- one whose framing or whose
nesting nothing got as far as a route with -- a method that is not served at a
path that is, and a mistake of ours, which says only that the request could
not be served. None of them is a route's own answer -- any route can be sent a
request that is not one, be asked with the wrong method, or meet a bug -- so
they are described **once**, as the fallback, rather than listed on every
route as though they were part of what it does.
"""


def refusals(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """Those statuses, each answering the one shape a refusal crosses in.

    Every table here also carries ``default`` (``ANYTHING_ELSE``), so a route
    describes everything it can answer and not only the part it decides.
    """
    # Not ``status``: that name is the framework's in the modules that use this.
    described: dict[int | str, dict[str, Any]] = {
        answered: {"model": ErrorResponse} for answered in statuses
    }
    described["default"] = {"model": ErrorResponse, "description": ANYTHING_ELSE}
    return described


READING = refusals(NO_SESSION, NOT_OURS, UNREADABLE)
"""What a read that names no id can refuse with."""

OPENING = refusals(NO_SESSION, NOT_OURS, NOT_THERE, UNREADABLE)
"""The same, for a read that names a conversation."""

WRITING = refusals(NO_SESSION, NOT_OURS, NOT_THERE, TOO_LARGE, NOT_JSON, UNREADABLE)
"""What a write can refuse with: the three the request protection answers
before a route is reached (``NOT_OURS``, ``TOO_LARGE``, ``NOT_JSON``) beside
the route's own."""

DELETING = refusals(
    NO_SESSION, NOT_OURS, NOT_THERE, STILL_ANSWERING, TOO_LARGE, NOT_JSON, UNREADABLE
)
"""A write, and the one refusal only deleting has: a conversation still
answering (``STILL_ANSWERING``)."""

LISTING_OFFERED = refusals(NO_SESSION, NOT_OURS)
"""A read that names nothing at all -- what the deployment offers, its agents
and its models: only who is asking can be wrong."""

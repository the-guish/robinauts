# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The two cookies a sign-in uses, and the attributes they carry.

A sign-in in progress is bound to the browser that began it by a cookie
holding its raw ``state``; a session is a cookie holding its secret. The store
keeps the SHA-256 of each and never the value, so these are the only two
places either exists.

Both are ``HttpOnly`` -- no script of ours reads them, and none of anybody
else's should -- ``Path=/`` and ``SameSite=Lax``. ``Lax`` rather than
``Strict``: the sign-in comes back as a top-level navigation from the
provider's own site, and ``Strict`` would drop the ``state`` cookie on exactly
that request, so no sign-in would ever complete.

On an ``https`` deployment both are ``Secure`` and carry the ``__Host-``
prefix, which a browser accepts only with ``Secure``, ``Path=/`` and **no**
``Domain``: a page on a sibling subdomain can then neither set nor overwrite
them. Over ``http``, which ``core`` allows for a loopback public URL alone,
the prefix would make the browser refuse the cookie outright, so the plain
name is used and every other server on the machine shares the origin. That is
the price of a development URL, and it is why ``http`` is loopback-only.
"""

from __future__ import annotations

import math

from fastapi import Response

HOST_PREFIX = "__Host-"
"""What a browser demands ``Secure``, ``Path=/`` and no ``Domain`` for."""

SESSION_COOKIE = "robinauts_session"
LOGIN_COOKIE = "robinauts_login"
"""The unprefixed names; ``cookie_name`` adds the prefix where it is allowed."""

SAME_SITE = "lax"


def cookie_name(name: str, *, secure: bool) -> str:
    """``name`` as this deployment spells it: prefixed on https, plain on http."""
    return f"{HOST_PREFIX}{name}" if secure else name


def session_cookie(*, secure: bool) -> str:
    """The name of the cookie holding a session's secret."""
    return cookie_name(SESSION_COOKIE, secure=secure)


def login_cookie(*, secure: bool) -> str:
    """The name of the cookie holding a sign-in's ``state``."""
    return cookie_name(LOGIN_COOKIE, secure=secure)


def set_cookie(response: Response, name: str, value: str, *, seconds: float, secure: bool) -> None:
    """Set one of ours on ``response``, for ``seconds``.

    Rounded up, and never to nothing: ``Max-Age=0`` is how a cookie is
    deleted, so a lifetime that rounded down to zero would sign the person out
    at the moment they signed in.
    """
    response.set_cookie(
        name,
        value,
        max_age=max(1, math.ceil(seconds)),
        path="/",
        secure=secure,
        httponly=True,
        samesite=SAME_SITE,
    )


def clear_cookie(response: Response, name: str, *, secure: bool) -> None:
    """Delete one of ours, with the attributes it was set with.

    The attributes are repeated because a browser matches a deletion against
    them: a ``Secure`` cookie is not cleared by an insecure one of the same
    name, and one set at ``Path=/`` is not cleared at another path.
    """
    response.delete_cookie(name, path="/", secure=secure, httponly=True, samesite=SAME_SITE)

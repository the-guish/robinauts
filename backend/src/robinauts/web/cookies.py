# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The session cookie and the login cookie, as this deployment spells them
(``docs/specs/sign-in.md``, "Sessions").

Both are ``HttpOnly``, ``SameSite=Lax``, ``Path=/``, ``Secure`` and named with the ``__Host-``
prefix. On an ``http`` ``public_url`` on loopback, which the proof and the stand-in run on, the
prefix and ``Secure`` are dropped, since a browser refuses both without TLS.
"""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlsplit

from fastapi import Response

from robinauts.web.sign_in import is_loopback


class Cookies:
    def __init__(self, public_url: str) -> None:
        origin = urlsplit(public_url)
        self.secure = not (origin.scheme == "http" and is_loopback(origin.hostname or ""))
        prefix = "__Host-" if self.secure else ""
        self.session = f"{prefix}robinauts_session"
        self.login = f"{prefix}robinauts_login"

    def set(self, response: Response, name: str, value: str, life: timedelta) -> None:
        response.set_cookie(
            name,
            value,
            max_age=int(life.total_seconds()),
            path="/",
            secure=self.secure,
            httponly=True,
            samesite="lax",
        )

    def clear(self, response: Response, name: str) -> None:
        response.delete_cookie(name, path="/", secure=self.secure, httponly=True, samesite="lax")

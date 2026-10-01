# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Running with no sign-in at all: the local development mode.

A mode for developing on one's own machine (``docs/specs/sign-in.md``, "Local
development mode"). There is no identity provider and no sign-in
configuration; every request runs as **one fixed local user**, who owns what
is created in the mode, so that ownership and everything else built on users
is exercised exactly as it is in a deployment.

Three things are here, because more than one layer needs each of them and no
two of those layers may import each other (``docs/layout.md``):

- **who that user is**: ``LOCAL_PROVIDER`` and ``LOCAL_SUBJECT``, the key of
  the one row the mode gets or creates. The provider id is spelt so that
  ``is_provider_id`` **refuses** it: a configured provider's id is a name, and
  ``!local`` is not one, so no configuration can name this provider, no
  sign-in can produce an identity for it, and the reservation is a matter of
  the shape rather than of a check somebody remembered to write;
- **what counts as this machine**: ``is_loopback``, which ``core`` asks of a
  URL and ``api`` asks of a request's ``Host`` header, and the stricter
  ``is_loopback_bind_host``, which is what a server may be **bound** to. The
  two are different questions. A request may *name* this machine however the
  person typed it, ``dev.localhost`` included -- that name is theirs to point
  wherever they like, and if it reached us it reached us over the loopback
  interface. A bind address is not a name somebody resolved: it is handed to
  the operating system, and only a literal loopback address, or the one name
  every system resolves to one, is certain to listen to this machine alone;
- **the mode itself**: ``LocalMode``, which carries the address the server
  will be served on and refuses to be anything but loopback.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from robinauts.legacy.domain.errors import InvalidValueError

LOCAL_PROVIDER = "!local"
"""The reserved provider of the one local user; no configuration may name it.

It is deliberately **not** spelt like a provider id (``domain.is_provider_id``
refuses a leading ``!``), and both the configuration reader and
``ProviderConfig`` hold providers to that shape. So this key cannot be reached
by configuring a provider, by signing in, or by any path that starts from a
provider id -- only by the local development mode, which is the point.
"""

LOCAL_SUBJECT = "developer"
"""The subject half of that key. Fixed: the mode has exactly one user."""

LOCAL_USER_NAME = "Local development"
"""What the interface shows for them, beside the banner saying sign-in is off."""

DEFAULT_LOCAL_HOST = "127.0.0.1"
"""Where the mode is served unless the person says otherwise; loopback either way."""


def is_loopback(host: str) -> bool:
    """Whether ``host`` names this machine: ``localhost``, or a loopback address."""
    if not isinstance(host, str):
        return False
    host = host.strip("[]").lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_loopback_bind_host(host: str) -> bool:
    """Whether a server bound to ``host`` can be reached from this machine only.

    Stricter than ``is_loopback`` on purpose, and used where an address is
    **bound** rather than read off a request. ``*.localhost`` is a name, and a
    name is resolved by whatever the machine resolves names with: the
    convention that it means loopback is a convention, and an entry in a
    resolver, a search domain or a hosts file can make it anything at all. A
    literal address cannot be redirected, and ``localhost`` itself is the one
    name a system is required to answer for itself.
    """
    if not isinstance(host, str):
        return False
    text = _bare(host)
    if text == "localhost":
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def _bare(host: str) -> str:
    """A host as it is compared and stored: no brackets, no root dot, lower case."""
    return host.strip().strip("[]").strip().lower().rstrip(".")


def host_of(authority: str) -> str:
    """The host in ``authority``: a ``Host`` header, or an address a server answered on.

    ``127.0.0.1:8000`` is ``127.0.0.1`` and ``[::1]:8000`` is ``::1``. An IPv6
    address with a port is written in brackets, and one without a port may be
    written bare -- an ASGI scope's ``server`` holds the address and the port
    apart, so that is the form it arrives in. What is refused is the middle
    case: ``::1:8000``, several colons and no brackets, has no reading anybody
    agrees on, and an address nobody can read is not this machine.
    """
    if not isinstance(authority, str):
        return ""
    text = authority.strip()
    if text.startswith("["):
        inside, closed, _ = text[1:].partition("]")
        return inside if closed else ""
    if text.count(":") > 1:
        # A bare IPv6 address, or an attempt at one with a port stuck on it;
        # ``is_loopback`` tells those apart, since only the first is an
        # address.
        return text
    return text.partition(":")[0]


@dataclass(frozen=True, slots=True)
class LocalMode:
    """The local development mode, and the address it is served on.

    The mode **carries the bind host** and refuses a non-loopback one here,
    where nothing has been bound yet: the composition root does not open
    sockets -- the command that starts the server does, a step from now -- so
    the rule is kept at the one moment that cannot be got around later. A
    deployment is the other mode, and it has a ``public_url``
    (``docs/specs/operations.md``, "It is not a way to deploy").
    """

    host: str = DEFAULT_LOCAL_HOST
    """The address to bind, as a server wants it: no brackets, no root dot.

    Normalised here, because it is handed on -- to the server that binds it,
    to the warning at start-up -- and ``[::1]`` is a way of writing an address
    inside a URL, not an address.
    """

    def __post_init__(self) -> None:
        if not isinstance(self.host, str):
            raise InvalidValueError(
                f"the local development mode is served on a host, not {self.host!r}"
            )
        object.__setattr__(self, "host", _normalised(self.host))
        if not is_loopback_bind_host(self.host):
            raise InvalidValueError(
                f"the local development mode serves the loopback interface only, and "
                f"{self.host!r} is not an address of it: serve it on "
                f"{DEFAULT_LOCAL_HOST}, ::1 or localhost -- a name of any other shape is"
                f" resolved by whatever this machine resolves names with -- or run a"
                f" deployment instead"
            )

    def serves(self, authority: str) -> bool:
        """Whether a request naming ``authority`` was addressed to this machine.

        ``authority`` is what a request's ``Host`` header holds, or the address
        the server answered on. Any loopback host with any port passes --
        ``localhost:8000``, ``127.0.0.1:8000``, ``[::1]:8000`` are all the same
        machine -- and everything else fails, which is what a page pointing its
        own host name at ``127.0.0.1`` runs into.
        """
        return is_loopback(host_of(authority))


def _normalised(host: str) -> str:
    """A bind host in the one form it is kept in; an address in its own spelling.

    ``ipaddress`` writes an address the one way there is, so ``[::1]``,
    ``::0001`` and ``::1`` are one host rather than three. Anything that is
    not an address is left as it was bared: only ``localhost`` gets past the
    check below, and it is already written the one way.
    """
    text = _bare(host)
    try:
        return ipaddress.ip_address(text).compressed
    except ValueError:
        return text

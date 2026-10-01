# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""URLs in one form each, so that comparing two of them means something.

An origin and an issuer are both compared exactly, by us and by the identity
provider: the redirect URI a provider registered, the ``Origin`` header of a
write, and an ID token's ``iss``. They are therefore normalised once, as the
configuration is read, and never again.

``is_loopback`` is used here and re-exported: it lives in ``domain``
(``robinauts.legacy.domain.local``) because ``api`` asks the same question of a
request's ``Host`` header in the local development mode and may not import
``core`` (``docs/layout.md``) -- the arrangement ``is_provider_id`` is in, and
for the same reason. What "loopback" means is one answer, in one place.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import parse_qsl, urlencode, urlsplit

from robinauts.legacy.domain import InvalidValueError, is_loopback

DEFAULT_RETURN_TO = "/"
"""Where someone lands after signing in when they asked for nowhere in particular."""

MAX_RETURN_TO = 512
"""The longest return target accepted; a URL longer than this is a trick, not a page."""

DEFAULT_PORTS = {"http": 80, "https": 443}
"""The schemes a deployment or an issuer may use, and the port each implies."""

_NAME = re.compile(r"[a-z0-9._-]+")
"""What a host may be spelt with: an A-label domain, or an IPv4 address."""


def normalise_origin(url: str) -> str:
    """``url`` as a browser writes an ``Origin``: ``scheme://host[:port]``.

    ``InvalidValueError`` unless it is an origin and nothing more. ``http`` is
    allowed for a loopback host alone: on any other host nothing would keep
    the cookie, or the authorization code, from the network in between.
    """
    scheme, host, port, path, _ = _split(url)
    if path not in ("", "/"):
        raise InvalidValueError(
            f"{url!r} has a path: robinauts is served at the root of its origin"
        )
    return _joined(scheme, host, port)


def normalise_issuer(url: str) -> str:
    """An issuer in one form: its path kept, without a trailing slash.

    An issuer may have a path -- Okta's authorization servers do -- so only
    the trailing slash goes, which OIDC treats as the same issuer and string
    comparison does not.
    """
    scheme, host, port, path, _ = _split(url)
    return _joined(scheme, host, port) + path.rstrip("/")


def normalise_endpoint(url: str) -> str:
    """An endpoint a provider published, in the one form it may be used in.

    ``InvalidValueError`` unless what is sent to it is protected: ``https``,
    or ``http`` on a loopback host, which is the stand-in provider of a
    development machine. An ID token whose signature is not checked
    (``robinauts.legacy.core.claims``) is worth exactly the transport it arrived
    over, so this is where that assumption is kept true.

    The URL is not handed back as it came. A discovery document is whatever
    answered the fetch, and ``urlsplit`` silently drops tabs, carriage returns
    and newlines wherever they are, exactly as browsers do:
    ``https://provider.example/authorize\r\nX-Injected: yes`` parses as a
    perfectly good URL, and the string itself would then go into a
    ``Location`` header. Here it does not parse at all, and neither does a
    host smuggled past the check in userinfo.

    What is rebuilt, exactly:

    - the scheme, host and port, written the one way this module writes them;
    - the query, from its parsed pairs, each escaped again -- an authorization
      endpoint may carry one, and Okta's do. ``a=1;b=2`` is one pair, as the
      WHATWG URL standard has it, and ``a`` alone comes back as ``a=``.
      **Which parameters may be there is not decided here**: the caller that
      builds a request out of this endpoint is the only one that knows which
      names are its own (``robinauts.legacy.application``).
    - the path is **not** rebuilt. It is kept exactly as it was written,
      percent-escapes and all, because re-escaping it would change what is
      asked for; nothing dangerous can be left in it, the whole URL having
      been refused already if it held a space or a control character.

    A fragment is refused, having no meaning in a request and every meaning
    in a browser.
    """
    scheme, host, port, path, query = _split(url, query=True)
    endpoint = _joined(scheme, host, port) + path
    if not query:
        return endpoint
    return f"{endpoint}?{urlencode(parse_qsl(query, keep_blank_values=True))}"


def endpoint_query(url: str) -> list[tuple[str, str]]:
    """The query pairs of an endpoint, as ``normalise_endpoint`` rebuilt them.

    For a caller that has to know what a provider put there before it adds
    parameters of its own.
    """
    return parse_qsl(urlsplit(url).query, keep_blank_values=True)


def safe_return_to(target: object) -> str | None:
    """``target`` if it is a place in this deployment, ``None`` if it is anything else.

    Where a person was before they signed in is told to us by a query
    parameter, which is to say by whoever wrote the link they followed. Sent
    back to unchecked, it is an open redirect: a link to our own sign-in page
    that lands on someone else's, with our name in the address bar on the way.

    So: a path of this origin and nothing else. It begins with one ``/``; it
    is not ``//host`` or ``/\\host``, which browsers read as another origin
    with the scheme left out; it carries no scheme, no host and no backslash,
    each of which is a way to spell one of those past a check that reads the
    string differently from a browser.

    Every character must be printable ASCII. A ``Location`` header carries
    nothing else -- a non-ASCII path is written percent-escaped, which is
    ASCII -- and the characters that are not printable are the ones worth
    refusing anyway: a newline is a header of its own, and a zero-width space
    makes two targets that are one to the eye.

    A hash route -- ``/#/chat/7``, which is what the interface uses -- passes.
    """
    if not isinstance(target, str) or not target or len(target) > MAX_RETURN_TO:
        return None
    if not target.isascii() or any(not 0x20 < ord(letter) < 0x7F for letter in target):
        return None
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        return None
    try:
        parts = urlsplit(target)
    except ValueError:
        return None
    if parts.scheme or parts.netloc:
        return None
    return target


def _split(url: str, *, query: bool = False) -> tuple[str, str, int | None, str, str]:
    """``url`` as scheme, host, port, path and query; ``InvalidValueError`` if not one.

    ``query`` says whether a query string is allowed at all: an origin and an
    issuer have none, an endpoint may.
    """
    if not isinstance(url, str):
        raise InvalidValueError(f"{url!r} is not a URL")
    text = url.strip()
    # urlsplit drops tabs and newlines wherever they are, as browsers do, so
    # ``https://exa<tab>mple.com`` would quietly become another host. A URL
    # in a configuration file holds none of that.
    if any(
        character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F for character in text
    ):
        raise InvalidValueError(f"{url!r} holds a space or a control character")
    try:
        parts = urlsplit(text)
        scheme = parts.scheme.lower()
        hostname = parts.hostname
    except ValueError:
        raise InvalidValueError(f"{url!r} is not a URL") from None
    if scheme not in DEFAULT_PORTS:
        raise InvalidValueError(f"{url!r} is not an http or https URL")
    if parts.username or parts.password or parts.fragment or (parts.query and not query):
        raise InvalidValueError(f"{url!r} holds more than a scheme, host, port and path")
    # Before ``hostname``, which lower-cases: that folds U+212A, the Kelvin
    # sign, onto a plain ``k``, and ``\u212aelvin.example`` would pass for
    # ``kelvin.example``.
    if not parts.netloc.isascii():
        raise InvalidValueError(
            f"{url!r} has a host outside ASCII: write it as its A-label "
            f"(punycode, xn--...) form, which is what a certificate and a "
            f"redirect URI carry"
        )
    host = _host(url, hostname or "")
    try:
        port = parts.port
    except ValueError:
        raise InvalidValueError(f"{url!r} has a port that is not one") from None
    if port == 0:
        raise InvalidValueError(f"{url!r} has port 0, which nothing listens on")
    if scheme == "http" and not is_loopback(host):
        raise InvalidValueError(f"{url!r} is http on a host that is not loopback: use https")
    return scheme, host, port, parts.path, parts.query


def _host(url: str, hostname: str) -> str:
    """The host in its one form; ``InvalidValueError`` unless it is a host at all.

    The root label's dot is silent in a name -- ``example.com.`` and
    ``example.com`` are one host -- but it is not silent to ``==``, and these
    strings are compared with ``==`` for the rest of their life. An IPv6
    literal has many spellings of one address, so it is written the way
    ``ipaddress`` compresses it. Everything else must be a name spelt with
    what a name is spelt with: a space, a control character, a backslash or a
    letter outside ASCII is not a host, whatever it may look like.
    """
    host = hostname.lower().rstrip(".")
    if not host:
        raise InvalidValueError(f"{url!r} has no host")
    if not host.isascii():
        raise InvalidValueError(
            f"{url!r} has a host outside ASCII: write it as its A-label "
            f"(punycode, xn--...) form, which is what a certificate and a "
            f"redirect URI carry"
        )
    if ":" in host:
        try:
            return ipaddress.ip_address(host).compressed
        except ValueError:
            raise InvalidValueError(f"{url!r} has a host that is not one") from None
    if not _NAME.fullmatch(host):
        raise InvalidValueError(f"{url!r} has a host that is not one")
    return host


def _joined(scheme: str, host: str, port: int | None) -> str:
    """The pieces back together, without a default port, an IPv6 host in brackets."""
    shown = f"[{host}]" if ":" in host else host
    if port is None or port == DEFAULT_PORTS[scheme]:
        return f"{scheme}://{shown}"
    return f"{scheme}://{shown}:{port}"

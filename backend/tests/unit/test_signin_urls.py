# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Origins, issuers and endpoints in one form each, and where a sign-in returns to."""

import pytest

from robinauts.legacy.core import (
    DEFAULT_RETURN_TO,
    MAX_RETURN_TO,
    is_loopback,
    normalise_endpoint,
    normalise_issuer,
    normalise_origin,
    safe_return_to,
)
from robinauts.legacy.domain import InvalidValueError


@pytest.mark.parametrize(
    ("written", "normalised"),
    [
        ("https://robinauts.example.com", "https://robinauts.example.com"),
        ("https://robinauts.example.com/", "https://robinauts.example.com"),
        ("HTTPS://Robinauts.Example.COM", "https://robinauts.example.com"),
        ("  https://robinauts.example.com  ", "https://robinauts.example.com"),
        ("https://robinauts.example.com:443", "https://robinauts.example.com"),
        ("https://robinauts.example.com:8443", "https://robinauts.example.com:8443"),
        ("http://localhost:8000", "http://localhost:8000"),
        ("http://localhost:80", "http://localhost"),
        ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
        ("http://[::1]:8000", "http://[::1]:8000"),
        # The root label's dot is silent in a name, and must be silent here
        # too: these strings are compared with == for the rest of their life.
        ("https://robinauts.example.com.", "https://robinauts.example.com"),
        ("https://robinauts.example.com./", "https://robinauts.example.com"),
        ("https://Robinauts.Example.com.:8443", "https://robinauts.example.com:8443"),
        ("http://localhost.:8000", "http://localhost:8000"),
    ],
)
def test_an_origin_comes_out_in_one_form(written: str, normalised: str) -> None:
    assert normalise_origin(written) == normalised


@pytest.mark.parametrize(
    "written",
    [
        "",
        "robinauts.example.com",
        "https://",
        "ftp://robinauts.example.com",
        "https://robinauts.example.com/app",
        "https://robinauts.example.com/a/",
        "https://robinauts.example.com?q=1",
        "https://robinauts.example.com#top",
        "https://ada:pw@robinauts.example.com",
        "https://robinauts.example.com:port",
        "https://...",
        "https://[::1",
        "http://robinauts.example.com",
        "http://10.0.0.1",
    ],
)
def test_anything_that_is_not_an_origin_over_https_is_refused(written: str) -> None:
    with pytest.raises(InvalidValueError):
        normalise_origin(written)


@pytest.mark.parametrize(
    ("written", "normalised"),
    [
        ("https://accounts.google.com", "https://accounts.google.com"),
        ("https://accounts.google.com.", "https://accounts.google.com"),
        ("https://ACCOUNTS.GOOGLE.COM.:443/", "https://accounts.google.com"),
        ("https://accounts.google.com/", "https://accounts.google.com"),
        ("https://ACCOUNTS.google.com//", "https://accounts.google.com"),
        (
            "https://example.okta.com/oauth2/default/",
            "https://example.okta.com/oauth2/default",
        ),
        ("https://example.okta.com:443/oauth2/v1", "https://example.okta.com/oauth2/v1"),
        ("http://localhost:8080/realms/test", "http://localhost:8080/realms/test"),
    ],
)
def test_an_issuer_keeps_its_path_and_loses_its_trailing_slash(
    written: str, normalised: str
) -> None:
    assert normalise_issuer(written) == normalised


def test_an_issuer_must_still_be_an_http_url_on_a_host() -> None:
    for written in ("example.okta.com", "https://", "http://example.okta.com"):
        with pytest.raises(InvalidValueError):
            normalise_issuer(written)


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "LOCALHOST",
        "localhost.",
        "api.localhost",
        "127.0.0.1",
        "127.9.9.9",
        "::1",
        "[::1]",
    ],
)
def test_loopback_is_this_machine(host: str) -> None:
    assert is_loopback(host)


@pytest.mark.parametrize(
    "host",
    [
        "",
        "robinauts.example.com",
        "localhost.example.com",
        "10.0.0.1",
        "0.0.0.0",
        "notlocalhost",
        "2001:db8::1",
        "robinauts.example.com.",
    ],
)
def test_anything_reachable_from_the_network_is_not_loopback(host: str) -> None:
    assert not is_loopback(host)


# --- a host is a host, in one form ----------------------------------------


@pytest.mark.parametrize(
    ("written", "normalised"),
    [
        ("https://[2001:0db8:0000:0000:0000:0000:0000:0001]", "https://[2001:db8::1]"),
        ("https://[2001:DB8::0:1]:8443", "https://[2001:db8::1]:8443"),
        ("http://[0:0:0:0:0:0:0:1]:8000", "http://[::1]:8000"),
    ],
)
def test_an_ipv6_address_comes_out_the_way_it_is_compressed(written: str, normalised: str) -> None:
    # One address has many spellings; two of them would never compare equal.
    assert normalise_origin(written) == normalised


@pytest.mark.parametrize(
    "written",
    [
        "https://exa mple.com",
        "https://exa\tmple.com",
        "https://exa\x01mple.com",
        "https://exa\\mple.com",
        "https://bücher.example",
        "https://Kelvin.example",
        "https://[2001:db8::zz]",
        "https://[1::2::3]",
        "https://example.com:0",
        "https://example.com:99999",
        "https://example.com:-1",
    ],
)
def test_a_host_that_is_not_a_host_is_refused(written: str) -> None:
    with pytest.raises(InvalidValueError):
        normalise_origin(written)


def test_a_host_outside_ascii_is_told_how_to_be_written() -> None:
    with pytest.raises(InvalidValueError) as raised:
        normalise_issuer("https://bücher.example/oauth2")
    assert "A-label" in str(raised.value)


@pytest.mark.parametrize("written", [None, 42, b"https://example.com", ["x"], {}, 1.5])
def test_anything_that_is_not_text_is_refused_as_the_contract_says(written: object) -> None:
    # The documented refusal is InvalidValueError, not whatever str method
    # happened to be reached first.
    for normalise in (normalise_origin, normalise_issuer):
        with pytest.raises(InvalidValueError):
            normalise(written)  # type: ignore[arg-type]


def test_a_host_that_is_not_text_is_not_loopback() -> None:
    assert not is_loopback(None)  # type: ignore[arg-type]
    assert not is_loopback(42)  # type: ignore[arg-type]


ENDPOINTS = [
    ("https://provider.example/authorize", "https://provider.example/authorize"),
    ("https://Provider.Example.:443/authorize", "https://provider.example/authorize"),
    ("https://provider.example", "https://provider.example"),
    ("https://provider.example/", "https://provider.example/"),
    # An authorization endpoint may carry a query of its own; Okta's do.
    ("https://provider.example/authorize?ab=1&c=2", "https://provider.example/authorize?ab=1&c=2"),
    (
        "https://provider.example:8443/oauth2/v1/token",
        "https://provider.example:8443/oauth2/v1/token",
    ),
    # Whitespace around it is stripped, as everywhere else in this module;
    # what would be dangerous is whitespace inside it, and that is refused.
    ("  https://provider.example/token\r\n", "https://provider.example/token"),
    # Loopback may speak http: the stand-in provider of a laptop.
    ("http://127.0.0.1:9000/token", "http://127.0.0.1:9000/token"),
    ("http://localhost/token", "http://localhost/token"),
]

NOT_ENDPOINTS = [
    # urlsplit drops these wherever they are, exactly as a browser does, so
    # each parses as a good URL -- and the string would then be a Location
    # header, with whatever follows the newline a header of its own.
    "https://provider.example/authorize\nX-Injected: yes",
    "https://provider.example/autho\trize",
    "https://provider.example\r\n@evil.example/token",
    # Userinfo: the host a person reads is not the host that is reached.
    "https://provider.example@evil.example/token",
    "https://user:pass@provider.example/token",
    # A fragment means nothing in a request and everything in a browser.
    "https://provider.example/authorize#fragment",
    # http anywhere but loopback, and anything that is not http at all.
    "http://provider.example/token",
    "ftp://provider.example/token",
    "javascript:alert(1)",
    "//provider.example/token",
    "/authorize",
    "",
    "https:///token",
    "https://provider.example:0/token",
    "https://bücher.example/token",
]


@pytest.mark.parametrize(("written", "used"), ENDPOINTS)
def test_an_endpoint_is_used_in_the_form_it_was_checked_in(written: str, used: str) -> None:
    assert normalise_endpoint(written) == used


@pytest.mark.parametrize("written", NOT_ENDPOINTS)
def test_an_endpoint_that_is_not_one_is_refused(written: str) -> None:
    with pytest.raises(InvalidValueError):
        normalise_endpoint(written)


RETURN_TO = ["/", "/#/chat/7", "/#/settings", "/conversations/8a1c?from=panel", "/a" * 200]

NOT_RETURN_TO = [
    # Another origin, spelt every way a browser reads as one.
    "https://evil.example/steal",
    "http://evil.example/steal",
    "//evil.example/steal",
    "/\\evil.example/steal",
    "\\\\evil.example",
    "javascript:alert(1)",
    "data:text/html,<script>",
    "mailto:someone@example.com",
    # Not a path of ours at all.
    "chat/7",
    "#/chat/7",
    "",
    " /#/chat",
    # A Location header carries printable ASCII: a newline is a header of its
    # own, and a zero-width space is a second target that looks like the first.
    "/#/chat\n",
    "/#/chat\rHeader: x",
    "/#/ch\tat",
    "/#/chat\x00",
    "/#/chat\u200b",
    "/#/caf\u00e9",
    "/#/chat ",
    "/a" * (MAX_RETURN_TO // 2 + 1),
]


@pytest.mark.parametrize("target", RETURN_TO)
def test_a_place_in_this_deployment_is_returned_to(target: str) -> None:
    assert safe_return_to(target) == target


@pytest.mark.parametrize("target", NOT_RETURN_TO)
def test_anything_that_could_leave_this_origin_is_not(target: str) -> None:
    assert safe_return_to(target) is None


@pytest.mark.parametrize("target", [None, 7, b"/#/chat", ["/#/chat"], object()])
def test_a_return_target_that_is_not_text_is_refused(target: object) -> None:
    assert safe_return_to(target) is None


def test_the_default_return_target_is_a_place_in_this_deployment() -> None:
    assert safe_return_to(DEFAULT_RETURN_TO) == DEFAULT_RETURN_TO

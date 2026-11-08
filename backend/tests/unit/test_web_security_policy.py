# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The interface's Content-Security-Policy with the origins it may frame beside its own pages,
and the origin of the URL those come from: an origin and nothing else ever reaches the policy."""

from __future__ import annotations

import pytest

from robinauts.web.app import origin_of_url
from robinauts.web.headers import (
    CONTENT_SECURITY_POLICY,
    content_security_policy,
    security_headers,
)


@pytest.mark.parametrize(
    ("frame_src", "framed"),
    [
        ((), "frame-src 'self';"),
        (("http://127.0.0.1:8420",), "frame-src 'self' http://127.0.0.1:8420;"),
        (("http://[::1]:8420",), "frame-src 'self' http://[::1]:8420;"),
        (("https://neorc.example.com",), "frame-src 'self' https://neorc.example.com;"),
    ],
)
def test_the_policy_frames_its_own_pages_and_the_origins_given(
    frame_src: tuple[str, ...], framed: str
) -> None:
    policy = content_security_policy(frame_src)
    assert framed in policy
    # Nothing but frame-src moves.
    assert policy.replace(framed, "frame-src 'self';") == CONTENT_SECURITY_POLICY


@pytest.mark.parametrize(
    "origin",
    [
        "*",
        "http:",
        "http://*.example.com",
        "http://127.0.0.1:8420/ui/",
        "http://127.0.0.1:8420; script-src *",
        "'unsafe-inline'",
        "javascript://x",
        "http://user@127.0.0.1",
        "HTTP://127.0.0.1",
    ],
)
def test_anything_but_an_origin_is_refused(origin: str) -> None:
    with pytest.raises(ValueError, match="not an origin"):
        content_security_policy((origin,))


def test_the_headers_carry_the_policy_and_still_refuse_to_be_framed() -> None:
    headers = dict(security_headers(("http://127.0.0.1:8420",)))
    assert headers["x-frame-options"] == "DENY"
    assert headers["content-security-policy"].endswith("frame-ancestors 'none'")


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("http://127.0.0.1:8420/ui/", "http://127.0.0.1:8420"),
        ("https://Neorc.Example.com/ui/?embed=1#/runs", "https://neorc.example.com"),
        ("http://[::1]:8420/ui/", "http://[::1]:8420"),
    ],
)
def test_the_origin_of_a_url_is_its_scheme_host_and_port(url: str, origin: str) -> None:
    assert origin_of_url(url) == origin


@pytest.mark.parametrize(
    "url", ["/ui/", "file:///etc/passwd", "javascript:alert(1)", "http://u:p@127.0.0.1/"]
)
def test_a_url_with_no_web_origin_is_refused(url: str) -> None:
    with pytest.raises(ValueError, match="not an http or https URL"):
        origin_of_url(url)

# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What the proxy of neorc's manager rewrites (``web/neorc.py``): the UI's policy, the
manager's redirects, the path it is asked for and the origin written into the policy."""

from __future__ import annotations

import pytest

from robinauts.web.neorc import local_location, origin_of, ui_policy, upstream_path

NEORC = (
    "default-src 'none'; script-src 'self'; connect-src 'self'; form-action 'self';"
    " frame-ancestors 'none'"
)

POLICIES = [
    (
        NEORC,
        "http://localhost:8000",
        "default-src 'none'; script-src 'self'; connect-src http://localhost:8000/neorc/;"
        " form-action 'self'; frame-ancestors 'self'",
    ),
    (
        NEORC,
        None,
        "default-src 'none'; script-src 'self'; connect-src 'self'; form-action 'self';"
        " frame-ancestors 'self'",
    ),
    ("default-src 'none'", None, "default-src 'none'; frame-ancestors 'self'"),
    (
        "Frame-Ancestors https://a.example;; CONNECT-SRC 'self' data: ;",
        "http://127.0.0.1:9",
        "frame-ancestors 'self'; connect-src http://127.0.0.1:9/neorc/ data:",
    ),
    ("", None, "frame-ancestors 'self'"),
]


@pytest.mark.parametrize(("policy", "origin", "expected"), POLICIES)
def test_ui_policy(policy: str, origin: str | None, expected: str) -> None:
    assert ui_policy(policy, origin) == expected


MANAGER = "http://127.0.0.1:8420"

LOCATIONS = [
    ("/ui/", "/neorc/ui/"),
    ("http://127.0.0.1:8420/ui/", "/neorc/ui/"),
    ("http://127.0.0.1:8420", "/neorc/"),
    ("https://idp.example/login", "https://idp.example/login"),
    ("http://127.0.0.1:9999/ui/", "http://127.0.0.1:9999/ui/"),
    ("//evil.example/x", "//evil.example/x"),
    ("runs", "runs"),
]


@pytest.mark.parametrize(("location", "expected"), LOCATIONS)
def test_local_location(location: str, expected: str) -> None:
    assert local_location(location, MANAGER) == expected


PATHS = [
    ({"path": "/neorc/ui/", "raw_path": b"/neorc/ui/"}, "/ui/"),
    ({"path": "/neorc", "raw_path": b"/neorc"}, "/"),
    ({"path": "/neorc/runs/a/b", "raw_path": b"/neorc/runs/a%2Fb"}, "/runs/a%2Fb"),
    ({"path": "/neorc//evil.example/x", "raw_path": b"/neorc//evil.example/x"}, "/evil.example/x"),
    ({"path": "/neorc/flows"}, "/flows"),
]


@pytest.mark.parametrize(("scope", "expected"), PATHS)
def test_upstream_path(scope: dict[str, object], expected: str) -> None:
    assert upstream_path(scope) == expected


ORIGINS = [
    ("http", "localhost:8000", "http://localhost:8000"),
    ("http", "LOCALHOST", "http://localhost"),
    ("https", "[::1]:8000", "https://[::1]:8000"),
    ("http", "127.0.0.1:8000; script-src *", None),
    ("http", "a b", None),
    ("ws", "localhost", None),
    ("http", None, None),
]


@pytest.mark.parametrize(("scheme", "host", "expected"), ORIGINS)
def test_origin_of(scheme: str, host: str | None, expected: str | None) -> None:
    assert origin_of(scheme, host) == expected

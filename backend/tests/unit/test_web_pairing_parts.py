# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The pure parts of pairing and of the bearer: the number, the link, the QR's SVG, the age a
session must be to mint, the prefix of a secret, and the cache of the allow-list check."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from qrcodegen import QrCode

from robinauts.web.pairing import (
    NUMBER_DIGITS,
    QUIET_MODULES,
    is_fresh,
    link_url,
    match_number,
    qr_svg,
    typed_number,
)
from robinauts.web.sign_in import RecentChecks, SecretKind, kind_of, secret_of_kind

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


def test_a_number_is_six_digits_zero_padded() -> None:
    numbers = {match_number() for _ in range(200)}
    assert all(re.fullmatch(r"[0-9]{6}", n) for n in numbers)
    assert len(numbers) > 150
    assert NUMBER_DIGITS == 6


@pytest.mark.parametrize(
    ("typed", "number"),
    [
        ("482913", "482913"),
        ("482 913", "482913"),
        ("000001", "000001"),
        ("48291", None),
        ("4829131", None),
        ("48291a", None),
        ("４８２９１３", None),
        ("", None),
    ],
)
def test_a_typed_number_is_six_digits_with_or_without_a_space(
    typed: str, number: str | None
) -> None:
    assert typed_number(typed) == number


def test_the_link_names_the_origin_and_carries_the_code_in_its_fragment() -> None:
    assert link_url("https://robinauts.example.com", "a-b_c") == (
        "https://robinauts.example.com/#/link?v=1&c=a-b_c"
    )


def test_the_svg_draws_every_dark_module_inside_a_quiet_zone_and_nothing_else() -> None:
    text = "https://robinauts.example.com/#/link?v=1&c=" + "x" * 43
    svg = qr_svg(text)
    qr = QrCode.encode_text(text, QrCode.Ecc.MEDIUM)
    size = qr.get_size()
    full = size + 2 * QUIET_MODULES
    assert svg.startswith('<svg xmlns="http://www.w3.org/2000/svg"')
    assert f'viewBox="0 0 {full} {full}"' in svg
    assert 'shape-rendering="crispEdges"' in svg
    assert "<script" not in svg
    assert "href" not in svg
    assert svg.count("<path") == 1
    drawn = {
        (int(x) - QUIET_MODULES, int(y) - QUIET_MODULES)
        for x, y in re.findall(r"M(\d+) (\d+)h1v1h-1z", svg)
    }
    dark = {(x, y) for y in range(size) for x in range(size) if qr.get_module(x, y)}
    assert drawn == dark


@pytest.mark.parametrize(
    ("age", "fresh"),
    [
        (timedelta(0), True),
        (timedelta(minutes=9, seconds=59), True),
        (timedelta(minutes=10), False),
        (timedelta(hours=1), False),
        (timedelta(seconds=-1), False),
    ],
)
def test_a_session_is_fresh_for_ten_minutes_from_its_sign_in(age: timedelta, fresh: bool) -> None:
    assert is_fresh(NOW - age, NOW) is fresh


@pytest.mark.parametrize(
    ("secret", "kind"),
    [
        ("rbd_abc", SecretKind.DEVICE),
        ("rbt_abc", SecretKind.TOKEN),
        ("abc", SecretKind.TOKEN),
        ("rbdabc", SecretKind.TOKEN),
        ("", SecretKind.TOKEN),
    ],
)
def test_the_prefix_picks_the_kind_and_no_prefix_is_an_older_token(
    secret: str, kind: SecretKind
) -> None:
    assert kind_of(secret) is kind


@pytest.mark.parametrize("kind", list(SecretKind))
def test_a_new_secret_names_its_kind(kind: SecretKind) -> None:
    secret = secret_of_kind(kind)
    assert secret.startswith(kind.value)
    assert kind_of(secret) is kind
    assert len(secret) > 40


def test_a_pass_of_the_allow_list_is_believed_for_sixty_seconds() -> None:
    checks = RecentChecks()
    assert checks.passed("a", NOW) is False
    checks.record("a", NOW)
    assert checks.passed("a", NOW + timedelta(seconds=59)) is True
    assert checks.passed("a", NOW + timedelta(seconds=60)) is False
    assert checks.passed("a", NOW - timedelta(seconds=1)) is False
    assert checks.passed("b", NOW) is False


def test_the_cache_drops_stale_passes_once_it_is_full() -> None:
    checks = RecentChecks(life=timedelta(seconds=60), kept=2)
    checks.record("old", NOW)
    checks.record("older", NOW)
    checks.record("new", NOW + timedelta(minutes=2))
    assert len(checks) == 1
    assert checks.passed("new", NOW + timedelta(minutes=2)) is True

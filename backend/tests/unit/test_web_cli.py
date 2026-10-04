# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

from __future__ import annotations

from pathlib import Path

import pytest
import uvicorn

from robinauts.controller.contract.domain import ConfigError
from robinauts.web.cli import NO_DATABASE, run, serving

SIGN_IN = {
    "public_url": "https://robinauts.example.com",
    "providers": {
        "okta": {
            "title": "Okta",
            "issuer": "https://example.okta.com/oauth2/default",
            "client_id": "robinauts",
            "client_secret_env": "ROBINAUTS_OKTA_SECRET",
        }
    },
    "allow": [{"provider": "okta", "everyone": True}],
}

DATABASE = {"ROBINAUTS_DATABASE_URL": "postgresql://robinauts@db/robinauts"}


def test_version_prints_the_version(capsys: pytest.CaptureFixture[str]) -> None:
    run(["version"])
    assert capsys.readouterr().out.strip()


def test_a_provider_is_served_with_its_sign_in() -> None:
    _, _, sign_in = serving(SIGN_IN, DATABASE, host="0.0.0.0", dev_no_sign_in=False)
    assert sign_in is not None
    assert list(sign_in.providers) == ["okta"]


@pytest.mark.parametrize("environ", [{}, {"ROBINAUTS_DATABASE_URL": ""}])
def test_sign_in_without_a_database_is_refused(environ: dict[str, str]) -> None:
    # One replica silently keeping its records in memory would be a deployment of its own.
    with pytest.raises(ConfigError) as refused:
        serving(SIGN_IN, environ, host="0.0.0.0", dev_no_sign_in=False)
    assert NO_DATABASE in str(refused.value)


def test_the_mode_may_keep_its_records_in_memory() -> None:
    _, _, sign_in = serving({}, {}, host="127.0.0.1", dev_no_sign_in=True)
    assert sign_in is None


def test_no_provider_is_refused_without_the_mode() -> None:
    with pytest.raises(ConfigError, match="--dev-no-sign-in"):
        serving({}, {}, host="127.0.0.1", dev_no_sign_in=False)


def test_the_mode_cannot_be_combined_with_a_sign_in_table() -> None:
    with pytest.raises(ConfigError, match="cannot be combined"):
        serving({"public_url": SIGN_IN["public_url"]}, {}, host="127.0.0.1", dev_no_sign_in=True)


def test_the_mode_refuses_a_host_off_loopback() -> None:
    with pytest.raises(ConfigError, match="--host 0.0.0.0"):
        serving({}, {}, host="0.0.0.0", dev_no_sign_in=True)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_the_mode_serves_a_loopback_host(host: str) -> None:
    _, _, sign_in = serving({}, {}, host=host, dev_no_sign_in=True)
    assert sign_in is None


def test_an_unknown_top_level_key_is_named() -> None:
    with pytest.raises(ConfigError, match="agent: unknown key"):
        serving({"agent": {}}, {}, host="127.0.0.1", dev_no_sign_in=True)


def test_a_refused_start_exits_before_it_binds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "robinauts.toml"
    path.write_text('public_url = "https://robinauts.example.com"\n')
    monkeypatch.setenv("ROBINAUTS_CONFIG", str(path))
    monkeypatch.setattr(uvicorn, "run", pytest.fail)
    with pytest.raises(SystemExit) as exited:
        run(["start", "--dev-no-sign-in"])
    assert exited.value.code == 1
    assert "cannot be combined" in capsys.readouterr().err

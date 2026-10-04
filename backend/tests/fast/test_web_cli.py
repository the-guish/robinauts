# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

from __future__ import annotations

from pathlib import Path
from unittest.mock import create_autospec

import pytest
import uvicorn

from robinauts.controller.contract.domain import ConfigError
from robinauts.web.cli import run, serving

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


def test_version_prints_the_version(capsys: pytest.CaptureFixture[str]) -> None:
    run(["version"])
    assert capsys.readouterr().out.strip()


def test_a_provider_is_served_with_its_sign_in() -> None:
    database = {"ROBINAUTS_DATABASE_URL": "postgresql://robinauts.example.com/robinauts"}
    _, _, sign_in = serving(SIGN_IN, database, host="0.0.0.0", dev_no_sign_in=False)
    assert sign_in is not None
    assert list(sign_in.providers) == ["okta"]


@pytest.mark.parametrize(
    ("tables", "host", "mode", "refusal"),
    [
        ({}, "127.0.0.1", False, "--dev-no-sign-in"),
        ({}, "0.0.0.0", True, "--host 0.0.0.0"),
        ({"agent": {}}, "127.0.0.1", True, "agent: unknown key"),
    ],
)
def test_a_start_is_refused_with_its_reason(
    tables: dict[str, object], host: str, mode: bool, refusal: str
) -> None:
    with pytest.raises(ConfigError, match=refusal):
        serving(tables, {}, host=host, dev_no_sign_in=mode)


def test_the_bound_on_model_calls_is_the_controller_s_key() -> None:
    config, _, _ = serving(
        {"max_model_calls_per_turn": 60}, {}, host="127.0.0.1", dev_no_sign_in=True
    )
    assert config.max_model_calls_per_turn == 60


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_the_mode_serves_a_loopback_host(host: str) -> None:
    _, _, sign_in = serving({}, {}, host=host, dev_no_sign_in=True)
    assert sign_in is None


def test_a_refused_start_exits_before_it_binds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "robinauts.toml"
    path.write_text('public_url = "https://robinauts.example.com"\n')
    monkeypatch.setenv("ROBINAUTS_CONFIG", str(path))
    serve = create_autospec(uvicorn.run, spec_set=True)
    monkeypatch.setattr(uvicorn, "run", serve)
    with pytest.raises(SystemExit) as exited:
        run(["start", "--dev-no-sign-in"])
    assert exited.value.code == 1
    assert "cannot be combined" in capsys.readouterr().err
    serve.assert_not_called()

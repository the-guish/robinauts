# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

from __future__ import annotations

from pathlib import Path

import pytest
from oneberth.web import Listen

from robinauts.cli.backend import local_url, robinauts_parts, serving
from robinauts.cli.composition import storage_from
from robinauts.cli.main import registry, run
from robinauts.controller.contract.domain import ConfigError
from robinauts.shared_domain.domain import StorageConfig, StorageKind

ONEBERTH = """
[apps.robinauts]
public_url = "https://robinauts.example.com"

[providers.okta]
title = "Okta"
issuer = "https://example.okta.com/oauth2/default"
client_id = "robinauts"
client_secret_env = "ROBINAUTS_OKTA_SECRET"

[[allow]]
provider = "okta"
everyone = true
"""

DATABASE = {"ROBINAUTS_DATABASE_URL": "postgresql://robinauts.example.com/robinauts"}
DATA_DIR = {"ROBINAUTS_DATA_DIR": "/home/someone/.robinauts"}


@pytest.fixture
def oneberth(tmp_path: Path) -> dict[str, str]:
    """``ONEBERTH_CONFIG``, naming a file with one provider, and its secret."""
    path = tmp_path / "oneberth.toml"
    path.write_text(ONEBERTH)
    return {"ONEBERTH_CONFIG": str(path), "ROBINAUTS_OKTA_SECRET": "s"}


def test_version_prints_the_version(capsys: pytest.CaptureFixture[str]) -> None:
    run(["version"])
    assert capsys.readouterr().out.strip()


def test_a_provider_is_served_with_oneberth_s_configuration(
    oneberth: dict[str, str],
) -> None:
    _, _, config = serving({}, DATABASE | oneberth, host="0.0.0.0", port=8000, local=False)
    assert list(config.providers) == ["okta"]
    assert config.app("robinauts").public_url == "https://robinauts.example.com"


@pytest.mark.parametrize(
    ("tables", "environ", "host", "local", "refusal"),
    [
        ({}, DATABASE, "127.0.0.1", False, "ONEBERTH_CONFIG names no file"),
        ({}, {}, "0.0.0.0", True, "0.0.0.0 is not a loopback address"),
        ({"agent": {}}, {}, "127.0.0.1", True, "agent: unknown key"),
        (
            {"orchestration": {}},
            {},
            "127.0.0.1",
            True,
            r"\[orchestration\]: no longer read",
        ),
        ({"providers": {}}, {}, "127.0.0.1", True, "providers: sign-in is oneberth's"),
        ({}, DATABASE | DATA_DIR, "127.0.0.1", True, "are both set: set one"),
        (
            {},
            {"ONEBERTH_CONFIG": "/etc/oneberth.toml"},
            "127.0.0.1",
            True,
            "unset ONEBERTH",
        ),
    ],
)
def test_a_start_is_refused_with_its_reason(
    tables: dict[str, object],
    environ: dict[str, str],
    host: str,
    local: bool,
    refusal: str,
) -> None:
    with pytest.raises(ConfigError, match=refusal):
        serving(tables, environ, host=host, port=8000, local=local)


def test_sign_in_through_providers_needs_a_database(oneberth: dict[str, str]) -> None:
    with pytest.raises(ConfigError, match="no database"):
        serving({}, oneberth, host="127.0.0.1", port=8000, local=False)
    serving({}, oneberth | DATA_DIR, host="127.0.0.1", port=8000, local=False)


def test_the_local_mode_serves_on_every_storage() -> None:
    for environ in ({}, DATABASE, DATA_DIR):
        _, _, config = serving({}, environ, host="127.0.0.1", port=8420, local=True)
        assert config.app("robinauts").public_url == "http://127.0.0.1:8420"
        assert not config.providers
    assert storage_from(DATA_DIR) == StorageConfig(
        StorageKind.LOCAL, path=DATA_DIR["ROBINAUTS_DATA_DIR"]
    )
    assert storage_from(DATABASE | DATA_DIR).kind is StorageKind.POSTGRES
    assert storage_from({}).kind is StorageKind.IN_PROCESS


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_the_local_mode_serves_a_loopback_host(host: str) -> None:
    serving({}, {}, host=host, port=8000, local=True)


@pytest.mark.parametrize(
    ("host", "url"),
    [
        ("127.0.0.1", "http://127.0.0.1:8000"),
        ("0.0.0.0", "http://127.0.0.1:8000"),
        ("::", "http://[::1]:8000"),
        ("::1", "http://[::1]:8000"),
        ("localhost", "http://localhost:8000"),
        ("10.0.0.7", "http://10.0.0.7:8000"),
    ],
)
def test_a_process_on_this_machine_reaches_a_listener_where_it_listens(host: str, url: str) -> None:
    assert local_url(host, 8000) == url


def test_a_refused_start_exits_before_it_binds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "robinauts.toml"
    path.write_text('public_url = "https://robinauts.example.com"\n')
    monkeypatch.setenv("ROBINAUTS_CONFIG", str(path))
    with pytest.raises(SystemExit) as exited:
        run(["start", "--local"])
    assert exited.value.code == 1
    assert "public_url: sign-in is oneberth's" in capsys.readouterr().err


def test_the_admin_scripts_know_the_roles_of_the_three_applications() -> None:
    names = {role.name for role in registry().roles}
    assert {
        "oneberth/admin",
        "robinauts/worker",
        "robinauts/handler",
        "neorc/ci",
    } <= names


def test_the_admin_scripts_need_the_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ROBINAUTS_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as exited:
        run(["devices", "list"])
    assert exited.value.code != 0
    assert "ROBINAUTS_DATABASE_URL" in capsys.readouterr().err


def test_the_one_call_refuses_a_configuration_that_is_not_there(tmp_path: Path) -> None:
    # What a host of its own catches: the same error as for a configuration that is wrong.
    listen = Listen("127.0.0.1", 8000)
    for environ in ({}, {"ROBINAUTS_CONFIG": str(tmp_path / "nowhere.toml")}):
        with pytest.raises(ConfigError, match="^ROBINAUTS_CONFIG"):
            robinauts_parts(environ, front=listen, worker=listen, local=True)

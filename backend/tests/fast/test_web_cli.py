# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

from __future__ import annotations

import argparse
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import create_autospec

import pytest
import uvicorn

from robinauts.controller.composition import compose
from robinauts.controller.contract.domain import (
    Config,
    ConfigError,
    DeviceDescription,
    DeviceSession,
    Identity,
    StorageConfig,
    StorageKind,
)
from robinauts.web.cli import devices_command, run, serving, worker_broker_url
from util.aio import asyncio_test

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


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_the_mode_serves_a_loopback_host(host: str) -> None:
    _, _, sign_in = serving({}, {}, host=host, dev_no_sign_in=True)
    assert sign_in is None


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
def test_a_worker_process_reaches_its_server_where_it_listens(host: str, url: str) -> None:
    assert worker_broker_url(host, 8000) == url


def test_a_refused_start_exits_before_it_binds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "robinauts.toml"
    path.write_text('public_url = "https://robinauts.example.com"\n')
    monkeypatch.setenv("ROBINAUTS_CONFIG", str(path))
    serve = create_autospec(uvicorn.Server.run, spec_set=True)
    monkeypatch.setattr(uvicorn.Server, "run", serve)
    with pytest.raises(SystemExit) as exited:
        run(["start", "--dev-no-sign-in"])
    assert exited.value.code == 1
    assert "cannot be combined" in capsys.readouterr().err
    serve.assert_not_called()


def ended(**which: object) -> argparse.Namespace:
    return argparse.Namespace(
        devices_command="end",
        **{"user": None, "device": None, "all": False, **which},
    )


@asyncio_test
async def test_the_operator_lists_and_ends_devices(capsys: pytest.CaptureFixture[str]) -> None:
    composed = compose(Config(), storage=StorageConfig(StorageKind.IN_MEMORY), secret_for={}.get)
    controller, credentials = composed.controller, composed.credentials
    ada = await controller.ensure_user(Identity("okta", "ada", email="ada@example.com"))
    grace = await controller.ensure_user(Identity("okta", "grace", email="grace@example.com"))
    twin = await controller.ensure_user(Identity("google", "grace", email="Grace@example.com"))
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    devices = []
    for n, user in enumerate((ada, ada, grace, twin)):
        device = DeviceSession(
            uuid.uuid4(),
            user.id,
            DeviceDescription(f"Phone {n}", "android"),
            hashlib.sha256(f"rbd_{n}".encode()).hexdigest(),
            now + timedelta(seconds=n),
            now + timedelta(seconds=n),
            now + timedelta(days=30),
        )
        await credentials.add_device_session(device)
        devices.append(device)

    assert await devices_command(credentials, argparse.Namespace(devices_command="list")) == 0
    listed = capsys.readouterr().out.splitlines()
    assert [line.split("\t")[:3] for line in listed] == [
        [str(devices[0].id), "ada@example.com", "Phone 0"],
        [str(devices[1].id), "ada@example.com", "Phone 1"],
        [str(devices[2].id), "grace@example.com", "Phone 2"],
        [str(devices[3].id), "Grace@example.com", "Phone 3"],
    ]

    # An email two users share is refused, and ends nothing.
    assert await devices_command(credentials, ended(user="grace@example.com")) == 1
    assert "2 users have the email" in capsys.readouterr().err
    assert await devices_command(credentials, ended(user="nobody@example.com")) == 1
    capsys.readouterr()

    assert await devices_command(credentials, ended(user="ADA@example.com")) == 0
    out = capsys.readouterr().out
    assert out.count("ended\t") == 2
    assert "2 device(s) ended" in out
    assert await devices_command(credentials, ended(device=devices[2].id)) == 0
    assert str(devices[2].id) in capsys.readouterr().out
    assert await devices_command(credentials, ended(device=devices[2].id)) == 1
    capsys.readouterr()
    assert await devices_command(credentials, ended(all=True)) == 0
    assert "1 device(s) ended" in capsys.readouterr().out
    assert await credentials.all_device_sessions() == []


def test_the_devices_commands_need_the_database_and_one_choice(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ROBINAUTS_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as exited:
        run(["devices", "list"])
    assert exited.value.code == 2
    assert "ROBINAUTS_DATABASE_URL is not set" in capsys.readouterr().err
    for refused in (["devices", "end"], ["devices", "end", "--all", "--device", str(uuid.uuid4())]):
        with pytest.raises(SystemExit) as exited:
            run(refused)
        assert exited.value.code == 2

# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The start-ups the real ``robinauts`` executable refuses."""

from __future__ import annotations

import os
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

import pytest

from util import stack

pytestmark = pytest.mark.io

SIGN_IN = """
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


def test_a_start_with_sign_in_and_no_database_is_refused(tmp_path: Path) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text("")
    oneberth = tmp_path / "oneberth.toml"
    oneberth.write_text(SIGN_IN)
    environment = {
        **os.environ,
        "ROBINAUTS_CONFIG": str(config),
        "ONEBERTH_CONFIG": str(oneberth),
    }
    environment["ROBINAUTS_OKTA_SECRET"] = "secret"
    environment.pop("ROBINAUTS_DATABASE_URL", None)
    command = [str(stack.ROBINAUTS), "start", "--port", str(stack.free_port())]
    try:
        started = subprocess.run(command, env=environment, capture_output=True, timeout=10)
    except subprocess.TimeoutExpired:
        raise AssertionError("the server started without a database") from None
    assert started.returncode != 0
    assert b"no database: set ROBINAUTS_DATABASE_URL" in started.stderr


def test_a_server_refuses_a_worker_that_runs_in_a_container_alone(tmp_path: Path) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text(
        '[agents.a]\ntitle = "A"\nsystem_prompt = ""\nmodels = ["m"]\nengine = "echo"\n'
        'worker = "boxed"\n'
        '[model_providers.p]\nkind = "anthropic"\napi_key_env = "P_KEY"\n'
        '[models.m]\nprovider = "p"\nname = "m-1"\n'
        '[workers.boxed]\nimage = "robinauts-base-worker"\n'
    )
    environment = {**os.environ, "ROBINAUTS_CONFIG": str(config)}
    for variable in ("ROBINAUTS_DATABASE_URL", "ONEBERTH_CONFIG", "ROBINAUTS_DATA_DIR"):
        environment.pop(variable, None)
    command = [str(stack.ROBINAUTS), "start", "--local", "--port", str(stack.free_port())]
    try:
        started = subprocess.run(command, env=environment, capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise AssertionError("the server started a worker it has no command for") from None
    assert started.returncode == 1
    assert started.stderr.decode().strip() == (
        "workers.boxed: runs in a container of its image, which robinauts desktop starts; a"
        " server starts a worker with the `command` its table names"
    )


def test_the_local_mode_refuses_a_public_host_that_is_not_loopback(tmp_path: Path) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text("")
    environment = {**os.environ, "ROBINAUTS_CONFIG": str(config)}
    for variable in ("ROBINAUTS_DATABASE_URL", "ONEBERTH_CONFIG", "ROBINAUTS_DATA_DIR"):
        environment.pop(variable, None)
    command = [str(stack.ROBINAUTS), "start", "--local", "--host", "0.0.0.0"]
    command += ["--public-host", "10.0.0.7", "--port", str(stack.free_port())]
    started = subprocess.run(command, env=environment, capture_output=True, timeout=30)
    assert started.returncode == 1
    assert started.stderr.decode().strip() == (
        "--local serves this machine alone: 10.0.0.7 is not a loopback address or localhost."
        " Bind elsewhere with --host, and name the loopback address a browser reaches with"
        " --public-host"
    )


def test_a_public_host_needs_the_local_mode(tmp_path: Path) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text("")
    environment = {**os.environ, "ROBINAUTS_CONFIG": str(config)}
    command = [str(stack.ROBINAUTS), "start", "--public-host", "127.0.0.1"]
    started = subprocess.run(command, env=environment, capture_output=True, timeout=30)
    assert started.returncode == 2
    assert (
        started.stderr.decode()
        .strip()
        .endswith(
            "--public-host is for --local: with sign-in, the public URL is in ONEBERTH_CONFIG"
        )
    )


@pytest.mark.parametrize(
    ("named", "content", "refusal"),
    [
        (None, None, "ROBINAUTS_CONFIG names no file: it holds the models, the agents"),
        ("nowhere.toml", None, "nowhere.toml: No such file or directory"),
        ("robinauts.toml", "this is = not [toml", "robinauts.toml: Expected '=' after a key"),
    ],
)
def test_a_start_without_a_configuration_it_can_read_says_so_in_one_line(
    tmp_path: Path, named: str | None, content: str | None, refusal: str
) -> None:
    environment = {**os.environ}
    for variable in ("ROBINAUTS_CONFIG", "ROBINAUTS_DATABASE_URL", "ONEBERTH_CONFIG"):
        environment.pop(variable, None)
    if named is not None:
        environment["ROBINAUTS_CONFIG"] = str(tmp_path / named)
    if content is not None:
        (tmp_path / "robinauts.toml").write_text(content)
    command = [str(stack.ROBINAUTS), "start", "--local", "--port", str(stack.free_port())]
    started = subprocess.run(command, env=environment, capture_output=True, timeout=30)
    assert started.returncode == 1
    said = started.stderr.decode().strip()
    assert said.startswith("ROBINAUTS_CONFIG"), said
    assert len(said.splitlines()) == 1, said
    assert refusal in said


def test_a_component_that_refuses_its_start_says_why_in_the_last_line(tmp_path: Path) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text("")
    data = tmp_path / "data"
    data.mkdir()
    with closing(sqlite3.connect(data / "robinauts.db")) as database:
        database.execute("CREATE TABLE another_application (x)")
    environment = {
        **os.environ,
        "ROBINAUTS_CONFIG": str(config),
        "ROBINAUTS_DATA_DIR": str(data),
    }
    for variable in ("ROBINAUTS_DATABASE_URL", "ONEBERTH_CONFIG"):
        environment.pop(variable, None)
    command = [str(stack.ROBINAUTS), "start", "--local", "--port", str(stack.free_port())]
    started = subprocess.run(command, env=environment, capture_output=True, timeout=60)
    assert started.returncode == 1
    last = started.stderr.decode().strip().splitlines()[-1]
    assert last.startswith("component robinauts failed to start: "), last
    assert "holds tables that are not Robinauts' (another_application)" in last, last


def test_a_grant_of_a_role_no_person_holds_is_refused_in_two_lines(tmp_path: Path) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text("")
    oneberth = tmp_path / "oneberth.toml"
    oneberth.write_text(
        SIGN_IN + '\n[[grant]]\nprovider = "okta"\neveryone = true\nroles = ["neorc/worker"]\n'
    )
    environment = {
        **os.environ,
        "ROBINAUTS_CONFIG": str(config),
        "ONEBERTH_CONFIG": str(oneberth),
        "ROBINAUTS_OKTA_SECRET": "secret",
        # Named and never reached: the grant is refused before the database opens.
        "ROBINAUTS_DATABASE_URL": "postgresql://nobody@127.0.0.1:1/nothing",
    }
    environment.pop("ROBINAUTS_DATA_DIR", None)
    command = [str(stack.ROBINAUTS), "start", "--port", str(stack.free_port())]
    started = subprocess.run(command, env=environment, capture_output=True, timeout=30)
    assert started.returncode == 1
    said = started.stderr.decode()
    # A checkout whose interface is not built warns of it first; the refusal ends the output.
    assert said.strip().splitlines()[-2:] == [
        "invalid oneberth configuration:",
        "grant 1: 'neorc/worker' is not a role for people",
    ], said
    assert "Traceback" not in said, said

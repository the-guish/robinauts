# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The tokens the backend mints for its own parts live in its memory alone
(``docs/architecture/web.md``, "The start and the stop"). Its workers', neorc's worker's, the
flows' upload's and the handlers' end with the process. The store holds none of them while it
serves, none after a stop, and none after a crash.

On PostgreSQL with identity providers, as a server is deployed, with a worker in a process of
its own and a flows folder: every kind of the backend's own token is minted."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
from oneberth.stores import opened_store

from util import stack
from util.controller_db import requires_postgres

pytestmark = requires_postgres

REPO = Path(__file__).resolve().parents[3]

CONFIG = """
[model_providers.echo]
kind = "anthropic"
api_key_env = "ECHO_API_KEY"

[models.echo]
provider = "echo"
name = "echo"

[agents.echo]
title = "Echo"
system_prompt = ""
models = ["echo"]
engine = "echo"
"""

SIGN_IN = """
[apps.robinauts]
public_url = "http://127.0.0.1:{port}"

[providers.okta]
title = "Okta"
issuer = "https://okta.invalid/oauth2/default"
client_id = "robinauts"
client_secret_env = "OKTA_SECRET"

[[allow]]
provider = "okta"
email_domain = "example.com"
"""


async def stored_tokens(url: str) -> list[str]:
    """The service tokens in the store that name a part of a Robinauts backend."""
    async with opened_store(database_url=url) as store:
        tokens = await store.service_tokens()
    return [token.name for token in tokens if token.name.startswith("robinauts-")]


@contextmanager
def a_server(config: Path, sign_in: Path, url: str, port: int) -> Iterator[subprocess.Popen[bytes]]:
    """``robinauts start`` with identity providers, once it says ``ready``."""
    environment = {
        **os.environ,
        **stack.API_KEY,
        "ROBINAUTS_CONFIG": str(config),
        "ONEBERTH_CONFIG": str(sign_in),
        "ROBINAUTS_DATABASE_URL": url,
        "ROBINAUTS_FLOWS_DIR": str(REPO / "orchestration" / "flows"),
        "ECHO_API_KEY": "not-a-key",
        "OKTA_SECRET": "not-a-secret",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    command = [str(stack.ROBINAUTS), "start", "--port", str(port)]
    command += ["--worker-port", str(stack.free_port())]
    # A group of its own: a crash leaves neorc's worker behind, which the test ends.
    running = subprocess.Popen(
        command,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stack.wait_until_ready(running)
        yield running
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(running.pid, signal.SIGKILL)
        running.wait()
        # Its reader has its end now: every process that wrote to it has ended.
        assert running.stdout is not None
        running.stdout.close()


def test_no_token_the_server_mints_for_itself_is_left_in_the_store(tmp_path: Path) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text(CONFIG)
    port = stack.free_port()
    sign_in = tmp_path / "oneberth.toml"
    sign_in.write_text(SIGN_IN.format(port=port))
    with stack.database(config) as url:
        with a_server(config, sign_in, url, port) as running:
            # Its worker is heard from with its token, and neorc took the flows with another.
            assert httpx.get(f"http://127.0.0.1:{port}/health").json() == stack.HEALTHY
            assert asyncio.run(stored_tokens(url)) == []
            running.send_signal(signal.SIGTERM)
            assert running.wait(timeout=stack.STOP_SECONDS) == 0
        assert asyncio.run(stored_tokens(url)) == []

        # A crash: no stop runs. The next start finds nothing of the last one's in the store.
        with a_server(config, sign_in, url, port) as running:
            running.kill()
            running.wait(timeout=stack.STOP_SECONDS)
        with a_server(config, sign_in, url, port) as running:
            assert asyncio.run(stored_tokens(url)) == []
            running.send_signal(signal.SIGTERM)
            assert running.wait(timeout=stack.STOP_SECONDS) == 0

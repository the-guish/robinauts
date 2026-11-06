# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""``/health`` is this node's workers, each heard from within five minutes: the age of the file
each worker touches at every heartbeat."""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path

import httpx

from util import stack
from util.controller_db import requires_postgres
from util.fake_openai import FakeLocalGPTServer

pytestmark = requires_postgres


def test_health_is_the_age_of_the_workers_activity(
    model_server: FakeLocalGPTServer, tmp_path: Path
) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text(
        stack.config_for(model_server.base_url).replace(
            'engine = "pydantic-ai"\n', 'engine = "pydantic-ai"\nworker = "special"\n', 1
        )
    )
    started: list[subprocess.Popen[bytes]] = []
    with (
        stack.database(config) as url,
        stack.server(config, url, stack.API_KEY, started) as server,
        httpx.Client(base_url=server, timeout=10.0) as api,
    ):
        here = Path(tempfile.gettempdir())
        for activity in (
            here / f"robinauts-worker-{started[0].pid}.alive",
            here / f"robinauts-worker-{started[0].pid}-special.alive",
        ):
            assert api.get("/health").json() == {"status": "ok"}

            six_minutes_ago = time.time() - 360
            os.utime(activity, (six_minutes_ago, six_minutes_ago))
            assert api.get("/health").status_code == 503

            activity.touch()
            assert api.get("/health").status_code == 200

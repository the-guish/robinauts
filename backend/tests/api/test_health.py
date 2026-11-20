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

from util import stack
from util.controller_db import requires_postgres
from util.fake_openai import FakeLocalGPTServer

pytestmark = requires_postgres

QUIET = """
[work]
heartbeat_seconds = 600
lease_seconds = 1200
"""
"""No heartbeat of the workers' own while the test runs: a beat would touch the file the test
has just made old, and ``/health`` would answer for the beat rather than for the test. A
heartbeat comes every 30 seconds otherwise, and a start on a busy machine can reach one."""


def test_health_is_the_age_of_the_workers_activity(
    model_server: FakeLocalGPTServer, tmp_path: Path
) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text(
        stack.config_for(model_server.base_url).replace(
            'engine = "pydantic-ai"\n',
            'engine = "pydantic-ai"\nworker = "special"\n',
            1,
        )
        + QUIET
    )
    started: list[subprocess.Popen[bytes]] = []
    with (
        stack.database(config) as url,
        stack.server(config, url, stack.API_KEY, started) as server,
        stack.client(server, timeout=10.0) as api,
    ):
        here = Path(tempfile.gettempdir())
        for activity in (
            here / f"robinauts-worker-{started[0].pid}.alive",
            here / f"robinauts-worker-{started[0].pid}-special.alive",
        ):
            assert api.get("/health").json() == stack.HEALTHY

            six_minutes_ago = time.time() - 360
            os.utime(activity, (six_minutes_ago, six_minutes_ago))
            assert api.get("/health").status_code == 503

            activity.touch()
            assert api.get("/health").status_code == 200

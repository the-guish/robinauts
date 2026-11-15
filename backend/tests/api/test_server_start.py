# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A server that says it is up runs turns: its worker has started."""

from __future__ import annotations

from pathlib import Path

from util import stack
from util.controller_db import requires_postgres
from util.fake_openai import FakeLocalGPTServer

pytestmark = requires_postgres


def test_the_first_turn_needs_no_wait_for_the_worker(
    local_gpt: FakeLocalGPTServer, tmp_path: Path
) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text(stack.config_for(local_gpt.base_url))
    with (
        stack.database(config) as url,
        stack.server(config, url, env=stack.API_KEY) as server,
        # Far less than the worker takes to start, and far more than a turn takes once it has.
        stack.client(server, timeout=2.0) as api,
    ):
        started = api.post(
            "/robinauts/api/turns", json={"agent_id": "pydantic_ai", "text": "hello"}
        )
        assert started.status_code == 200, started.text
        assert '"RUN_FINISHED"' in started.text


def test_a_checkout_whose_interface_is_not_built_serves_the_api_and_says_so_at_ui(
    local_gpt: FakeLocalGPTServer, tmp_path: Path
) -> None:
    """As CI's backend job runs it: ``frontend/dist`` has no ``index.html`` there."""
    config = tmp_path / "robinauts.toml"
    config.write_text(stack.config_for(local_gpt.base_url))
    empty = tmp_path / "dist"
    empty.mkdir()
    env = {**stack.API_KEY, "ROBINAUTS_UI_DIR": str(empty)}
    with (
        stack.database(config) as url,
        stack.server(config, url, env=env) as server,
        stack.client(server) as api,
    ):
        listed = api.get("/robinauts/api/conversations")
        page = api.get("/ui/")
    assert listed.status_code == 200, listed.text
    assert page.status_code == 404
    assert "The interface is not built" in page.text

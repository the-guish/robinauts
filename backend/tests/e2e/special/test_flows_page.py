# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The Flows page: with an orchestrator beside the server, the panel offers "Flows", which
frames neorc's UI, on neorc's own origin, in its embedded mode
(``docs/architecture/orchestration.md``).

neorc is the stand-in of ``util/fake_neorc.py``: its manager's page refuses to be framed but by
the origins it is started with in ``--frame-ancestors``, as neorc's UI does, and says the
address it was opened at. That the browser draws it in the frame, with no Content-Security-Policy
violation on either side (``util/browser.py``), is what proves each side lets the other in."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from util import stack
from util.fake_openai import FakeLocalGPTServer

pytestmark = [pytest.mark.io, pytest.mark.database]

FAKE_NEORC = Path(__file__).resolve().parents[2] / "util" / "fake_neorc.py"
RUN = "11111111-2222-4333-8444-555555555555"

ORCHESTRATION = """
[orchestration]
command = {command}
code_location = "{code}"
port = {port}
"""


@pytest.fixture
def server(local_gpt: FakeLocalGPTServer, tmp_path: Path) -> Iterator[str]:
    config = tmp_path / "robinauts.toml"
    config.write_text(
        stack.config_for(local_gpt.base_url)
        + ORCHESTRATION.format(
            command=json.dumps([sys.executable, str(FAKE_NEORC)]),
            code=stack.ROOT / "orchestration",
            port=stack.free_port(),
        )
    )
    environment = {
        **stack.API_KEY,
        "NEORC_DATABASE_URL": "postgresql://127.0.0.1/neorc-unused",
        "FAKE_NEORC_LOG": str(tmp_path / "neorc.jsonl"),
        # The stand-in's worker imports the handlers from the checkout: it must leave nothing.
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    with stack.database(config) as url, stack.server(config, url, env=environment) as running:
        yield running


def test_flows_frames_neorc_embedded_beside_the_panel(server: str, page: Page) -> None:
    page.goto(server + "/ui/")
    panel = page.get_by_role("complementary", name="Panel")
    panel.get_by_role("link", name="Flows").click()

    expect(page).to_have_url(server + "/ui/#/flows")
    expect(page.get_by_role("heading", level=1, name="Flows")).to_be_visible()
    frame = page.frame_locator('iframe[title="Flows in neorc"]')
    expect(frame.locator("#where")).to_have_text("neorc at ?embed=1#/runs")

    page.goto(f"{server}/ui/#/flows/runs/{RUN}")
    expect(frame.locator("#where")).to_have_text(f"neorc at ?embed=1#/runs/{RUN}")

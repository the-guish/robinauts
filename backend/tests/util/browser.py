# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A browser, and the steps a person takes in the interface, for ``tests/e2e``.

That directory is not collected by a plain run (``norecursedirs``): ``scripts/check-e2e.sh``
builds the interface, installs the browser and runs it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from playwright.sync_api import Locator, Page, expect, sync_playwright

from util.stack import ROOT

# What the interface says of an answer that failed (frontend/src/chat/state.ts).
DID_NOT_FINISH = "This answer did not finish: something went wrong while it was being produced."
# What Chromium's console says of what the Content-Security-Policy blocked.
CSP = "Content Security Policy"


@contextmanager
def browser_page() -> Iterator[Page]:
    if not (ROOT / "frontend" / "dist" / "index.html").is_file():
        pytest.fail("the interface is not built: run scripts/check-e2e.sh")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            # The Content-Security-Policy may block what no step looks at: every test checks it.
            violations: list[str] = []
            page.on("console", lambda m: violations.append(m.text) if CSP in m.text else None)
            yield page
            assert not violations, violations
        finally:
            browser.close()


def send(page: Page, message: str) -> None:
    page.get_by_label("Message input").fill(message)
    page.get_by_label("Send message").click()


def questions(page: Page) -> Locator:
    return page.locator('[data-role="user"]').get_by_role("paragraph")


def answers(page: Page) -> Locator:
    return page.locator('[data-role="assistant"]').get_by_role("paragraph")


def expect_thread(page: Page, exchanges: list[tuple[str, str]]) -> None:
    """The thread on the screen is exactly these questions, each with its answer."""
    expect(questions(page)).to_have_text([q for q, _ in exchanges])
    expect(answers(page)).to_have_text([a for _, a in exchanges])


def edit(page: Page, old: str, new: str) -> None:
    message = page.locator('[data-role="user"]').filter(has_text=old)
    message.hover()  # its actions show on hover
    message.get_by_role("button", name="Edit").click()
    page.get_by_role("textbox", name="Edit message").fill(new)
    page.get_by_role("button", name="Update").click()


def regenerate_last(page: Page) -> None:
    answer = page.locator('[data-role="assistant"]').last
    answer.hover()
    answer.get_by_role("button", name="Refresh").click()


def history(page: Page) -> Locator:
    """The conversations listed in the panel, newest first."""
    return page.get_by_label("Conversations").get_by_role("link")


def open_conversation(page: Page, title: str) -> None:
    history(page).filter(has_text=title).click()


def archive(page: Page, title: str) -> None:
    """Hover that conversation's row, which shows its archive button, and press it."""
    history(page).filter(has_text=title).hover()
    page.get_by_role("button", name=f"Archive {title}").click()


def conversation_action(page: Page, title: str, action: str) -> None:
    """Open that conversation's actions and press one of them: "Rename" or "Delete"."""
    page.get_by_role("button", name=f"Actions for {title}").click()
    page.get_by_role("group", name=f"Actions for {title}").get_by_role(
        "button", name=action
    ).click()

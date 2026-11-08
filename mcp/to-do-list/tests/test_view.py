# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
"""The view: one document that names nothing outside itself and speaks the host's protocol."""

import re

import pytest

from to_do_list.server import view_html


@pytest.fixture(scope="module")
def html():
    return view_html()


@pytest.mark.parametrize(
    "reference",
    ["http", "//", "url(", "@import", "src=", "href=", "srcset", "<link", "<iframe", "<object"],
)
def test_the_view_names_nothing_outside_itself(html, reference):
    # Under default-src 'none' a reference would fail anyway; it is refused here so that the
    # view never even asks. "//" covers protocol-relative addresses and JavaScript's line
    # comments alike, which is why the script's comments are all block comments.
    assert reference not in html.lower()


@pytest.mark.parametrize("api", ["fetch(", "XMLHttpRequest", "WebSocket", "localStorage", "eval("])
def test_the_view_uses_nothing_the_sandbox_takes_away(html, api):
    assert api not in html


def test_the_style_and_the_script_are_inline_and_alone(html):
    assert len(re.findall(r"<script\b", html)) == 1
    assert len(re.findall(r"<style\b", html)) == 1
    assert "<script>" in html


@pytest.mark.parametrize(
    "method",
    [
        "ui/initialize",
        "ui/notifications/initialized",
        "ui/notifications/tool-result",
        "ui/notifications/host-context-changed",
        "tools/call",
    ],
)
def test_the_view_speaks_each_message_of_the_protocol(html, method):
    assert f'"{method}"' in html


@pytest.mark.parametrize("tool", ["add_todo", "toggle_todo", "remove_todo"])
def test_the_view_calls_each_tool_that_changes_the_list(html, tool):
    assert f'"{tool}"' in html


def test_the_view_writes_text_as_text(html):
    assert "innerHTML" not in html
    assert "insertAdjacentHTML" not in html
    assert "document.write" not in html


def test_the_input_has_a_label(html):
    assert '<label for="new-text">' in html
    assert '<input id="new-text"' in html

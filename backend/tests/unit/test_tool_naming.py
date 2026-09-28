# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The tool a server lists, the result a call returns, and the naming between.

``domain.ListedTool`` and ``domain.ToolResult`` are what crosses the
``ToolServers`` port; ``core.tools`` is what turns the listed into the shown
(``docs/specs/agents.md``, "Tools"): the full name, the tools left out and
why, one sorted list for a run, and a full name split back into its server's
prefix and the tool's own name.
"""

from __future__ import annotations

import pytest

from robinauts.core import LeftOut, named_tools, split_tool_name, tools_for_run
from robinauts.domain import (
    MAX_LISTED_TOOL_NAME_CHARS,
    MAX_PART_CHARS,
    MAX_TOOL_NAME_CHARS,
    InvalidValueError,
    ListedTool,
    ToolDefinition,
    ToolResult,
    ToolServerConfig,
)

OBJECT = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
GITHUB = ToolServerConfig(id="github", url="https://api.githubcopilot.com/mcp/", secret_env="G")
JIRA = ToolServerConfig(
    id="jira", url="https://x.atlassian.net/mcp", secret_env="J", prefix="atlassian"
)


def listed(name: str, **changes: object) -> ListedTool:
    fields: dict[str, object] = {"name": name, "input_schema": OBJECT}
    fields.update(changes)
    return ListedTool(**fields)  # type: ignore[arg-type]


# --- the records -----------------------------------------------------------------


def test_a_listed_tool_is_the_servers_own_name_and_the_rest_as_a_definition_carries_it() -> None:
    tool = ListedTool(
        name="search repositories",
        description="Search.",
        input_schema=OBJECT,
        annotations={"readOnlyHint": True},
    )

    assert (tool.name, tool.description) == ("search repositories", "Search.")
    assert tool.input_schema == OBJECT and tool.annotations == {"readOnlyHint": True}
    # Copies, as plain data; the least a server may list is a name.
    assert tool.input_schema is not OBJECT
    assert ListedTool("echo") == ListedTool("echo", "", {}, {})


@pytest.mark.parametrize(
    "name", ["", "   ", "two\nlines", "n" * (MAX_LISTED_TOOL_NAME_CHARS + 1), 5]
)
def test_a_listed_tool_has_one_line_of_name(name: object) -> None:
    with pytest.raises(InvalidValueError):
        ListedTool(name)  # type: ignore[arg-type]


def test_a_listed_tool_s_schema_and_annotations_are_plain_data() -> None:
    with pytest.raises(InvalidValueError):
        ListedTool("t", input_schema=["not", "an", "object"])  # type: ignore[arg-type]
    with pytest.raises(InvalidValueError):
        ListedTool("t", annotations={"when": object()})


def test_a_tool_result_is_text_and_a_flag_bounded_as_the_part_it_becomes() -> None:
    assert ToolResult("found 3").is_error is False
    assert ToolResult("", is_error=True) == ToolResult("", True)
    with pytest.raises(InvalidValueError):
        ToolResult("x" * (MAX_PART_CHARS + 1))
    with pytest.raises(InvalidValueError, match="yes or no"):
        ToolResult("x", is_error="yes")  # type: ignore[arg-type]


# --- naming ------------------------------------------------------------------------


def test_a_listed_tool_is_shown_under_its_servers_prefix_and_its_own_name() -> None:
    kept, left_out = named_tools(GITHUB, [listed("search", description="Search.")])

    assert kept == (
        ToolDefinition(name="github__search", description="Search.", input_schema=OBJECT),
    )
    assert left_out == ()


def test_the_prefix_is_the_servers_configured_one_and_the_annotations_ride_along() -> None:
    (kept,), _ = named_tools(JIRA, [listed("find_issue", annotations={"readOnlyHint": True})])

    assert kept.name == "atlassian__find_issue"
    assert kept.annotations == {"readOnlyHint": True}


def test_a_tool_whose_full_name_the_vendors_would_not_take_is_left_out_by_name() -> None:
    too_long = "s" * (MAX_TOOL_NAME_CHARS - len("github__") + 1)

    kept, left_out = named_tools(GITHUB, [listed("has space"), listed(too_long), listed("ok")])

    assert [tool.name for tool in kept] == ["github__ok"]
    assert [(out.server_id, out.name) for out in left_out] == [
        ("github", "has space"),
        ("github", too_long),
    ]
    assert all("is not one the vendors take" in out.reason for out in left_out)


def test_a_tool_whose_schema_is_not_an_object_at_the_top_is_left_out() -> None:
    either = listed("either", input_schema={"anyOf": [OBJECT, {"type": "string"}]})
    untyped = listed("untyped", input_schema={"properties": {}})

    kept, left_out = named_tools(GITHUB, [either, untyped, listed("ok")])

    assert [tool.name for tool in kept] == ["github__ok"]
    assert [out.name for out in left_out] == ["either", "untyped"]
    assert all("not an object at the top" in out.reason for out in left_out)


def test_a_tool_a_server_lists_twice_is_shown_once() -> None:
    kept, left_out = named_tools(GITHUB, [listed("search", description="first"), listed("search")])

    assert [(tool.name, tool.description) for tool in kept] == [("github__search", "first")]
    assert left_out == (LeftOut("github", "search", "the server listed it twice"),)


def test_a_run_is_handed_every_servers_tools_sorted_by_full_name() -> None:
    kept, left_out = tools_for_run(
        [JIRA, GITHUB],
        {
            "github": [listed("search"), listed("bad name")],
            "jira": [listed("find_issue")],
        },
    )

    assert [tool.name for tool in kept] == ["atlassian__find_issue", "github__search"]
    assert [out.name for out in left_out] == ["bad name"]


def test_a_server_that_listed_nothing_or_was_not_asked_adds_nothing() -> None:
    assert tools_for_run([GITHUB, JIRA], {"github": []}) == ((), ())


def test_two_runs_over_the_same_listing_are_handed_identical_lists() -> None:
    listing = {"github": [listed("b"), listed("a")], "jira": [listed("c")]}

    first, _ = tools_for_run([GITHUB, JIRA], listing)
    second, _ = tools_for_run([JIRA, GITHUB], listing)

    assert first == second
    assert [tool.name for tool in first] == ["atlassian__c", "github__a", "github__b"]


def test_a_full_name_splits_at_the_first_separator_into_prefix_and_name() -> None:
    assert split_tool_name("github__search") == ("github", "search")
    # A tool's own name may hold the separator; the prefix may not, so the
    # first one is the join.
    assert split_tool_name("github__search__deep") == ("github", "search__deep")


@pytest.mark.parametrize("full", ["search", "__search", "github__", "", "___"])
def test_a_name_the_platform_did_not_give_splits_into_nothing(full: str) -> None:
    assert split_tool_name(full) is None

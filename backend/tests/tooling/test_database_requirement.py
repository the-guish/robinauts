# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""When a missing database is a skipped test, and when it is a failed run.

A skip is the right answer on a laptop with no PostgreSQL and the wrong one
in CI: a typo in the URL, a service container that never started, a variable
dropped from the workflow -- and the tests that prove the store works stop
running while the build stays green. ``ROBINAUTS_REQUIRE_POSTGRES`` is how a
build says it will not accept that.

The guard lives here rather than beside the database tests because those
modules skip themselves, and a skipped module cannot fail a run. This one
never skips.
"""

from __future__ import annotations

import pytest

from util.postgres import (
    DATABASE_URL,
    REQUIRE_POSTGRES,
    database_required,
    database_tests_missing,
    no_database_reason,
)


def test_a_database_that_is_required_and_absent_fails_this_run() -> None:
    # The whole point. In CI this passes because the workflow gives the job a
    # PostgreSQL; if it ever stops doing so, this is the failure that says
    # which tests silently stopped running.
    problem = no_database_reason(DATABASE_URL)
    if problem is not None and database_required(REQUIRE_POSTGRES):
        pytest.fail(
            f"ROBINAUTS_REQUIRE_POSTGRES is set, so the database tests may not be skipped: "
            f"{problem}"
        )


@pytest.mark.parametrize("unset", [None, "", " ", "0", "false", "FALSE", "No", "off", " off "])
def test_a_database_is_optional_unless_something_says_otherwise(unset: str | None) -> None:
    # Unset is the default, and a workflow that writes "0" to turn the rule
    # off gets what it asked for rather than the opposite.
    assert database_required(unset) is False


@pytest.mark.parametrize("set_to", ["1", "true", "yes", "on", "required", " 1 "])
def test_anything_else_makes_a_database_compulsory(set_to: str) -> None:
    assert database_required(set_to) is True


def test_the_reason_a_run_gives_for_skipping_names_the_variable() -> None:
    # Whoever reads it has to know what to set.
    reason = no_database_reason(None)

    assert reason is not None
    assert "ROBINAUTS_TEST_DATABASE_URL" in reason


def test_a_database_that_is_there_is_no_reason_to_skip_anything() -> None:
    assert no_database_reason("postgresql://someone@example/somewhere") is None


# And then: did any of them actually run? `conftest.py` asks this at the end
# of the session; the decision is here so that it can be asked without one.


def test_a_run_that_needs_no_database_is_never_questioned() -> None:
    assert database_tests_missing(required=False, ran=0, narrowed=True) is None


def test_a_required_database_that_no_test_touched_fails_the_run() -> None:
    # The hole the variable alone left: `-m "not io"` sets it, skips every
    # database test, and goes green.
    problem = database_tests_missing(required=True, ran=0, narrowed=False)

    assert problem is not None
    assert "not one test marked `database` ran" in problem


def test_a_narrowed_run_cannot_be_the_one_that_proves_the_database() -> None:
    # Even when some database tests did run: a run that selected a subset
    # says nothing about the rest, and CI never selects a subset.
    problem = database_tests_missing(required=True, ran=5, narrowed=True)

    assert problem is not None
    assert "-m, -k or a path" in problem


def test_a_whole_run_that_exercised_the_database_is_believed() -> None:
    assert database_tests_missing(required=True, ran=1, narrowed=False) is None

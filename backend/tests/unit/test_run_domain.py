# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The run record: what each state asks it to hold."""

import uuid
from datetime import datetime

import pytest

from conversations import AGENT, CONVERSATION, MODEL, RUN, at, ended, provenance, run
from robinauts.legacy.domain import (
    ACTIVE_RUN_STATES,
    ENDED_RUN_STATES,
    MAX_RUN_ERROR_CHARS,
    Engine,
    InvalidValueError,
    RunState,
)

NAIVE = datetime(2026, 9, 21, 9, 0)


def test_the_states_are_the_ones_the_spec_names() -> None:
    assert {state.value for state in RunState} == {
        "running",
        "waiting",
        "finished",
        "failed",
        "cancelled",
        "interrupted",
    }
    assert ACTIVE_RUN_STATES == {RunState.RUNNING, RunState.WAITING}
    assert ENDED_RUN_STATES == {
        RunState.FINISHED,
        RunState.FAILED,
        RunState.CANCELLED,
        RunState.INTERRUPTED,
    }


@pytest.mark.parametrize("state", sorted(ACTIVE_RUN_STATES))
def test_an_active_run_is_active_and_has_not_ended(state: RunState) -> None:
    going = run(state=state)
    assert going.is_active
    assert going.finished_at is None
    with pytest.raises(InvalidValueError, match="has not ended"):
        run(state=state, finished_at=at(2))


@pytest.mark.parametrize("state", sorted(ENDED_RUN_STATES))
def test_an_ended_run_is_not_active_and_records_when_it_ended(state: RunState) -> None:
    over = ended(state)
    assert not over.is_active
    assert over.finished_at == at(2)
    with pytest.raises(InvalidValueError, match="records when it ended"):
        ended(state, finished_at=None)


def test_a_failed_run_says_what_went_wrong() -> None:
    assert ended(RunState.FAILED).error == "the provider said no"
    with pytest.raises(InvalidValueError, match="what went wrong"):
        ended(RunState.FAILED, error=None)
    with pytest.raises(InvalidValueError, match="what went wrong"):
        ended(RunState.FAILED, error="")


@pytest.mark.parametrize("state", [RunState.CANCELLED, RunState.INTERRUPTED])
def test_a_run_that_ended_badly_may_say_why(state: RunState) -> None:
    assert ended(state, error="the process went away").error == "the process went away"
    assert ended(state).error is None


@pytest.mark.parametrize("state", [RunState.RUNNING, RunState.WAITING, RunState.FINISHED])
def test_a_run_that_did_not_end_badly_has_no_error_to_record(state: RunState) -> None:
    fields = {"finished_at": at(2)} if state is RunState.FINISHED else {}
    with pytest.raises(InvalidValueError, match="no error to record"):
        run(state=state, error="something", **fields)


def test_an_error_is_bounded() -> None:
    assert len(ended(RunState.FAILED, error="x" * MAX_RUN_ERROR_CHARS).error) == MAX_RUN_ERROR_CHARS
    with pytest.raises(InvalidValueError, match="at most"):
        ended(RunState.FAILED, error="x" * (MAX_RUN_ERROR_CHARS + 1))


def test_a_run_says_what_a_message_it_produced_records() -> None:
    assert run().provenance == provenance()
    assert run(id=RUN, agent=AGENT, model=MODEL, engine=Engine.LANGGRAPH).provenance.engine is (
        Engine.LANGGRAPH
    )


def test_a_run_has_not_started_until_a_process_takes_it_up() -> None:
    assert run(started_at=None).started_at is None
    assert run().started_at == at(0)


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "not a uuid"},
        {"conversation_id": str(CONVERSATION)},
        {"message_id": None},
        {"agent": "An Agent"},
        {"model": ""},
        {"engine": "langgraph"},
        {"state": "running"},
        {"state": None},
        {"created_at": NAIVE},
        {"started_at": NAIVE},
        {"error": 7},
    ],
)
def test_a_run_refuses_a_field_of_the_wrong_kind(changes: dict[str, object]) -> None:
    with pytest.raises(InvalidValueError):
        run(**changes)


def test_a_run_answers_one_message_of_one_conversation() -> None:
    answered = uuid.uuid4()
    going = run(message_id=answered)
    assert (going.conversation_id, going.message_id) == (CONVERSATION, answered)

# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Beginning a turn: the three request shapes, and everything that refuses one.

``application.Turns.start`` and ``regenerate`` over the fakes
(``tests/turns.py``). What is tested here is the control flow and the rules it
applies -- which parent a message may hang under, whose conversation it is,
what a new chat is titled -- and never the store, which has a contract suite
of its own.

The shapes are the ones a turn request has (``docs/specs/wire.md``): a new
conversation with an agent, a new message in one that exists (a continuation
or an edit, told apart only by the parent it names), and a regeneration, which
appends no message at all.
"""

from __future__ import annotations

import uuid

import pytest

from aio import asyncio_test
from conversations import (
    AGENT,
    MODEL,
    OTHER_CONVERSATION,
    OTHER_MODEL,
    agent_definition,
    model_config,
    offered,
)
from fakes import CountingIdSource, FakeClock, MemoryConversationStore, ScriptedAgent, says
from robinauts.legacy.adapters import AsyncioRunExecutor, MemoryRunSignals
from robinauts.legacy.application import Turns
from robinauts.legacy.domain import (
    ACTIVE_RUN_STATES,
    ConversationNotFoundError,
    Engine,
    InvalidMessageTreeError,
    InvalidValueError,
    Message,
    MessageNotFoundError,
    ModelNotOfferedError,
    Run,
    RunAlreadyActiveError,
    RunState,
    TextPart,
    UnknownAgentError,
    UnknownModelError,
)
from turns import AUTHOR, NOW, SOMEBODY_ELSE, Wiring, begun, stored_messages, wired

ANSWER = "Someone who plays fair."


class Watched(MemoryConversationStore):
    """The store, with a note of every call that writes. Nothing else differs."""

    def __init__(self) -> None:
        super().__init__()
        self.wrote: list[str] = []

    async def start_run(self, **kwargs: object) -> None:
        self.wrote.append("start_run")
        await super().start_run(**kwargs)  # type: ignore[arg-type]

    async def add_conversation(self, conversation: object) -> None:
        self.wrote.append("add_conversation")
        await super().add_conversation(conversation)  # type: ignore[arg-type]

    async def append_message(self, *args: object, **kwargs: object) -> None:
        self.wrote.append("append_message")
        await super().append_message(*args, **kwargs)  # type: ignore[arg-type]

    async def touch_conversation(self, *args: object, **kwargs: object) -> object:
        self.wrote.append("touch_conversation")
        return await super().touch_conversation(*args, **kwargs)  # type: ignore[arg-type]


# --- a new conversation ------------------------------------------------------


@asyncio_test
async def test_a_new_chat_stores_the_conversation_the_question_and_the_run_at_once() -> None:
    wiring = wired(store=Watched())

    begun_turn = await wiring.turns.start(AUTHOR, agent_id=AGENT, text="What is a robinaut?")

    # One call, because a turn begins all at once or not at all: a
    # conversation with no question in it, or a question with no run, is
    # something no process may be able to leave behind.
    assert wiring.store.wrote == ["start_run"]  # type: ignore[attr-defined]
    stored = await wiring.store.conversation_by_id(begun_turn.conversation.id)
    assert stored is not None
    assert stored.owner_id == AUTHOR.id
    assert stored.agent == AGENT
    # The agent's default is copied in: the conversation names its model
    # from the start rather than deferring to the agent (docs/specs/agents.md).
    assert stored.model == begun_turn.conversation.model == wiring.definition.model
    assert [message.text for message in await stored_messages(wiring.store, stored.id)] == [
        "What is a robinaut?"
    ]
    assert begun_turn.run.state is RunState.RUNNING
    assert begun_turn.run.message_id == begun_turn.message.id
    assert begun_turn.run.engine is wiring.definition.engine
    assert begun_turn.run.model == wiring.definition.model
    # Nothing has taken it up yet; the start is stamped when it ends.
    assert begun_turn.run.started_at is None
    assert begun_turn.run.created_at == NOW


@asyncio_test
async def test_a_new_chat_is_titled_from_the_beginning_of_its_first_message() -> None:
    wiring = wired()

    begun_turn = await wiring.turns.start(
        AUTHOR,
        agent_id=AGENT,
        text="\n \nWhat   is a robinaut?\nAnd what is not one?",
    )

    assert begun_turn.conversation.title == "What is a robinaut?"


@asyncio_test
async def test_a_new_chat_cannot_hang_under_a_message() -> None:
    wiring = wired()

    with pytest.raises(MessageNotFoundError):
        await wiring.turns.start(AUTHOR, agent_id=AGENT, text="Under what?", parent_id=uuid.uuid4())


@asyncio_test
async def test_an_agent_this_deployment_does_not_have_is_refused() -> None:
    wiring = wired()

    with pytest.raises(UnknownAgentError):
        await wiring.turns.start(AUTHOR, agent_id="nobody", text="Hello?")
    # And a name that is not an id at all is refused as a value, before
    # anything is looked up.
    with pytest.raises(InvalidValueError):
        await wiring.turns.start(AUTHOR, agent_id="NOT AN ID", text="Hello?")


@asyncio_test
async def test_a_turn_names_an_agent_or_a_conversation_and_not_both() -> None:
    wiring = wired()

    with pytest.raises(InvalidValueError):
        await wiring.turns.start(AUTHOR, text="Where does this go?")
    with pytest.raises(InvalidValueError):
        await wiring.turns.start(AUTHOR, agent_id=AGENT, conversation_id=uuid.uuid4(), text="Both?")


# --- the question itself -----------------------------------------------------


@asyncio_test
async def test_a_message_with_nothing_in_it_is_refused() -> None:
    wiring = wired()

    for nothing in ("", "   \n\t ", "\x00"):
        with pytest.raises(InvalidValueError):
            await wiring.turns.start(AUTHOR, agent_id=AGENT, text=nothing)
    with pytest.raises(InvalidValueError):
        await wiring.turns.start(AUTHOR, agent_id=AGENT, text=None)  # type: ignore[arg-type]


@asyncio_test
async def test_what_a_browser_sent_is_repaired_before_it_is_stored() -> None:
    wiring = wired()

    begun_turn = await wiring.turns.start(AUTHOR, agent_id=AGENT, text="a\x00b\ud800c")

    # The NUL is dropped and the lone surrogate replaced: what is stored is
    # storable, and the message is not lost over a character.
    assert begun_turn.message.text == "ab�c"


@asyncio_test
async def test_a_message_longer_than_one_part_is_carried_in_the_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The bound made small, so that a test about splitting is not a test about
    # a megabyte of text.
    monkeypatch.setattr("robinauts.legacy.domain.conversation.MAX_PART_CHARS", 4)
    wiring = wired()

    begun_turn = await wiring.turns.start(AUTHOR, agent_id=AGENT, text="abcdefghij")

    assert begun_turn.message.parts == (TextPart("abcd"), TextPart("efgh"), TextPart("ij"))
    assert begun_turn.message.text == "abcdefghij"


# --- a message in a conversation that exists ---------------------------------


@asyncio_test
async def test_a_message_continues_the_branch_it_names() -> None:
    wiring = wired(*says(ANSWER))
    first = await begun(wiring)
    await wiring.turns.execute(first)
    answered = (await stored_messages(wiring.store, first.conversation_id))[-1]

    begun_turn = await wiring.turns.start(
        AUTHOR, conversation_id=first.conversation_id, text="And why?", parent_id=answered.id
    )

    assert begun_turn.message is not None
    assert begun_turn.message.parent_id == answered.id
    assert begun_turn.run.message_id == begun_turn.message.id
    assert begun_turn.run.agent == AGENT


@asyncio_test
async def test_an_edit_is_a_sibling_under_the_parent_it_names() -> None:
    wiring = wired(*says(ANSWER))
    first = await begun(wiring)
    await wiring.turns.execute(first)
    answered = (await stored_messages(wiring.store, first.conversation_id))[-1]
    followed = await wiring.turns.start(
        AUTHOR, conversation_id=first.conversation_id, text="And why?", parent_id=answered.id
    )
    await wiring.turns.execute(followed.run)

    edited = await wiring.turns.start(
        AUTHOR,
        conversation_id=first.conversation_id,
        text="No: why not?",
        parent_id=answered.id,
    )

    # Beside the message it replaces, under the same parent: nothing is
    # overwritten, and the earlier turn is kept in the store, off the path a
    # reader is shown.
    assert edited.message.parent_id == followed.message.parent_id
    stored = await stored_messages(wiring.store, first.conversation_id)
    assert followed.message.id in {message.id for message in stored}


@asyncio_test
async def test_editing_the_first_question_gives_the_conversation_another_root() -> None:
    wiring = wired(*says(ANSWER))
    first = await begun(wiring)
    await wiring.turns.execute(first)

    edited = await wiring.turns.start(
        AUTHOR, conversation_id=first.conversation_id, text="Ask it another way.", parent_id=None
    )

    assert edited.message.parent_id is None
    assert edited.run.message_id == edited.message.id


@asyncio_test
async def test_a_parent_from_another_conversation_is_simply_not_there() -> None:
    wiring = wired()
    first = await begun(wiring)
    elsewhere = await wiring.turns.start(AUTHOR, agent_id=AGENT, text="Another chat.")

    with pytest.raises(MessageNotFoundError):
        await wiring.turns.start(
            AUTHOR,
            conversation_id=first.conversation_id,
            text="Across?",
            parent_id=elsewhere.message.id,
        )


@asyncio_test
async def test_a_question_does_not_hang_under_a_question() -> None:
    wiring = wired()
    first = await begun(wiring)

    with pytest.raises(InvalidMessageTreeError):
        await wiring.turns.start(
            AUTHOR,
            conversation_id=first.conversation_id,
            text="Two in a row?",
            parent_id=first.message_id,
        )


# --- regenerating ------------------------------------------------------------


@asyncio_test
async def test_regenerating_answers_the_question_that_began_the_turn() -> None:
    wiring = wired(*says(ANSWER))
    first = await begun(wiring)
    await wiring.turns.execute(first)
    answered = (await stored_messages(wiring.store, first.conversation_id))[-1]

    again = await wiring.turns.regenerate(
        AUTHOR, conversation_id=first.conversation_id, message_id=answered.id
    )

    assert again.message is None
    assert again.run.message_id == first.message_id
    assert again.run.id != first.id
    # Nothing was appended: the question is already there.
    assert len(await stored_messages(wiring.store, first.conversation_id)) == 2


@asyncio_test
async def test_only_an_answer_is_regenerated() -> None:
    wiring = wired()
    first = await begun(wiring)

    with pytest.raises(InvalidMessageTreeError):
        await wiring.turns.regenerate(
            AUTHOR, conversation_id=first.conversation_id, message_id=first.message_id
        )


@asyncio_test
async def test_regenerating_a_message_that_is_not_there_is_a_message_not_found() -> None:
    wiring = wired()
    first = await begun(wiring)

    with pytest.raises(MessageNotFoundError):
        await wiring.turns.regenerate(
            AUTHOR, conversation_id=first.conversation_id, message_id=uuid.uuid4()
        )


# --- ownership ---------------------------------------------------------------


@asyncio_test
async def test_somebody_elses_conversation_answers_exactly_like_one_that_is_not_there() -> None:
    wiring = wired()
    first = await begun(wiring)

    with pytest.raises(ConversationNotFoundError) as theirs:
        await wiring.turns.start(
            SOMEBODY_ELSE, conversation_id=first.conversation_id, text="Let me in."
        )
    with pytest.raises(ConversationNotFoundError) as missing:
        await wiring.turns.start(
            SOMEBODY_ELSE, conversation_id=OTHER_CONVERSATION, text="Let me in."
        )

    assert type(theirs.value) is type(missing.value)
    # And regenerating is the same door.
    with pytest.raises(ConversationNotFoundError):
        await wiring.turns.regenerate(
            SOMEBODY_ELSE, conversation_id=first.conversation_id, message_id=first.message_id
        )


# --- one active run ----------------------------------------------------------


@asyncio_test
async def test_a_second_turn_while_one_is_going_is_refused() -> None:
    wiring = wired(*says(ANSWER))
    first = await begun(wiring)

    with pytest.raises(RunAlreadyActiveError):
        await wiring.turns.start(AUTHOR, conversation_id=first.conversation_id, text="And another?")
    # Nothing of the refused turn was written.
    assert len(await stored_messages(wiring.store, first.conversation_id)) == 1
    assert len(await wiring.store.runs_of(first.conversation_id)) == 1


@asyncio_test
async def test_a_regeneration_while_one_is_going_is_refused_too() -> None:
    wiring = wired(*says(ANSWER))
    first = await begun(wiring)
    await wiring.turns.execute(first)
    answered = (await stored_messages(wiring.store, first.conversation_id))[-1]
    await wiring.turns.start(AUTHOR, conversation_id=first.conversation_id, text="And why?")

    with pytest.raises(RunAlreadyActiveError):
        await wiring.turns.regenerate(
            AUTHOR, conversation_id=first.conversation_id, message_id=answered.id
        )


@asyncio_test
async def test_once_the_run_has_ended_the_next_turn_begins() -> None:
    wiring = wired(*says(ANSWER))
    first = await begun(wiring)
    await wiring.turns.execute(first)

    followed = await wiring.turns.start(
        AUTHOR, conversation_id=first.conversation_id, text="And why?"
    )

    assert followed.run.is_active
    assert await wiring.store.active_run_of(first.conversation_id) == followed.run
    assert not [run for run in await wiring.store.runs_in(ACTIVE_RUN_STATES) if run.id == first.id]


# --- the conversation's model ------------------------------------------------


@asyncio_test
async def test_a_new_chat_runs_on_the_model_its_author_picked() -> None:
    wiring = wired()

    begun_turn = await wiring.turns.start(
        AUTHOR, agent_id=AGENT, model_id=OTHER_MODEL, text="What is a robinaut?"
    )

    stored = await wiring.store.conversation_by_id(begun_turn.conversation.id)
    assert stored is not None
    assert stored.model == OTHER_MODEL
    assert begun_turn.run.model == OTHER_MODEL
    # The agent is the one asked for; only the model moved.
    assert stored.agent == AGENT


@asyncio_test
async def test_no_model_picked_is_the_agent_s_default() -> None:
    wiring = wired()

    begun_turn = await wiring.turns.start(
        AUTHOR, agent_id=AGENT, model_id=None, text="What is a robinaut?"
    )

    assert begun_turn.conversation.model == begun_turn.run.model == MODEL


@asyncio_test
async def test_a_model_this_deployment_does_not_offer_begins_nothing() -> None:
    wiring = wired(store=Watched())

    with pytest.raises(UnknownModelError) as refused:
        await wiring.turns.start(AUTHOR, agent_id=AGENT, model_id="gpt-5-5", text="Hello?")
    # The model the request named, and not a conversation's: that is the
    # other refusal (below), and a client says something else about it.
    assert type(refused.value) is UnknownModelError
    # A name that is not an id at all is refused as a value.
    with pytest.raises(InvalidValueError):
        await wiring.turns.start(AUTHOR, agent_id=AGENT, model_id="GPT 5.5", text="Hello?")

    assert wiring.store.wrote == []  # type: ignore[attr-defined]


@asyncio_test
async def test_a_model_is_picked_for_a_new_conversation_and_not_by_a_turn_in_one() -> None:
    wiring = wired()
    first = await begun(wiring)

    with pytest.raises(InvalidValueError):
        await wiring.turns.start(
            AUTHOR, conversation_id=first.conversation_id, model_id=OTHER_MODEL, text="And?"
        )

    assert len(await stored_messages(wiring.store, first.conversation_id)) == 1


@asyncio_test
async def test_setting_the_model_writes_it_and_dates_the_conversation() -> None:
    wiring = wired()
    first = await begun(wiring)
    wiring.clock.advance(60)

    changed = await wiring.turns.set_model(AUTHOR, first.conversation_id, OTHER_MODEL)

    assert changed.model == OTHER_MODEL
    assert changed.updated_at == wiring.clock.now()
    # What came back is what the store holds.
    assert await wiring.store.conversation_by_id(first.conversation_id) == changed


@asyncio_test
async def test_the_next_turn_runs_on_the_model_the_conversation_was_moved_to() -> None:
    wiring = wired(*says(ANSWER))
    first = await begun(wiring)
    await wiring.turns.execute(first)
    await wiring.turns.set_model(AUTHOR, first.conversation_id, OTHER_MODEL)

    followed = await wiring.turns.start(
        AUTHOR, conversation_id=first.conversation_id, text="And why?"
    )

    assert followed.run.model == OTHER_MODEL
    # The first run is a record of what it ran on, and that has not moved.
    assert (await wiring.store.run_by_id(first.id)).model == MODEL  # type: ignore[union-attr]


@asyncio_test
async def test_a_model_this_deployment_does_not_offer_is_not_set() -> None:
    wiring = wired()
    first = await begun(wiring)

    with pytest.raises(UnknownModelError):
        await wiring.turns.set_model(AUTHOR, first.conversation_id, "gpt-5-5")
    # A name that is not an id at all is refused as a value, and so is a
    # conversation id that is not one.
    with pytest.raises(InvalidValueError):
        await wiring.turns.set_model(AUTHOR, first.conversation_id, "GPT 5.5")
    with pytest.raises(InvalidValueError):
        await wiring.turns.set_model(AUTHOR, "not-a-uuid", OTHER_MODEL)  # type: ignore[arg-type]

    stored = await wiring.store.conversation_by_id(first.conversation_id)
    assert stored is not None
    assert stored.model == MODEL
    assert stored.updated_at == NOW


@asyncio_test
async def test_only_its_author_sets_a_conversation_s_model() -> None:
    wiring = wired()
    first = await begun(wiring)

    with pytest.raises(ConversationNotFoundError) as theirs:
        await wiring.turns.set_model(SOMEBODY_ELSE, first.conversation_id, OTHER_MODEL)
    with pytest.raises(ConversationNotFoundError) as missing:
        await wiring.turns.set_model(AUTHOR, OTHER_CONVERSATION, OTHER_MODEL)

    # Somebody else's is answered exactly like one that is not there.
    assert type(theirs.value) is type(missing.value)
    stored = await wiring.store.conversation_by_id(first.conversation_id)
    assert stored is not None
    assert stored.model == MODEL


@asyncio_test
async def test_the_model_may_be_changed_while_a_run_is_going_and_the_run_keeps_its_own() -> None:
    wiring = wired()
    going = await begun(wiring)

    changed = await wiring.turns.set_model(AUTHOR, going.conversation_id, OTHER_MODEL)

    assert changed.model == OTHER_MODEL
    still = await wiring.store.active_run_of(going.conversation_id)
    assert still is not None
    assert still.id == going.id
    assert still.model == MODEL


async def _on_a_model_since_removed() -> tuple[Wiring, Run, Message]:
    """A conversation on ``OTHER_MODEL``, answered, and then a restart whose
    configuration no longer has that model: the same store, fewer models."""
    before = wired(*says(ANSWER))
    first = (
        await before.turns.start(
            AUTHOR, agent_id=AGENT, model_id=OTHER_MODEL, text="What is a robinaut?"
        )
    ).run
    await before.turns.execute(first)
    answered = (await stored_messages(before.store, first.conversation_id))[-1]
    after = wired(*says(ANSWER), store=before.store, models=offered())
    return after, first, answered


@asyncio_test
async def test_a_model_the_deployment_no_longer_offers_refuses_every_kind_of_turn() -> None:
    # No falling back to the agent's default: the point of choosing is knowing
    # who answers (docs/specs/agents.md). A continuation, an edit and a
    # regeneration are refused alike, and none of them writes anything.
    wiring, first, answered = await _on_a_model_since_removed()
    conversation_id = first.conversation_id

    with pytest.raises(ModelNotOfferedError):
        await wiring.turns.start(
            AUTHOR, conversation_id=conversation_id, text="And why?", parent_id=answered.id
        )
    with pytest.raises(ModelNotOfferedError):
        await wiring.turns.start(AUTHOR, conversation_id=conversation_id, text="Rather, who?")
    with pytest.raises(ModelNotOfferedError):
        await wiring.turns.regenerate(
            AUTHOR, conversation_id=conversation_id, message_id=answered.id
        )

    assert len(await stored_messages(wiring.store, conversation_id)) == 2
    assert len(await wiring.store.runs_of(conversation_id)) == 1
    stored = await wiring.store.conversation_by_id(conversation_id)
    assert stored is not None
    assert stored.model == OTHER_MODEL


@asyncio_test
async def test_somebody_else_s_conversation_on_a_removed_model_is_still_not_there() -> None:
    # Whose it is is decided first: the model's refusal says something about
    # the conversation, and a stranger is told what a missing one tells.
    wiring, first, answered = await _on_a_model_since_removed()
    conversation_id = first.conversation_id

    with pytest.raises(ConversationNotFoundError):
        await wiring.turns.start(
            SOMEBODY_ELSE, conversation_id=conversation_id, text="And?", parent_id=answered.id
        )
    with pytest.raises(ConversationNotFoundError):
        await wiring.turns.regenerate(
            SOMEBODY_ELSE, conversation_id=conversation_id, message_id=answered.id
        )


@asyncio_test
async def test_a_conversation_on_a_removed_model_carries_on_once_moved_to_one_on_offer() -> None:
    wiring, first, answered = await _on_a_model_since_removed()

    await wiring.turns.set_model(AUTHOR, first.conversation_id, MODEL)
    followed = await wiring.turns.start(
        AUTHOR, conversation_id=first.conversation_id, text="And why?", parent_id=answered.id
    )

    assert followed.run.model == MODEL


# --- wiring ------------------------------------------------------------------


def test_an_agent_whose_engine_is_not_wired_is_a_deployment_that_does_not_start() -> None:
    # Caught where the mistake is -- at wiring -- and not at the first turn of
    # the conversation somebody started with that agent.
    definition = agent_definition(engine=Engine.LANGGRAPH)

    with pytest.raises(InvalidValueError):
        Turns(
            store=MemoryConversationStore(),
            clock=FakeClock(),
            ids=CountingIdSource(),
            agents={definition.id: definition},
            models=offered(),
            engines={Engine.PYDANTIC_AI: ScriptedAgent()},
            executor=AsyncioRunExecutor(),
            signals=MemoryRunSignals(),
        )


def test_an_agent_filed_under_a_name_that_is_not_its_own_is_refused() -> None:
    definition = agent_definition()

    with pytest.raises(InvalidValueError):
        Turns(
            store=MemoryConversationStore(),
            clock=FakeClock(),
            ids=CountingIdSource(),
            agents={"somebody-else": definition},
            models=offered(),
            engines={definition.engine: ScriptedAgent()},
            executor=AsyncioRunExecutor(),
            signals=MemoryRunSignals(),
        )


def test_an_agent_whose_default_model_is_not_offered_is_a_deployment_that_does_not_start() -> None:
    # Every conversation it began would be refused at its first turn.
    definition = agent_definition(model="haiku")

    with pytest.raises(InvalidValueError):
        Turns(
            store=MemoryConversationStore(),
            clock=FakeClock(),
            ids=CountingIdSource(),
            agents={definition.id: definition},
            models=offered(),
            engines={definition.engine: ScriptedAgent()},
            executor=AsyncioRunExecutor(),
            signals=MemoryRunSignals(),
        )


def test_a_model_filed_under_a_name_that_is_not_its_own_is_refused() -> None:
    definition = agent_definition()

    with pytest.raises(InvalidValueError):
        Turns(
            store=MemoryConversationStore(),
            clock=FakeClock(),
            ids=CountingIdSource(),
            agents={definition.id: definition},
            models={MODEL: model_config(), "somebody-else": model_config(OTHER_MODEL)},
            engines={definition.engine: ScriptedAgent()},
            executor=AsyncioRunExecutor(),
            signals=MemoryRunSignals(),
        )


def test_a_turn_service_refuses_limits_that_are_not_limits() -> None:
    for changes in (
        {"turn_seconds": 0},
        {"turn_seconds": -1.0},
        {"turn_seconds": "soon"},
    ):
        with pytest.raises(InvalidValueError):
            wired(**changes)  # type: ignore[arg-type]

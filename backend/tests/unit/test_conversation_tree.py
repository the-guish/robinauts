# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The tree: which parents are legal, and which path of it a reader sees."""

import time
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import FrozenInstanceError, replace

import pytest

from conversations import CONVERSATION, OTHER_CONVERSATION, answer, at, question
from robinauts.core import (
    ConversationTree,
    check_answers_calls,
    check_parent,
    check_tree,
    may_follow,
    tree_of,
    tree_of_stored,
)
from robinauts.domain import (
    Channel,
    InvalidMessageTreeError,
    InvalidValueError,
    Message,
    MessageNotFoundError,
    Role,
    StoredDataError,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)


def exchange() -> tuple[Message, Message, Message, Message]:
    """Two turns: a question, its answer, a second question, its answer."""
    first = question("one", seconds=0)
    said = answer(first, "1", seconds=1)
    second = question("two", parent=said, seconds=2)
    replied = answer(second, "2", seconds=3)
    return first, said, second, replied


def call(call_id: str) -> ToolCallPart:
    return ToolCallPart(call_id, "github__search", {"q": call_id})


def results(parent: Message, *call_ids: str, seconds: float = 2.0) -> Message:
    """The tool message under ``parent`` answering those calls, one result each."""
    return Message(
        id=uuid.uuid4(),
        conversation_id=CONVERSATION,
        parent_id=parent.id,
        role=Role.TOOL,
        parts=tuple(ToolResultPart(call_id, f"result of {call_id}") for call_id in call_ids),
        created_at=at(seconds),
        channel=Channel.WEB,
    )


def tool_turn() -> tuple[Message, Message, Message, Message]:
    """A question, an answer that calls two tools, the results, the final answer."""
    asked = question("look it up", seconds=0)
    calling = answer(asked, parts=(TextPart("Let me look."), call("c1"), call("c2")), seconds=1)
    answered = results(calling, "c1", "c2", seconds=2)
    final = answer(answered, "Found it.", seconds=3)
    return asked, calling, answered, final


def once(messages: Iterable[Message]) -> Iterator[Message]:
    """The messages as a generator: readable once, and no more than once."""
    return iter(list(messages))


def tree(messages: Iterable[Message], **changes: object) -> ConversationTree:
    """The tree of a conversation's messages, checked."""
    fields: dict[str, object] = {"conversation_id": CONVERSATION}
    fields.update(changes)
    return tree_of(messages, **fields)  # type: ignore[arg-type]


# --- what may follow what ---------------------------------------------------


@pytest.mark.parametrize(
    ("role", "parent_role", "allowed"),
    [
        # A conversation begins with a question, and only with a question.
        (Role.USER, None, True),
        (Role.ASSISTANT, None, False),
        (Role.TOOL, None, False),
        # A question follows an answer, and nothing else.
        (Role.USER, Role.ASSISTANT, True),
        (Role.USER, Role.USER, False),
        (Role.USER, Role.TOOL, True),
        # A turn is a chain: an answer follows the question, another answer,
        # or the result of a tool it called.
        (Role.ASSISTANT, Role.USER, True),
        (Role.ASSISTANT, Role.ASSISTANT, True),
        (Role.ASSISTANT, Role.TOOL, True),
        # A tool message follows the answer that called it.
        (Role.TOOL, Role.ASSISTANT, True),
        (Role.TOOL, Role.USER, False),
        (Role.TOOL, Role.TOOL, False),
    ],
)
def test_the_whole_rule_of_the_shape_of_a_conversation(
    role: Role, parent_role: Role | None, allowed: bool
) -> None:
    assert may_follow(role, parent_role) is allowed


def test_the_rule_is_asked_with_roles_and_not_with_something_else() -> None:
    with pytest.raises(InvalidMessageTreeError):
        may_follow("user", None)
    with pytest.raises(InvalidMessageTreeError):
        may_follow(Role.USER, "assistant")
    with pytest.raises(InvalidMessageTreeError):
        check_parent(Role.USER, "a message")


def test_check_parent_applies_the_rule_to_a_message() -> None:
    first, said, _, _ = exchange()
    assert check_parent(Role.USER, None) is None
    assert check_parent(Role.USER, said) is None
    assert check_parent(Role.ASSISTANT, first) is None
    assert check_parent(Role.ASSISTANT, said) is None
    with pytest.raises(InvalidMessageTreeError, match="does not follow"):
        check_parent(Role.ASSISTANT, None)
    with pytest.raises(InvalidMessageTreeError, match="does not follow"):
        check_parent(Role.USER, first)


def test_a_tool_message_follows_an_answer_that_made_calls() -> None:
    asked, calling, answered, final = tool_turn()
    assert check_parent(Role.TOOL, calling) is None
    assert check_parent(Role.ASSISTANT, answered) is None
    with pytest.raises(InvalidMessageTreeError, match="made tool calls"):
        check_parent(Role.TOOL, answer(asked, "no calls here"))
    with pytest.raises(InvalidMessageTreeError, match="does not follow"):
        check_parent(Role.TOOL, asked)


def test_a_tool_message_answers_exactly_the_calls_of_its_parent_once_each() -> None:
    """The results of one call batch are one tool message: none missing, none
    extra, none twice (``docs/specs/conversations.md``)."""
    asked, calling, answered, final = tool_turn()
    assert check_answers_calls(answered, calling) is None
    # In either order: a batch runs in parallel and its results land as they land.
    assert check_answers_calls(results(calling, "c2", "c1"), calling) is None
    for wrong in (
        results(calling, "c1"),
        results(calling, "c1", "c2", "c3"),
        results(calling, "c1", "c3"),
    ):
        with pytest.raises(InvalidMessageTreeError, match="exactly the calls of its parent"):
            check_answers_calls(wrong, calling)
    # A message of another role has no calls to answer.
    assert check_answers_calls(final, answered) is None
    assert check_answers_calls(asked, calling) is None
    # Under the wrong parent it is not that parent's answer at all.
    with pytest.raises(InvalidMessageTreeError, match="does not hang under"):
        check_answers_calls(answered, answer(asked, parts=(call("c1"), call("c2"))))
    with pytest.raises(InvalidMessageTreeError):
        check_answers_calls("not a message", calling)  # type: ignore[arg-type]


def test_the_tree_holds_a_tool_message_to_its_parents_calls() -> None:
    asked, calling, answered, final = tool_turn()
    whole = tree([asked, calling, answered, final])
    assert whole.path_to(final.id) == (asked, calling, answered, final)
    assert whole.turn_start(final.id) == asked
    assert whole.parent_for_regenerate(final.id) == asked.id
    # The rule is the tree's: rows that break it are no conversation.
    with pytest.raises(InvalidMessageTreeError, match="exactly the calls of its parent"):
        tree([asked, calling, results(calling, "c1")])
    plain = answer(asked, "plain", seconds=1)
    with pytest.raises(InvalidMessageTreeError, match="made tool calls"):
        tree([asked, plain, results(plain, "c1")])
    # The results of one call batch are one tool message: two under one
    # answer would read as one replacing the other, and nothing legitimate
    # writes a second, so they are rows that are no conversation.
    first = results(calling, "c1", "c2", seconds=2)
    second = results(calling, "c1", "c2", seconds=2.5)
    with pytest.raises(InvalidMessageTreeError, match="more than one tool message"):
        tree([asked, calling, first, second])
    # A call left without a result is a run that stopped, and the format allows it.
    assert tree([asked, calling]).visible_path() == (asked, calling)
    with pytest.raises(StoredDataError):
        tree_of_stored([asked, calling, results(calling, "c1")], conversation_id=CONVERSATION)


# --- what a collection of messages must be ----------------------------------


def test_a_conversation_nobody_has_written_in_yet_passes() -> None:
    empty = tree([])
    assert empty.messages == ()
    assert empty.leaves() == ()
    assert empty.visible_leaf() is None
    assert empty.visible_path() == ()
    assert check_tree([], conversation_id=CONVERSATION) == ()


def test_a_well_formed_conversation_passes_and_comes_back_in_order() -> None:
    first, said, second, replied = exchange()
    assert tree([replied, first, second, said]).messages == (first, said, second, replied)
    assert check_tree([replied, first, second, said], conversation_id=CONVERSATION) == (
        first,
        said,
        second,
        replied,
    )


def test_a_turn_may_produce_several_messages() -> None:
    first = question("one", seconds=0)
    said = answer(first, "thinking out loud", seconds=1)
    also = answer(said, "and the answer", seconds=2)
    second = question("two", parent=also, seconds=3)
    built = tree([first, said, also, second])
    assert built.messages == (first, said, also, second)
    assert built.path_to(second.id) == (first, said, also, second)


def test_two_messages_of_the_same_id_are_refused() -> None:
    first = question()
    with pytest.raises(InvalidMessageTreeError, match="share the id"):
        tree([first, replace(first, parts=first.parts)])


def test_something_that_is_not_a_message_is_refused() -> None:
    with pytest.raises(InvalidMessageTreeError):
        tree(["a message"])


def test_a_parent_that_is_not_here_is_refused() -> None:
    with pytest.raises(InvalidMessageTreeError, match="no parent here"):
        tree([answer(uuid.uuid4())])


def test_messages_of_two_conversations_are_refused() -> None:
    with pytest.raises(InvalidMessageTreeError, match="do not belong"):
        tree([question(), question(conversation_id=OTHER_CONVERSATION, seconds=1)])


def test_a_parent_in_another_conversation_is_an_orphan() -> None:
    elsewhere = question(conversation_id=OTHER_CONVERSATION)
    with pytest.raises(InvalidMessageTreeError):
        tree([answer(elsewhere)], conversation_id=OTHER_CONVERSATION)


def test_the_conversation_is_said_and_never_inferred() -> None:
    """A caller that passed the wrong messages is refused, not answered."""
    first = question(conversation_id=OTHER_CONVERSATION)
    with pytest.raises(InvalidMessageTreeError, match="do not belong"):
        tree([first])
    assert tree([first], conversation_id=OTHER_CONVERSATION).messages == (first,)
    with pytest.raises(TypeError):
        tree_of([first])  # type: ignore[call-arg]
    with pytest.raises(InvalidValueError, match="is a UUID"):
        tree([first], conversation_id="not a uuid")


def test_a_cycle_is_refused() -> None:
    first = question("one", seconds=0)
    said = answer(first, seconds=1)
    with pytest.raises(InvalidMessageTreeError, match="its own ancestor"):
        tree([replace(first, parent_id=said.id), said])


def test_a_question_may_stand_at_the_root_and_so_may_a_second_one() -> None:
    first = question("one", seconds=0)
    edited = question("one, better", seconds=1)
    assert len(tree([first, edited]).messages) == 2


def test_a_long_branch_is_walked_once_per_message() -> None:
    messages = deep(200)
    built = tree(messages)
    assert len(built.messages) == len(messages)
    assert len(built.path_to(messages[-1].id)) == len(messages)


def test_the_messages_are_read_once_whatever_they_arrive_in() -> None:
    first, said, second, replied = exchange()
    messages = [first, said, second, replied]
    assert tree(once(messages)).messages == tuple(messages)
    assert tree_of_stored(once(messages), conversation_id=CONVERSATION).messages == tuple(messages)
    assert check_tree(once(messages), conversation_id=CONVERSATION) == tuple(messages)


def test_the_order_is_by_time_and_then_by_id() -> None:
    same = [question("a", seconds=0), question("b", seconds=0), question("c", seconds=0)]
    assert tree(same).messages == tuple(sorted(same, key=lambda message: message.id.bytes))


# --- one door for rows ------------------------------------------------------


def test_rows_are_checked_once_at_the_one_door_they_come_through() -> None:
    """The read path: what comes out of the database is ours to get right,
    and it is said once rather than at every question afterwards."""
    first = question("one", seconds=0)
    said = answer(first, seconds=1)
    cycle = [replace(first, parent_id=said.id), said]
    for broken in ([answer(uuid.uuid4())], ["not a message"], cycle):
        with pytest.raises(StoredDataError) as refused:
            tree_of_stored(broken, conversation_id=CONVERSATION)
        assert refused.value.__cause__ is not None
    with pytest.raises(StoredDataError):
        tree_of_stored([question(conversation_id=OTHER_CONVERSATION)], conversation_id=CONVERSATION)


def test_afterwards_a_question_can_only_fail_for_the_requests_reasons() -> None:
    """A checked tree answers; the one way it refuses is an id nobody has."""
    first, said, second, replied = exchange()
    built = tree_of_stored([first, said, second, replied], conversation_id=CONVERSATION)
    assert built.conversation_id == CONVERSATION
    assert built.messages == (first, said, second, replied)
    assert built.path_to(replied.id) == (first, said, second, replied)
    assert built.visible_leaf() == replied
    assert built.visible_path() == (first, said, second, replied)
    assert built.turn_start(replied.id) == second
    assert built.parent_for_regenerate(replied.id) == second.id
    assert built.leaves() == (replied,)
    assert built.message_at(said.id) == said
    assert built.check_attachment(parent_id=replied.id, role=Role.USER) is None

    nobodys = uuid.uuid4()
    for ask in (
        built.path_to,
        built.turn_start,
        built.parent_for_regenerate,
        built.message_at,
    ):
        with pytest.raises(MessageNotFoundError):
            ask(nobodys)
    with pytest.raises(MessageNotFoundError):
        built.check_attachment(parent_id=nobodys, role=Role.USER)


def test_a_tree_cannot_be_rewritten_from_outside() -> None:
    """Documented immutable, and immutable in fact. There is no index to
    hand in either: a tree is made of its messages, and everything else about
    it is computed from them."""
    first, said, _, _ = exchange()
    built = tree([first, said])
    with pytest.raises(TypeError):
        built.at[said.id] = first  # type: ignore[index]
    with pytest.raises(TypeError):
        built.below[said.id] = (first,)  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        built.messages = ()  # type: ignore[misc]


def test_an_unchecked_tree_cannot_be_built_at_all() -> None:
    """The constructor is the check: there is no other way to have one."""
    first = question("one", seconds=0)
    said = answer(first, seconds=1)
    looped = replace(first, parent_id=said.id)
    with pytest.raises(InvalidMessageTreeError, match="its own ancestor"):
        ConversationTree((looped, said), conversation_id=CONVERSATION)
    with pytest.raises(InvalidMessageTreeError, match="no parent here"):
        ConversationTree((said,), conversation_id=CONVERSATION)


def test_a_walk_still_ends_if_a_tree_is_doctored_afterwards() -> None:
    """Defence in depth behind the constructor: nothing reaches these."""
    first = question("one", seconds=0)
    said = answer(first, seconds=1)
    built = tree([first, said])
    looped = replace(first, parent_id=said.id)
    object.__setattr__(built, "at", {looped.id: looped, said.id: said})
    object.__setattr__(built, "below", {looped.id: (said,), said.id: (looped,)})
    with pytest.raises(InvalidMessageTreeError, match="does not end"):
        built.path_to(said.id)


# --- paths and leaves --------------------------------------------------------


def test_a_path_runs_from_a_root_down_to_the_leaf() -> None:
    first, said, second, replied = exchange()
    built = tree([said, replied, first, second])
    assert built.path_to(replied.id) == (first, said, second, replied)
    assert built.path_to(first.id) == (first,)


def test_a_path_to_a_message_of_another_branch_is_that_branch_alone() -> None:
    first, said, second, replied = exchange()
    other = question("two, differently", parent=said, seconds=4)
    assert tree([first, said, second, replied, other]).path_to(other.id) == (first, said, other)


def test_leaves_are_the_ends_of_the_branches() -> None:
    first, said, second, replied = exchange()
    other = question("two, differently", parent=said, seconds=4)
    assert tree([first, said, second, replied, other]).leaves() == (replied, other)


# --- turns ------------------------------------------------------------------


def test_the_turn_a_message_belongs_to_is_found_from_anywhere_inside_it() -> None:
    first = question("one", seconds=0)
    said = answer(first, "thinking", seconds=1)
    also = answer(said, "answering", seconds=2)
    built = tree([first, said, also])
    assert built.turn_start(also.id) == first
    assert built.turn_start(said.id) == first
    assert built.turn_start(first.id) == first


# --- editing and regenerating -----------------------------------------------


def test_a_regeneration_attaches_under_the_question_of_the_whole_turn() -> None:
    first, said, second, replied = exchange()
    messages = [first, said, second, replied]
    assert tree(messages).parent_for_regenerate(replied.id) == second.id
    again = answer(second, "2, again", seconds=4)
    assert tree([*messages, again]).path_to(again.id) == (first, said, second, again)


def test_regenerating_a_turn_of_several_messages_goes_back_to_its_question() -> None:
    """Not under whatever the old answer followed: the turn is replaced."""
    first = question("one", seconds=0)
    said = answer(first, "thinking", seconds=1)
    also = answer(said, "answering", seconds=2)
    messages = [first, said, also]
    assert tree(messages).parent_for_regenerate(also.id) == first.id
    again = answer(first, "answering again", seconds=3)
    built = tree([*messages, again])
    assert built.path_to(again.id) == (first, again)
    assert built.visible_path() == (first, again)


def test_only_an_answer_is_regenerated() -> None:
    first, said, _, _ = exchange()
    built = tree([first, said])
    with pytest.raises(InvalidMessageTreeError, match="only an answer is regenerated"):
        built.parent_for_regenerate(first.id)
    with pytest.raises(MessageNotFoundError):
        built.parent_for_regenerate(uuid.uuid4())


def test_where_a_new_message_may_attach() -> None:
    first, said, _, _ = exchange()
    built = tree([first, said])
    assert built.check_attachment(parent_id=None, role=Role.USER) is None
    assert built.check_attachment(parent_id=said.id, role=Role.USER) is None
    assert built.check_attachment(parent_id=first.id, role=Role.ASSISTANT) is None
    assert built.check_attachment(parent_id=said.id, role=Role.ASSISTANT) is None
    with pytest.raises(InvalidMessageTreeError):
        built.check_attachment(parent_id=first.id, role=Role.USER)
    with pytest.raises(InvalidMessageTreeError):
        built.check_attachment(parent_id=None, role=Role.ASSISTANT)
    with pytest.raises(MessageNotFoundError):
        built.check_attachment(parent_id=uuid.uuid4(), role=Role.USER)


def test_a_new_message_may_not_hang_under_another_conversations_message() -> None:
    """These are one conversation's messages: somebody else's is not here."""
    first, said, _, _ = exchange()
    elsewhere = question("theirs", conversation_id=OTHER_CONVERSATION)
    with pytest.raises(MessageNotFoundError):
        tree([first, said]).check_attachment(parent_id=elsewhere.id, role=Role.ASSISTANT)


# --- the visible path: what a reader sees -----------------------------------


def test_the_visible_path_runs_from_the_root_to_the_newest_leaf() -> None:
    first, said, second, replied = exchange()
    built = tree([replied, second, first, said])
    assert built.visible_leaf() == replied
    assert built.visible_path() == (first, said, second, replied)


def test_an_edit_puts_the_old_question_and_everything_under_it_off_the_path() -> None:
    first, said, second, replied = exchange()
    edited = question("two, better", parent=said, seconds=4)
    built = tree([first, said, second, replied, edited])
    assert built.visible_leaf() == edited
    assert built.visible_path() == (first, said, edited)
    # Kept in the tree, off the path: soft-deleted by shape, for analytics.
    assert (second, replied) == (built.message_at(second.id), built.message_at(replied.id))


def test_a_regeneration_shows_only_the_new_answer() -> None:
    first, said, second, replied = exchange()
    again = answer(second, "2, again", seconds=4)
    built = tree([first, said, second, replied, again])
    assert built.visible_path() == (first, said, second, again)


def test_editing_the_first_question_shows_only_the_new_roots_path() -> None:
    first, said, _, _ = exchange()
    again = question("one, again", seconds=4)
    assert tree([first, said, again]).visible_path() == (again,)


def test_a_run_in_flight_ends_the_thread_at_what_it_is_extending() -> None:
    """A regeneration writes nothing until its first answer completes, so the
    newest leaf is still what it is replacing; the thread a reader is sent
    ends at the message the run's next one hangs under instead."""
    first, said, second, replied = exchange()
    built = tree([first, said, second, replied])
    # Regenerating the first answer: nothing written yet, the run answers
    # the first question.
    assert built.visible_path(extending=first.id) == (first,)
    # Regenerating the last answer: the same rule, at the end of the thread.
    assert built.visible_path(extending=second.id) == (first, said, second)
    # Once the regeneration has completed an answer, it is the newest leaf
    # and the two rules agree.
    again = answer(first, "1, again", seconds=4)
    regenerated = tree([first, said, second, replied, again])
    assert regenerated.visible_path(extending=again.id) == (first, again)
    assert regenerated.visible_path() == (first, again)


def test_an_ordinary_turn_or_an_edit_in_flight_extends_the_visible_path() -> None:
    first, said, second, replied = exchange()
    # A question just asked is the newest leaf, and it is what the run extends.
    asked = tree([first, said, second])
    assert asked.visible_path(extending=second.id) == asked.visible_path()
    # So is an edited question: the old one stays off the path.
    edited = question("two, better", parent=said, seconds=4)
    built = tree([first, said, second, replied, edited])
    assert built.visible_path(extending=edited.id) == (first, said, edited)
    assert built.visible_path() == (first, said, edited)
    # And an answer the run has completed, with more of the turn to come.
    answered = tree([first, said, second, replied])
    assert answered.visible_path(extending=replied.id) == (first, said, second, replied)


def test_a_run_extending_nothing_here_is_refused() -> None:
    first, said, _, _ = exchange()
    with pytest.raises(MessageNotFoundError):
        tree([first, said]).visible_path(extending=uuid.uuid4())
    with pytest.raises(MessageNotFoundError):
        tree([]).visible_path(extending=first.id)


def test_the_newest_leaf_is_by_time_and_then_by_id() -> None:
    """Not the leaf of the branch begun last: one continued later is the
    visible one. And a tie on the clock is settled by id, so that two stores
    -- or two reads -- agree on the thread."""
    first = question("one", seconds=0)
    old = answer(first, "the old branch", seconds=1)
    new = answer(first, "the new branch", seconds=2)
    continued = question("carrying on the old one", parent=old, seconds=10)
    assert tree([first, old, new, continued]).visible_path() == (first, old, continued)

    tied = sorted(
        (answer(first, "a", seconds=1), answer(first, "b", seconds=1)),
        key=lambda message: message.id.bytes,
    )
    assert tree([first, *tied]).visible_leaf() == tied[-1]
    assert tree([first, *reversed(tied)]).visible_leaf() == tied[-1]


# --- a conversation somebody has used --------------------------------------


def wide(children: int) -> tuple[Message, list[Message]]:
    """One question, answered ``children`` times: what regenerating leaves."""
    asked = question("one", seconds=0)
    return asked, [answer(asked, f"{step}", seconds=step + 1) for step in range(children)]


def deep(steps: int) -> list[Message]:
    """One branch, ``steps`` messages long."""
    messages = [question("one", seconds=0)]
    for step in range(1, steps):
        previous = messages[-1]
        messages.append(
            answer(previous, "said", seconds=step)
            if previous.role is Role.USER
            else question("more", parent=previous, seconds=step)
        )
    return messages


def test_a_conversation_of_ten_thousand_messages_is_read_once_not_once_each() -> None:
    """A guard against a quadratic walk, not a benchmark.

    Ten thousand answers under one question is what regenerating leaves
    behind in the store, and a function that found the newest of them by
    walking the whole conversation for each would take seconds to open it.
    """
    asked, said = wide(10_000)
    messages = [asked, *said]
    chain = deep(10_000)

    started = time.monotonic()
    wide_tree = tree_of_stored(messages, conversation_id=CONVERSATION)
    deep_tree = tree_of_stored(chain, conversation_id=CONVERSATION)
    assert len(wide_tree.messages) == 10_001
    assert len(wide_tree.below[asked.id]) == 10_000
    assert len(wide_tree.leaves()) == 10_000
    assert wide_tree.visible_path() == (asked, said[-1])
    assert len(deep_tree.path_to(chain[-1].id)) == 10_000
    assert len(deep_tree.visible_path()) == 10_000
    assert deep_tree.turn_start(chain[-1].id) == chain[-2]
    spent = time.monotonic() - started

    assert spent < 5.0, f"ten walks of ten thousand messages took {spent:.1f}s"

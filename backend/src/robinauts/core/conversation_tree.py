# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The rules of the tree: what a conversation's messages may look like.

A conversation is **stored** as a tree and **shown** as one thread
(``docs/specs/conversations.md``). Editing a question or regenerating an
answer writes a new message beside the old one, under the same parent, and
nothing is overwritten; what a reader sees is the **visible path**, the walk
from a root to the newest leaf. Everything the edit put aside is still in
the tree, off that path: that is how an edited-away message is soft-deleted
-- no column says so, the shape does -- and how its lineage is kept for
analytics, which reads the whole tree. Nothing else does: every other reader
is handed ``visible_path`` and never a message off it. Everything that
follows from that is here, as pure functions over a collection of messages
-- the store hands them over, this says what they mean.

**The visible path is the newest leaf's**, by the one order every message
has -- ``created_at``, then id. It is the whole rule, and it holds because an
edit or a regeneration always writes the newest message, and because a
conversation has one run at a time, so an older branch can never gain a
newer message than the one that put it aside. The one moment it does not
hold is a regeneration in flight, which has put the old answer aside before
writing anything: then the thread ends at what the run is extending
(``visible_path(extending=...)``).

The invariants, all of them refused loudly rather than worked around:

- one conversation's messages, with no two of the same id;
- every parent is present: a message whose parent is elsewhere is an orphan,
  and its branch cannot be read;
- no cycles, which is also what guarantees a root exists;
- the roles follow the one rule in ``may_follow``.

**A turn is a chain.** A root is a question. A question follows an answer, or
nothing. An answer follows a question, another answer, or a tool result -- a
turn may produce several messages, and with tools it will produce a call and a
result between them (``docs/specs/runs.md``). A tool message follows the
answer that made the call. So a turn is one question and everything the run
produced under it, and ``turn_start`` is what finds the question again from
anywhere inside it. The ``tool`` role is refused by this build where a message
is built; its rule is written here all the same, so that carrying it later
changes nothing about the shape of a conversation.

**More than one root is legal.** Editing the first question of a conversation
gives the new question the same parent as the old one -- which is nothing --
so a conversation gains a second root, and the visible path begins there.
That is the same as an edit anywhere else in the tree, and the only place it
looks different.

**The conversation is read once.** A ``ConversationTree`` is every message by
its id, in order, and every parent's children in order, built in one pass and
sorted once. Every function here builds one and asks it; a caller with more
than one question to ask builds it itself and asks it directly. Nothing walks
the conversation again for each message it looks at, so ten thousand siblings
are as cheap as ten.

**Reading rows is not reading a request.** ``tree_of_stored`` is the one door
for messages the **application** decoded out of what a store returned: it
checks them once, as stored
data -- anything wrong with them is ``StoredDataError``
(``robinauts.domain.reading_stored``) -- and hands back a ``ConversationTree``. Every
question asked of that tree afterwards can then fail for one reason only, and
it is a reason of the request's: an id it named that is not there
(``MessageNotFoundError``).

**There is one way to ask.** Every question about a conversation is a method
of ``ConversationTree``, and the only way to get one is ``tree_of`` or
``tree_of_stored``, both of which say which conversation they are reading and
check it before they answer. There are no loose functions that take messages
and answer half-checked: a question answered over messages nobody had checked
was a question that could cross conversations, and two ways of asking meant
two sets of rules.

``tree_of_stored`` is the door for rows and ``tree_of`` the door for
everything else. A conversation of ours that is no tree is a fault of the
deployment, answered and logged as one, not a 422 quoting our own ids back at
a browser. An id that a request named and that is simply not there stays a
``MessageNotFoundError`` in both: nothing is wrong with the rows.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from robinauts.domain import (
    InvalidMessageTreeError,
    Message,
    MessageNotFoundError,
    Role,
    checked_uuid,
    describe,
    reading_stored,
)

UNREADABLE = "a stored conversation is not a tree this build can read"
"""What anyone outside is told when our own rows are no conversation."""

_MAY_FOLLOW: Mapping[Role, frozenset[Role | None]] = {
    Role.USER: frozenset({None, Role.ASSISTANT}),
    Role.ASSISTANT: frozenset({Role.USER, Role.ASSISTANT, Role.TOOL}),
    Role.TOOL: frozenset({Role.ASSISTANT}),
}
"""What each role may hang under; ``None`` is "it is a root".

The whole rule of the shape of a conversation, in one table: only a question
is a root, a question follows an answer, an answer follows the question or
whatever the run has produced since, and a tool message follows the answer
that called the tool.
"""


def may_follow(role: Role, parent_role: Role | None) -> bool:
    """Whether a message of ``role`` may hang under one of ``parent_role``."""
    if not isinstance(role, Role) or not (parent_role is None or isinstance(parent_role, Role)):
        raise InvalidMessageTreeError(
            f"a message's role is a Role, not {describe(role)} under {describe(parent_role)}"
        )
    return parent_role in _MAY_FOLLOW[role]


def check_parent(role: Role, parent: Message | None) -> None:
    """``InvalidMessageTreeError`` unless ``role`` may hang under ``parent``.

    The one place the rule is applied: the tree's own check and the check a
    new message goes through are the same check, so they cannot part company.
    A tool message's parent must also have made calls for it to answer;
    which calls, and that it answers each once, is ``check_answers_calls``,
    asked of the whole message once there is one.
    """
    if parent is not None and not isinstance(parent, Message):
        raise InvalidMessageTreeError(f"a parent is a message, not {describe(parent)}")
    parent_role = None if parent is None else parent.role
    if not may_follow(role, parent_role):
        follows = "nothing" if parent_role is None else f"a message of role {parent_role.value!r}"
        raise InvalidMessageTreeError(f"a message of role {role.value!r} does not follow {follows}")
    if role is Role.TOOL and parent is not None and not parent.tool_calls:
        raise InvalidMessageTreeError("a tool message follows an answer that made tool calls")


def check_answers_calls(message: Message, parent: Message) -> None:
    """``InvalidMessageTreeError`` unless a tool message answers its parent's calls.

    **The results of one call batch are one tool message**
    (``docs/specs/conversations.md``): one result per call the parent made,
    each naming its call, none missing, none extra and none twice, so that
    the visible path holds every result and no two tool messages ever stand
    side by side as one replacing the other. A message of another role has
    no calls to answer and passes.

    Applied where the tree is built, as every rule of the shape is, and by the
    application before it writes a tool message -- the same check, so the two
    cannot part company.
    """
    if not isinstance(message, Message) or not isinstance(parent, Message):
        raise InvalidMessageTreeError(
            f"a tool message and its parent are messages, not {describe(message)}"
            f" under {describe(parent)}"
        )
    if message.role is not Role.TOOL:
        return
    if message.parent_id != parent.id:
        raise InvalidMessageTreeError(f"message {message.id} does not hang under {parent.id}")
    answered = [part.call_id for part in message.tool_results]
    made = [part.call_id for part in parent.tool_calls]
    if sorted(answered) != sorted(made):
        raise InvalidMessageTreeError(
            f"tool message {message.id} answers {len(answered)} call(s) and its parent"
            f" made {len(made)}; a tool message answers exactly the calls of its parent,"
            " once each"
        )


# --- the conversation, read once --------------------------------------------


@dataclass(frozen=True, slots=True)
class ConversationTree:
    """A conversation's messages, read once and checked once.

    **It is made of its messages and nothing else, and it is checked.**
    Everything else here is computed from them when it is built, so there is
    no index to hand in and none that can disagree with them; and every rule
    of this module -- one conversation, parents present, roles, no cycles --
    is applied in the constructor, so **an unchecked tree cannot exist**.
    ``tree_of`` is this by another name, and ``tree_of_stored`` is this with
    the refusals turned into ``StoredDataError``.
    """

    messages: tuple[Message, ...]
    """Every message, oldest first, ties broken by id."""
    conversation_id: uuid.UUID
    """Which conversation these are. Said, and never inferred."""
    at: Mapping[uuid.UUID, Message] = field(init=False, default_factory=dict)
    """Every message by its id. Read-only, as is everything here."""
    below: Mapping[uuid.UUID | None, tuple[Message, ...]] = field(init=False, default_factory=dict)
    """The children of each message, in order; the roots under ``None``."""

    def __post_init__(self) -> None:
        checked_uuid(self.conversation_id, "a conversation's id")
        at: dict[uuid.UUID, Message] = {}
        for message in self.messages:
            if not isinstance(message, Message):
                raise InvalidMessageTreeError(
                    f"a conversation holds messages, not {describe(message)}"
                )
            if message.id in at:
                raise InvalidMessageTreeError(f"two messages share the id {message.id}")
            at[message.id] = message
        ordered = tuple(sorted(at.values(), key=_order))
        below: dict[uuid.UUID | None, list[Message]] = {}
        for message in ordered:
            below.setdefault(message.parent_id, []).append(message)
        conversations = {message.conversation_id for message in ordered}
        if conversations - {self.conversation_id}:
            raise InvalidMessageTreeError(
                f"these messages do not belong to conversation {self.conversation_id}"
            )
        # Read-only in fact and not only in the docstring: a caller that
        # could put a message back into `below` could make a cycle, and a
        # walk of a cycle does not end.
        object.__setattr__(self, "messages", ordered)
        object.__setattr__(self, "at", MappingProxyType(at))
        object.__setattr__(
            self,
            "below",
            MappingProxyType({parent: tuple(kept) for parent, kept in below.items()}),
        )
        self._check_shape()

    def _check_shape(self) -> None:
        """Parents present, roles as ``may_follow`` says, tool messages answering
        their parent's calls, no cycles."""
        for message in self.messages:
            if message.parent_id is not None and message.parent_id not in self.at:
                raise InvalidMessageTreeError(
                    f"message {message.id} has no parent here: {message.parent_id}"
                )
            parent = None if message.parent_id is None else self.at[message.parent_id]
            check_parent(message.role, parent)
            if parent is not None:
                check_answers_calls(message, parent)
        # Walked once per message with the settled ones remembered, so a deep
        # conversation is not re-walked from every leaf.
        settled: set[uuid.UUID] = set()
        for message in self.messages:
            seen: list[uuid.UUID] = []
            walking: set[uuid.UUID] = set()
            current: Message | None = message
            while current is not None and current.id not in settled:
                if current.id in walking:
                    raise InvalidMessageTreeError(f"message {current.id} is its own ancestor")
                walking.add(current.id)
                seen.append(current.id)
                current = None if current.parent_id is None else self.at[current.parent_id]
            settled.update(seen)

    # --- looking one up ---

    def message_at(self, message_id: uuid.UUID) -> Message:
        """The message of that id; ``MessageNotFoundError`` if it is not here."""
        try:
            return self.at[message_id]
        except KeyError:
            raise MessageNotFoundError(f"no message {message_id} in this conversation") from None

    def leaves(self) -> tuple[Message, ...]:
        """The messages nothing hangs under: the end of each branch."""
        return tuple(message for message in self.messages if message.id not in self.below)

    # --- the visible path ---

    def path_to(self, leaf_id: uuid.UUID) -> tuple[Message, ...]:
        """The path from a root down to ``leaf_id``: the history an engine is sent."""
        path: list[Message] = []
        current: Message | None = self.message_at(leaf_id)
        while current is not None:
            path.append(current)
            if len(path) > len(self.messages):
                raise InvalidMessageTreeError(f"the path to {leaf_id} does not end")
            current = None if current.parent_id is None else self.at.get(current.parent_id)
        path.reverse()
        return tuple(path)

    def visible_leaf(self) -> Message | None:
        """The newest leaf: the end of the one thread a reader sees.

        ``None`` while the conversation is empty. The newest by the order
        every message has -- ``created_at``, then id -- and not the leaf of
        whichever branch began last: the message an edit or a regeneration
        wrote is the newest there is, so its branch is the visible one.
        """
        leaves = self.leaves()
        return leaves[-1] if leaves else None

    def visible_path(self, *, extending: uuid.UUID | None = None) -> tuple[Message, ...]:
        """The one thread a reader sees: a root down to the newest leaf, oldest first.

        Everything a reader other than analytics is handed. A message off
        this path was put aside by an edit or a regeneration and is kept in
        the tree for that reason alone.

        **While a run is in flight** the thread ends where the run is
        writing: ``extending`` is the message the run's next one will hang
        under -- the last it completed, or the question it answers
        (``core.resume_point``'s ``follows``) -- and the path is the path to
        it. A regeneration writes nothing until its first answer completes,
        so until then the newest leaf is still the answer being replaced, or
        whatever came after it, and the thread by the newest leaf would show
        what the run is about to put aside, with the new answer arriving
        below it. For every other turn the two agree: the question just
        asked, or the answer just completed, is the newest leaf. An
        ``extending`` that is not here is ``MessageNotFoundError``.
        """
        if extending is not None:
            return self.path_to(extending)
        leaf = self.visible_leaf()
        return () if leaf is None else self.path_to(leaf.id)

    # --- turns ---

    def turn_start(self, message_id: uuid.UUID) -> Message:
        """The question that began the turn ``message_id`` belongs to.

        A turn is a question and everything a run produced under it, which may
        be several messages. From anywhere inside one, this walks up to the
        question -- which is where a regeneration hangs, and which is the
        message a run answers.
        """
        current = self.message_at(message_id)
        seen: set[uuid.UUID] = set()
        while current.role is not Role.USER:
            if current.parent_id is None or current.id in seen:
                raise InvalidMessageTreeError(f"message {message_id} belongs to no turn")
            seen.add(current.id)
            current = self.message_at(current.parent_id)
        return current

    def parent_for_regenerate(self, message_id: uuid.UUID) -> uuid.UUID:
        """Where a regeneration of ``message_id`` attaches: under its own question.

        A turn may have produced several messages, so the new answer hangs
        where the whole turn hung -- under the question -- and not under
        whatever the old answer happened to follow. The earlier turn is kept
        in the tree, off the visible path.
        """
        message = self.message_at(message_id)
        if message.role is not Role.ASSISTANT:
            raise InvalidMessageTreeError(
                f"only an answer is regenerated, not a message of role {message.role.value!r}"
            )
        return self.turn_start(message_id).id

    def check_attachment(self, *, parent_id: uuid.UUID | None, role: Role) -> None:
        """Whether a new message of ``role`` may be added under ``parent_id``.

        What the application asks before it writes: the parent is one of
        **these** messages -- which are one conversation's -- and the roles
        follow the rule. A parent that belongs to somebody else's conversation
        is simply not here, and is answered as such. A new question with no
        parent is a conversation's first -- or, after an edit of that first
        question, another root beside it.
        """
        check_parent(role, None if parent_id is None else self.message_at(parent_id))


def tree_of(messages: Iterable[Message], *, conversation_id: uuid.UUID) -> ConversationTree:
    """The messages, read once and checked: every rule of this module.

    ``ConversationTree`` by another name, since the record checks itself.
    ``conversation_id`` is required and never inferred: inferring it would
    mean that a caller who passed the wrong messages -- somebody else's, or
    two conversations' -- was answered about whatever it passed instead of
    being refused, and that is the one mistake these rules exist to catch. An
    empty collection is a conversation nobody has written in yet, and passes.
    """
    return ConversationTree(tuple(messages), conversation_id=conversation_id)


def tree_of_stored(messages: Iterable[Message], *, conversation_id: uuid.UUID) -> ConversationTree:
    """The one door for rows: checked once, as stored data.

    Anything wrong with the messages themselves is a fault of the deployment
    and is raised as ``StoredDataError``. Afterwards the tree answers
    questions, and the only way one of those fails is an id the **request**
    named that is not there.
    """
    with reading_stored(UNREADABLE):
        return tree_of(messages, conversation_id=conversation_id)


def _order(message: Message) -> tuple[object, ...]:
    return (message.created_at, message.id.bytes)


def check_tree(messages: Iterable[Message], *, conversation_id: uuid.UUID) -> tuple[Message, ...]:
    """The messages of that conversation, checked, oldest first.

    ``tree_of(...).messages``, for a caller that wants the rules applied and
    has no further question to ask.
    """
    return tree_of(messages, conversation_id=conversation_id).messages

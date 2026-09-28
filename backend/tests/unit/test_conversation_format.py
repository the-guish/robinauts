# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The canonical encoding: what goes to a store and an export, and back."""

import uuid
from copy import deepcopy
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from conversations import CONVERSATION, RUN, answer, question
from robinauts.core import (
    EXTRAS,
    MAX_EXTRAS_BYTES,
    MAX_EXTRAS_DEPTH,
    instant,
    message_from_data,
    message_from_stored,
    message_to_data,
    part_from_data,
    part_to_data,
    provenance_to_data,
)
from robinauts.core import conversation_format as format_module
from robinauts.domain import (
    EARLIEST_YEAR,
    FORMAT_VERSION,
    LATEST_YEAR,
    MAX_PART_CHARS,
    MAX_PARTS,
    SUPPORTED_PART_KINDS,
    InvalidValueError,
    Message,
    PartKind,
    ReasoningPart,
    Role,
    StoredDataError,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    UnsupportedContentError,
    UnsupportedFormatError,
    text_parts,
)

CALL = ToolCallPart("toolu_01", "github__search", {"q": "robinauts", "limit": 3})
RESULT = ToolResultPart("toolu_01", "found 3", is_error=False)
BLOCKS = {"anthropic": {"thinking": [{"type": "thinking", "signature": "sig", "thinking": "hm"}]}}


def results(parent: Message, *parts: ToolResultPart) -> Message:
    """The tool message answering ``parent``'s calls."""
    return Message(
        id=uuid.uuid4(),
        conversation_id=CONVERSATION,
        parent_id=parent.id,
        role=Role.TOOL,
        parts=parts or (RESULT,),
        created_at=parent.created_at,
    )


LETTERED = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
"""An id with letters in it, so that a spelling by case can be told apart."""


def data_of(message: Message, **changes: object) -> dict[str, object]:
    """A message's data with something changed, the way a store might hold it."""
    return {**message_to_data(message), **changes}


PROVENANCE = {
    "agent": "assistant",
    "engine": "pydantic-ai",
    "model": "sonnet",
    "run_id": str(RUN),
}


# --- round trips ------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        question(),
        question("é中\U0001f600 \n\t"),
        question(parts=(TextPart("one"), ReasoningPart("two"), TextPart("three"))),
        question(parts=(TextPart(""),)),
        answer(question(), parts=(TextPart(""),), seconds=3),
        question(created_at=datetime(2026, 9, 21, 9, 0, tzinfo=timezone(timedelta(hours=-3)))),
        answer(question(), seconds=2.5),
        answer(question(), parts=(ReasoningPart("thinking"), TextPart("said"))),
        answer(question(), parts=(TextPart("Let me look."), CALL), extras=BLOCKS),
        answer(question(), parts=(CALL, ToolCallPart("toolu_02", "jira__find", {}))),
        results(answer(question(), parts=(CALL,))),
        results(
            answer(question(), parts=(CALL,)),
            ToolResultPart("toolu_01", "it broke: é\n", is_error=True),
        ),
        question(extras={"vendor": {"cache": "hit", "n": [1, 2.5, None, False]}}),
    ],
)
def test_a_message_survives_the_round_trip_unchanged(message: Message) -> None:
    assert message_from_data(message_to_data(message)) == message


def test_the_tool_parts_are_written_with_their_own_keys() -> None:
    asked = answer(question(), parts=(TextPart("Let me look."), CALL))
    assert message_to_data(asked)["parts"] == [
        {"kind": "text", "text": "Let me look."},
        {
            "kind": "tool_call",
            "call_id": "toolu_01",
            "name": "github__search",
            "arguments": {"q": "robinauts", "limit": 3},
        },
    ]
    assert message_to_data(results(asked))["parts"] == [
        {"kind": "tool_result", "call_id": "toolu_01", "text": "found 3", "is_error": False}
    ]
    assert message_to_data(results(asked))["role"] == "tool"
    # The arguments written out are a copy: editing the document edits no record.
    document = message_to_data(asked)
    document["parts"][1]["arguments"]["q"] = "changed"
    assert asked.tool_calls[0].arguments == {"q": "robinauts", "limit": 3}


def test_a_tool_part_read_back_is_checked_as_hard_as_one_built() -> None:
    assert part_from_data(
        {"kind": "tool_call", "call_id": "c", "name": "t", "arguments": {"a": {"b": None}}}
    ) == ToolCallPart("c", "t", {"a": {"b": None}})
    for broken in (
        {"kind": "tool_call", "call_id": "", "name": "t", "arguments": {}},
        {"kind": "tool_call", "call_id": "c", "name": "not a name", "arguments": {}},
        {"kind": "tool_call", "call_id": "c", "name": "t", "arguments": "q=x"},
        {"kind": "tool_call", "call_id": "c", "name": "t", "arguments": ["q"]},
        {"kind": "tool_call", "call_id": "c", "name": "t"},
        {"kind": "tool_result", "call_id": "c", "text": "ok"},
        {"kind": "tool_result", "call_id": "c", "text": "ok", "is_error": "no"},
        {"kind": "tool_result", "call_id": "c", "text": "ok", "is_error": 0},
        {"kind": "tool_result", "call_id": "c", "text": 7, "is_error": False},
        {"kind": "tool_result", "call_id": "c", "text": "ok", "is_error": False, "name": "t"},
    ):
        with pytest.raises(InvalidValueError):
            part_from_data(broken)


def test_extras_are_written_when_there_are_some_and_left_out_when_not() -> None:
    """A message with nothing of a vendor's is written as it always was."""
    plain = question("one")
    assert EXTRAS not in message_to_data(plain)
    kept = answer(question(), extras=BLOCKS)
    assert message_to_data(kept)[EXTRAS] == BLOCKS
    assert message_from_data(message_to_data(kept)).extras == BLOCKS
    # Written out as a copy, and read back as one.
    document = message_to_data(kept)
    document[EXTRAS]["anthropic"]["thinking"].clear()
    assert kept.extras == BLOCKS


def test_a_messages_content_is_written_inside_its_document() -> None:
    """One shape at the port: parts have no document of their own."""
    message = question(parts=(TextPart("one"), ReasoningPart("two")))
    assert message_to_data(message)["parts"] == [
        {"kind": "text", "text": "one"},
        {"kind": "reasoning", "text": "two"},
    ]
    assert message_from_data(message_to_data(message)).parts == message.parts


def test_a_part_survives_the_round_trip() -> None:
    for part in (TextPart("hello"), ReasoningPart("hmm")):
        assert part_from_data(part_to_data(part)) == part


SAMPLES = {
    PartKind.TEXT: {"kind": "text", "text": "some"},
    PartKind.REASONING: {"kind": "reasoning", "text": "some"},
    PartKind.TOOL_CALL: {"kind": "tool_call", "call_id": "c", "name": "t", "arguments": {"a": 1}},
    PartKind.TOOL_RESULT: {"kind": "tool_result", "call_id": "c", "text": "ok", "is_error": True},
}
"""One document per kind this build carries; the test below holds the two in step."""


def test_every_kind_this_build_carries_has_an_encoder_and_a_decoder() -> None:
    """A record added to the union and forgotten here stops, rather than
    being written out as whatever fields it shares with the ones above it."""
    assert set(SAMPLES) == SUPPORTED_PART_KINDS
    for kind, sample in SAMPLES.items():
        built = part_from_data(sample)
        assert built.kind is kind
        assert part_to_data(built) == sample

    class Later:
        """A kind somebody added without an encoding."""

        kind = PartKind.IMAGE
        text = "not to be written out as text"

    with pytest.raises(InvalidValueError, match="no encoding"):
        part_to_data(Later())


def test_the_encoding_is_plain_data_with_the_version_on_it() -> None:
    message = answer(question(), seconds=1)
    assert message_to_data(message) == {
        "format_version": FORMAT_VERSION,
        "id": str(message.id),
        "conversation_id": str(CONVERSATION),
        "parent_id": str(message.parent_id),
        "role": "assistant",
        "parts": [{"kind": "text", "text": "Someone who plays fair."}],
        "created_at": message.created_at.isoformat(),
        "channel": "web",
        "provenance": PROVENANCE,
    }
    assert message_to_data(question())["parent_id"] is None
    assert message_to_data(question())["provenance"] is None
    assert provenance_to_data(message.provenance) == PROVENANCE


# --- one instant, one spelling ----------------------------------------------


def test_two_spellings_of_one_instant_encode_identically() -> None:
    away = datetime(2026, 9, 21, 14, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    here = away.astimezone(UTC)
    assert away == here
    assert instant(away) == instant(here) == "2026-09-21T09:00:00+00:00"
    assert message_to_data(question(created_at=away))["created_at"] == (
        message_to_data(question(created_at=here))["created_at"]
    )


def test_a_time_is_written_in_utc_and_read_from_anywhere() -> None:
    away = datetime(2026, 9, 21, 9, 0, 0, 500, tzinfo=timezone(timedelta(hours=-3)))
    written = message_to_data(question(created_at=away))["created_at"]
    assert written == "2026-09-21T12:00:00.000500+00:00"
    assert message_from_data(data_of(question(), created_at="2026-09-21T12:00:00Z")).created_at == (
        datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    )
    read = message_from_data(message_to_data(question(created_at=away)))
    assert read.created_at == away
    assert instant(read.created_at) == written


# --- the version ------------------------------------------------------------


def test_data_of_a_version_above_this_build_is_refused() -> None:
    with pytest.raises(UnsupportedFormatError, match="reads up to"):
        message_from_data(data_of(question(), format_version=FORMAT_VERSION + 1))


@pytest.mark.parametrize("version", [-1, "1", 1.0, True, None])
def test_data_that_names_no_version_is_refused(version: object) -> None:
    with pytest.raises(UnsupportedFormatError, match="which version"):
        message_from_data(data_of(question(), format_version=version))


def test_data_with_no_version_at_all_is_refused() -> None:
    data = message_to_data(question())
    del data["format_version"]
    with pytest.raises(UnsupportedFormatError):
        message_from_data(data)


def test_an_older_version_is_read_through_its_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hook a version bump uses, proved with a version that never existed.

    Version 1 is the first, so there is nothing to upgrade from yet. This
    stands in a version 0 whose ``said`` is version 1's ``parts``, and shows
    that a build reads everything written before it.
    """

    def from_version_0(fields: dict[str, Any]) -> dict[str, Any]:
        lifted = dict(fields)
        lifted["parts"] = lifted.pop("said")
        lifted["format_version"] = 1
        return lifted

    monkeypatch.setattr(format_module, "_MESSAGE_UPGRADES", {0: from_version_0})
    message = question("one")
    old = data_of(message, format_version=0)
    old["said"] = old.pop("parts")
    assert message_from_data(old) == message


def test_an_upgrade_never_touches_what_it_was_given(monkeypatch: pytest.MonkeyPatch) -> None:
    """A store that read one row twice would otherwise get two answers."""

    def helps_itself(fields: dict[str, Any]) -> dict[str, Any]:
        fields["parts"] = fields.pop("said")
        fields["parts"][0]["text"] = "rewritten"
        fields["format_version"] = 1
        return fields

    monkeypatch.setattr(format_module, "_MESSAGE_UPGRADES", {0: helps_itself})
    old = data_of(question("one"), format_version=0)
    old["said"] = old.pop("parts")
    before = deepcopy(old)
    assert message_from_data(old).text == "rewritten"
    assert old == before


def test_an_upgrade_that_goes_past_this_build_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bound is checked after every step, not only before the first."""

    def overshoots(fields: dict[str, Any]) -> dict[str, Any]:
        lifted = dict(fields)
        lifted["parts"] = lifted.pop("said")
        lifted["format_version"] = FORMAT_VERSION + 5
        return lifted

    monkeypatch.setattr(format_module, "_MESSAGE_UPGRADES", {0: overshoots})
    old = data_of(question("one"), format_version=0)
    old["said"] = old.pop("parts")
    with pytest.raises(UnsupportedFormatError, match="went past"):
        message_from_data(old)


def test_a_version_with_no_upgrade_is_refused_rather_than_guessed_at() -> None:
    old = data_of(question(), format_version=0)
    with pytest.raises(UnsupportedFormatError):
        message_from_data(old)


def test_an_upgrade_that_does_not_move_the_version_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(format_module, "_MESSAGE_UPGRADES", {0: lambda fields: fields})
    with pytest.raises(UnsupportedFormatError, match="did not move it forward"):
        message_from_data(data_of(question(), format_version=0))


# --- kinds and roles this build does not carry ------------------------------


@pytest.mark.parametrize(
    "data",
    [
        {"kind": "image", "media_type": "image/png", "sha256": "ab" * 32},
        {"kind": "file", "filename": "notes.pdf", "media_type": "application/pdf"},
    ],
)
def test_a_kind_this_build_lacks_is_unsupported_whatever_it_carries(
    data: dict[str, object],
) -> None:
    """The kind is read first: keys that belong to a kind we have never built
    must not turn "not supported yet" into "malformed"."""
    with pytest.raises(UnsupportedContentError, match=f"{data['kind']}.*not supported yet"):
        part_from_data(data)


def test_a_message_of_the_tool_role_is_read_and_held_to_its_rules() -> None:
    asked = answer(question(), parts=(CALL,))
    assert message_from_data(message_to_data(results(asked))).role is Role.TOOL
    # The record's rules hold on the way in: a tool message holds results.
    with pytest.raises(InvalidValueError, match="tool results and nothing else"):
        message_from_data(data_of(question(), role="tool"))
    with pytest.raises(InvalidValueError, match="only an assistant message has provenance"):
        message_from_data(data_of(results(asked), provenance=PROVENANCE))


@pytest.mark.parametrize("kind", ["", "audio", "TEXT", 7, None])
def test_a_kind_of_no_version_of_the_format_is_refused(kind: object) -> None:
    with pytest.raises(InvalidValueError):
        part_from_data({"kind": kind, "text": "..."})


@pytest.mark.parametrize("role", ["system", "User", "", 7, None])
def test_a_role_of_no_version_of_the_format_is_refused(role: object) -> None:
    with pytest.raises(InvalidValueError):
        message_from_data(data_of(question(), role=role))


# --- data that is not this format -------------------------------------------


@pytest.mark.parametrize("data", ["a message", 7, None, [], {7: "keys"}])
def test_data_that_is_not_an_object_is_refused(data: object) -> None:
    with pytest.raises(InvalidValueError):
        message_from_data(data)
    with pytest.raises(InvalidValueError):
        part_from_data(data)


def test_a_key_that_is_not_there_is_refused_rather_than_guessed_at() -> None:
    """A document is written whole: reading a missing ``parent_id`` as "this
    is a root" would quietly reparent a message."""
    whole = message_to_data(answer(question()))
    for key in ("parent_id", "id", "role", "parts", "created_at", "channel", "provenance"):
        without = {name: value for name, value in whole.items() if name != key}
        with pytest.raises(InvalidValueError, match=f"written with .*{key}"):
            message_from_data(without)
    part = {"kind": "text", "text": "hi"}
    with pytest.raises(InvalidValueError, match="written with text"):
        part_from_data({"kind": "text"})
    assert part_from_data(part) == TextPart("hi")
    # And `extras` is the one a build is allowed not to know about.
    assert message_from_data(whole) == message_from_data({**whole})


def test_a_key_nobody_wrote_is_refused_rather_than_dropped() -> None:
    for read in (
        lambda: message_from_data(data_of(question(), tokens=12)),
        lambda: part_from_data({"kind": "text", "text": "hi", "signature": "opaque"}),
        lambda: message_from_data(
            data_of(answer(question()), provenance={**PROVENANCE, "cached": True})
        ),
    ):
        with pytest.raises(InvalidValueError, match="1 key"):
            read()


def test_a_newer_builds_additive_data_is_read_past_and_ignored() -> None:
    """The one key a build may meet, not understand, and read past. Every
    other unknown key is still refused, because a build that dropped a field
    it did not recognise would write the message back without it."""
    message = question("one")
    later = data_of(message, extras={"vendor": {"cache": "hit"}, "usage": {"input": 12}})
    later["parts"] = [{"kind": "text", "text": "one", "extras": {"signature": "abc"}}]
    read = message_from_data(later)
    # A message's extras are this build's to keep: they come back as they were.
    assert read == question("one", id=message.id, extras=later[EXTRAS])
    assert read.parts == message.parts
    # A part's are read past, and this build writes none back.
    assert not hasattr(read.parts[0], "extras")
    assert EXTRAS not in message_to_data(read)["parts"][0]
    assert message_to_data(read)[EXTRAS] == later[EXTRAS]


def test_extras_are_an_object_and_a_bounded_one() -> None:
    for broken in ("a string", 7, ["a", "list"], {"deep": {"a": float("nan")}}):
        with pytest.raises(InvalidValueError):
            message_from_data(data_of(question(), extras=broken))
        with pytest.raises(InvalidValueError):
            part_from_data({"kind": "text", "text": "hi", "extras": broken})
    oversize = {"vendor": "x" * (MAX_EXTRAS_BYTES + 1)}
    with pytest.raises(InvalidValueError, match="at most"):
        message_from_data(data_of(question(), extras=oversize))
    with pytest.raises(InvalidValueError, match="at most"):
        part_from_data({"kind": "text", "text": "hi", "extras": oversize})
    empty = question("one")
    assert message_from_data(data_of(empty, extras={})) == empty


def test_extras_are_bounded_in_shape_before_they_are_measured() -> None:
    """A deep enough object is a RecursionError out of the encoder, which is
    neither a refusal nor a message anyone can act on."""
    deep: dict[str, object] = {}
    inside = deep
    for _ in range(20_000):
        inside["in"] = {}
        inside = inside["in"]  # type: ignore[assignment]
    with pytest.raises(InvalidValueError, match="nests at most"):
        message_from_data(data_of(question(), extras=deep))
    with pytest.raises(InvalidValueError, match="nests at most"):
        part_from_data({"kind": "text", "text": "hi", "extras": deep})

    wide = {"many": [{"a": step} for step in range(50_000)]}
    with pytest.raises(InvalidValueError, match="holds at most"):
        message_from_data(data_of(question(), extras=wide))

    at_the_bound: dict[str, object] = {}
    inside = at_the_bound
    for _ in range(MAX_EXTRAS_DEPTH - 2):
        inside["in"] = {}
        inside = inside["in"]  # type: ignore[assignment]
    fits = question("one")
    assert message_from_data(data_of(fits, extras=at_the_bound)) == question(
        "one", id=fits.id, extras=at_the_bound
    )


def test_the_keys_of_extras_are_names_at_every_depth() -> None:
    """json would coerce a number to a string and write something back that
    is not what it was given."""
    for broken in ({1: "x"}, {"a": {1: "x"}}, {"a": [{"b": {2: "x"}}]}):
        with pytest.raises(InvalidValueError, match="are names"):
            message_from_data(data_of(question(), extras=broken))
        with pytest.raises(InvalidValueError, match="are names"):
            part_from_data({"kind": "text", "text": "hi", "extras": broken})


def test_the_text_in_extras_is_storable_at_every_depth() -> None:
    """A NUL cannot be stored in a jsonb column and a lone surrogate cannot
    be encoded at all -- and neither may escape a decoder as something other
    than a refusal."""
    for broken in (
        {"note": "a\x00b"},
        {"note": "a\ud800b"},
        {"a": {"b": ["deep", "\udfff"]}},
        {"a\x00key": "fine"},
    ):
        with pytest.raises(InvalidValueError, match="storable"):
            message_from_data(data_of(question(), extras=broken))
        with pytest.raises(InvalidValueError, match="storable"):
            part_from_data({"kind": "text", "text": "hi", "extras": broken})


def test_the_extras_bound_is_the_same_size_in_every_script() -> None:
    """Measured as the UTF-8 it would really be written as, not as escapes."""
    outside_ascii = {"note": "\u4e2d" * (MAX_EXTRAS_BYTES // 3)}
    with pytest.raises(InvalidValueError, match="bytes written out"):
        message_from_data(data_of(question(), extras=outside_ascii))
    assert message_to_data(
        message_from_data(data_of(question("one"), extras={"note": "\u4e2d" * 100}))
    )


def test_a_key_is_counted_and_never_repeated() -> None:
    """A key is as much the sender's text as a value is."""
    secret = "a-password-somebody-typed-in-the-wrong-box"
    with pytest.raises(InvalidValueError) as refused:
        message_from_data(data_of(question(), **{secret: 1, "x" * 10_000: 2}))
    assert secret not in str(refused.value)
    assert "2 key(s)" in str(refused.value)
    assert len(str(refused.value)) < 300


def test_a_message_holds_between_one_part_and_the_bound() -> None:
    with pytest.raises(InvalidValueError, match="at least one part"):
        message_from_data(data_of(question(), parts=[]))
    too_many = [{"kind": "text", "text": "x"}] * (MAX_PARTS + 1)
    with pytest.raises(InvalidValueError, match=f"at most {MAX_PARTS} parts"):
        message_from_data(data_of(question(), parts=too_many))


@pytest.mark.parametrize(
    "changes",
    [
        {"id": LETTERED.upper()},
        {"id": "{" + LETTERED + "}"},
        {"id": LETTERED.replace("-", "")},
        {"id": "urn:uuid:" + LETTERED},
        {"id": "not a uuid"},
        {"id": 7},
        {"id": None},
        {"parent_id": "not a uuid"},
        {"created_at": "2026-09-21T09:00:00"},
        {"created_at": "yesterday"},
        {"created_at": 7},
        {"channel": "slack"},
        {"parts": "hello"},
        {"parts": [{"kind": "text", "text": 7}]},
        {"parts": [{"kind": "text", "text": "a" * (MAX_PART_CHARS + 1)}]},
        {"parts": [{"kind": "text", "text": "a\u0000b"}]},
        {"parts": [{"kind": "text"}]},
    ],
)
def test_a_field_of_the_wrong_kind_is_refused(changes: dict[str, object]) -> None:
    with pytest.raises(InvalidValueError):
        message_from_data(data_of(question(), **changes))


def test_a_refusal_never_repeats_the_value_it_refused() -> None:
    secret = "a-password-somebody-typed-in-the-wrong-box"
    for changes in (
        {"id": secret},
        {"created_at": secret},
        {"role": secret},
        {"channel": secret},
        {"parts": [{"kind": "text", "text": secret + "\u0000"}]},
        {"parts": secret},
    ):
        with pytest.raises(InvalidValueError) as refused:
            message_from_data(data_of(question(), **changes))
        assert secret not in str(refused.value)


@pytest.mark.parametrize(
    "provenance",
    [
        {**PROVENANCE, "engine": "openai"},
        {**PROVENANCE, "run_id": "nope"},
        {**PROVENANCE, "agent": "An Agent"},
        {"agent": "assistant", "engine": "pydantic-ai", "run_id": str(RUN)},
        "assistant",
    ],
)
def test_provenance_that_says_nothing_usable_is_refused(provenance: object) -> None:
    with pytest.raises(InvalidValueError):
        message_from_data(data_of(answer(question()), provenance=provenance))


def test_an_answer_needs_its_provenance_and_a_question_may_not_have_it() -> None:
    with pytest.raises(InvalidValueError):
        message_from_data(data_of(answer(question()), provenance=None))
    with pytest.raises(InvalidValueError):
        message_from_data(data_of(question(), provenance=PROVENANCE))


# --- reading our own rows ---------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        {"format_version": FORMAT_VERSION + 1},
        "not a message",
        None,
    ],
)
def test_a_row_this_build_cannot_read_is_not_the_requests_fault(data: object) -> None:
    with pytest.raises(StoredDataError) as refused:
        message_from_stored(data)
    assert refused.value.__cause__ is not None


def test_stored_content_of_a_kind_this_build_lacks_is_a_fault_of_ours() -> None:
    stored = data_of(question(), parts=[{"kind": "image", "sha256": "ab"}])
    with pytest.raises(StoredDataError):
        message_from_stored(stored)
    # On the request path the same content is the request's business.
    with pytest.raises(UnsupportedContentError):
        message_from_data(stored)


def test_a_row_that_can_be_read_is_read() -> None:
    message = answer(question())
    assert message_from_stored(message_to_data(message)) == message


# --- making content out of text ---------------------------------------------


def test_a_long_answer_is_carried_in_several_parts_and_never_cut() -> None:
    text = "a" * (MAX_PART_CHARS + 10)
    parts = text_parts(text)
    assert len(parts) == 2
    assert "".join(part.text for part in parts) == text
    assert all(len(part.text) <= MAX_PART_CHARS for part in parts)


def test_text_that_fits_is_one_part_and_nothing_is_no_part_at_all() -> None:
    assert text_parts("hello") == (TextPart("hello"),)
    assert text_parts("") == (TextPart(""),)


def test_a_split_falls_between_characters() -> None:
    text = "\U0001f600" * (MAX_PART_CHARS + 1)
    parts = text_parts(text)
    assert "".join(part.text for part in parts) == text
    for part in parts:
        part.text.encode("utf-8")


def test_more_text_than_a_message_can_hold_is_refused_rather_than_truncated() -> None:
    with pytest.raises(InvalidValueError, match="at most"):
        text_parts("a" * (MAX_PARTS * MAX_PART_CHARS + 1))
    with pytest.raises(InvalidValueError):
        text_parts(7)  # type: ignore[arg-type]


# --- encoding refuses what is not a message ---------------------------------


@pytest.mark.parametrize("value", ["hello", 7, None, {"kind": "text", "text": "hi"}])
def test_only_the_formats_own_records_are_encoded(value: object) -> None:
    with pytest.raises(InvalidValueError):
        part_to_data(value)
    with pytest.raises(InvalidValueError):
        message_to_data(value)
    with pytest.raises(InvalidValueError):
        provenance_to_data(value)


# --- times that can be written back -----------------------------------------


@pytest.mark.parametrize(
    "written",
    [
        "0001-01-01T00:00:00+05:00",
        "1969-12-31T23:59:59+00:00",
        "9999-12-31T23:59:59-05:00",
        "9999-01-01T00:00:00+00:00",
    ],
)
def test_a_time_no_offset_of_it_could_be_written_back_is_refused(written: str) -> None:
    """Nothing un-encodable gets in: turning one of these into UTC overflows
    what a datetime can hold, and a record that cannot be written back is a
    record that must not be read in."""
    with pytest.raises(InvalidValueError, match="between the years"):
        message_from_data(data_of(question(), created_at=written))


@pytest.mark.parametrize("year", [EARLIEST_YEAR, LATEST_YEAR])
def test_the_ends_of_the_window_are_read_and_written(year: int) -> None:
    when = datetime(year, 6, 1, 12, 0, tzinfo=UTC)
    assert message_from_data(data_of(question(), created_at=instant(when))).created_at == when


@pytest.mark.parametrize(
    "written",
    [
        "1970-01-01T00:00:00+14:00",
        "1970-01-01T09:00:00+14:00",
        "9998-12-31T23:00:00-14:00",
    ],
)
def test_a_time_inside_the_window_until_it_is_utc_is_refused(written: str) -> None:
    """The window is on the UTC value, because that is what is written: a
    build that wrote one of these could not read it back."""
    with pytest.raises(InvalidValueError, match="once it is UTC"):
        message_from_data(data_of(question(), created_at=written))
    with pytest.raises(InvalidValueError, match="once it is UTC"):
        instant(datetime.fromisoformat(written))


@pytest.mark.parametrize(
    "written",
    [
        "1970-01-01T00:00:00+00:00",
        "1970-01-01T14:00:00+14:00",
        "9998-12-31T09:59:00-14:00",
        "2026-09-21T09:00:00+05:30",
        "2026-09-21T09:00:00Z",
    ],
)
def test_every_time_that_is_accepted_survives_the_round_trip(written: str) -> None:
    message = message_from_data(data_of(question(), created_at=written))
    again = message_from_data(message_to_data(message))
    assert again == message
    assert again.created_at == datetime.fromisoformat(written)
    assert message_to_data(again)["created_at"] == message_to_data(message)["created_at"]


def test_a_time_outside_the_window_is_refused_rather_than_overflowing() -> None:
    far = datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=5)))
    with pytest.raises(InvalidValueError, match="between the years"):
        instant(far)


def test_a_message_of_another_conversation_is_read_as_it_was_written() -> None:
    other = uuid.uuid4()
    message = question(conversation_id=other)
    assert message_from_data(message_to_data(message)).conversation_id == other

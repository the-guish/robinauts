# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The records of the conversation format, and what they refuse to be."""

import time
import uuid
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from types import MappingProxyType

import pytest

from conversations import (
    AGENT,
    CONVERSATION,
    MODEL,
    OWNER,
    RUN,
    answer,
    conversation,
    provenance,
    question,
)
from robinauts.domain import (
    EARLIEST_YEAR,
    LATEST_YEAR,
    MAX_CALL_ID_CHARS,
    MAX_EXTRAS_BYTES,
    MAX_PART_CHARS,
    MAX_PARTS,
    MAX_TITLE_CHARS,
    MAX_TOOL_NAME_CHARS,
    NO_LONGER_OFFERED,
    SUPPORTED_PART_KINDS,
    SUPPORTED_ROLES,
    Channel,
    Conversation,
    Engine,
    InvalidValueError,
    Message,
    MessagePart,
    PartKind,
    ReasoningPart,
    Role,
    TextPart,
    ToolCallPart,
    ToolDefinition,
    ToolResultPart,
    UnsupportedContentError,
    check_supported,
    check_supported_role,
    checked_call_id,
    checked_config_id,
    checked_fragment,
    checked_parts,
    checked_tool_name,
    clean_text,
    describe,
    is_config_id,
    is_tool_name,
    kept_parts,
    tools_for_request,
    unanswered_calls,
)

NAIVE = datetime(2026, 9, 21, 9, 0)


# --- the ids an operator writes in the configuration ------------------------


@pytest.mark.parametrize("value", ["assistant", "a", "gpt-4o", "claude_sonnet", "a" * 40, "0"])
def test_a_config_id_is_a_lower_case_name(value: str) -> None:
    assert is_config_id(value)
    assert checked_config_id(value, "an agent's id") == value


@pytest.mark.parametrize(
    "value", ["", "Assistant", "-leading", "_leading", "a" * 41, "with space", "eh?", 7, None]
)
def test_anything_else_is_no_config_id(value: object) -> None:
    assert not is_config_id(value)
    with pytest.raises(InvalidValueError):
        checked_config_id(value, "an agent's id")


def test_the_engines_are_the_two_the_configuration_names() -> None:
    assert {engine.value for engine in Engine} == {"langgraph", "pydantic-ai"}


# --- parts ------------------------------------------------------------------


def test_a_part_carries_the_kind_it_is_stored_under() -> None:
    assert TextPart.kind is PartKind.TEXT
    assert ReasoningPart.kind is PartKind.REASONING
    assert TextPart("hello").text == "hello"
    # The encoding is core's, in one place and in both directions: a record
    # here holds and checks, and knows nothing about how it is written down.
    assert not hasattr(TextPart("hello"), "to_data")
    assert not hasattr(provenance(), "to_data")


def test_a_part_holds_bounded_text_and_nothing_else() -> None:
    assert TextPart("a" * MAX_PART_CHARS).text
    with pytest.raises(InvalidValueError):
        TextPart("a" * (MAX_PART_CHARS + 1))
    with pytest.raises(InvalidValueError):
        TextPart(7)  # type: ignore[arg-type]
    with pytest.raises(InvalidValueError):
        ReasoningPart(None)  # type: ignore[arg-type]


def test_the_bound_on_a_part_is_longer_than_a_model_can_answer() -> None:
    """A model asked for its longest answer is paid for either way.

    Sixty-four thousand output tokens is a quarter of a million characters,
    and an answer over the bound is carried in several parts by
    ``domain.text_parts``, never cut.
    """
    assert MAX_PART_CHARS >= 1_000_000
    assert MAX_PARTS * MAX_PART_CHARS >= 64_000_000


@pytest.mark.parametrize("text", ["a\x00b", "a\ud800b", "\udfff"])
def test_stored_text_is_text_a_database_and_utf_8_can_hold(text: str) -> None:
    with pytest.raises(InvalidValueError):
        TextPart(text)
    with pytest.raises(InvalidValueError):
        conversation(title=text)
    # And the repair is what the layer that meets such text applies first.
    assert TextPart(clean_text(text)).text == clean_text(text)
    clean_text(text).encode("utf-8")


def test_cleaning_joins_a_pair_that_arrived_in_two_pieces() -> None:
    """The case it exists for: a provider split one character across two
    deltas, and replacing both halves would lose a character that did
    arrive."""
    assert clean_text("\ud83d" + "\ude00") == "\U0001f600"
    assert clean_text("a" + "\ud83d" + "\ude00" + "b") == "a\U0001f600b"
    assert clean_text("\U0001f600") == "\U0001f600", "what arrived whole is untouched"


@pytest.mark.parametrize(
    ("given", "cleaned"),
    [
        ("a\x00b", "ab"),
        ("a\ud800b", "a\ufffdb"),
        ("\udfff", "\ufffd"),
        ("\ude00\ud83d", "\ufffd\ufffd"),
        ("caf\u00e9 \U0001f600", "caf\u00e9 \U0001f600"),
        ("", ""),
    ],
)
def test_cleaning_replaces_what_is_left_and_drops_what_cannot_be_stored(
    given: str, cleaned: str
) -> None:
    assert clean_text(given) == cleaned
    clean_text(given).encode("utf-8")


def test_cleaning_a_megabyte_is_linear_enough_to_be_dull() -> None:
    started = time.monotonic()
    cleaned = clean_text(("a" * 999_999) + "\ud83d\ude00")
    assert cleaned.endswith("\U0001f600")
    assert time.monotonic() - started < 1.0


def test_cleaning_takes_text() -> None:
    with pytest.raises(InvalidValueError):
        clean_text(7)  # type: ignore[arg-type]


def test_a_fragment_of_a_message_may_be_half_a_character() -> None:
    """A provider splits its answer where it likes; a delta carries it all."""
    assert checked_fragment("a\ud800", "a delta's text", 10) == "a\ud800"
    with pytest.raises(InvalidValueError):
        checked_fragment("aaa", "a delta's text", 2)


def test_a_refusal_never_repeats_what_it_refused() -> None:
    secret = "a-password-somebody-typed-in-the-wrong-box"
    for build in (
        lambda: TextPart(secret + "\x00"),
        lambda: question(parts=(secret,)),
        lambda: question(role=secret),
        lambda: conversation(title=secret + "\n" + secret),
        lambda: conversation(agent=secret),
        lambda: question(id=secret),
    ):
        with pytest.raises(InvalidValueError) as refused:
            build()
        assert secret not in str(refused.value)


def test_what_a_refusal_says_instead() -> None:
    assert describe("hello") == "text of 5 characters"
    assert describe(None) == "nothing"
    assert describe(7) == "an int"
    assert describe([1, 2]) == "a list of 2"
    assert describe(b"ab") == "2 bytes"


def test_the_format_names_every_kind_and_carries_four_of_them() -> None:
    assert {kind.value for kind in PartKind} == {
        "text",
        "image",
        "file",
        "reasoning",
        "tool_call",
        "tool_result",
    }
    assert SUPPORTED_PART_KINDS == {
        PartKind.TEXT,
        PartKind.REASONING,
        PartKind.TOOL_CALL,
        PartKind.TOOL_RESULT,
    }
    assert isinstance(TextPart(""), MessagePart)
    assert isinstance(ReasoningPart(""), MessagePart)
    assert isinstance(call(), MessagePart)
    assert isinstance(result(), MessagePart)
    for kind in SUPPORTED_PART_KINDS:
        assert check_supported(kind) is kind


@pytest.mark.parametrize("kind", [PartKind.IMAGE, PartKind.FILE])
def test_a_kind_this_build_does_not_carry_is_refused_by_name(kind: PartKind) -> None:
    with pytest.raises(UnsupportedContentError, match=f"{kind.value}.*not supported yet"):
        check_supported(kind)


def test_the_roles_are_the_three_a_turn_with_tools_has() -> None:
    assert {role.value for role in Role} == {"user", "assistant", "tool"}
    assert SUPPORTED_ROLES == {Role.USER, Role.ASSISTANT, Role.TOOL}
    for role in Role:
        assert check_supported_role(role) is role


# --- tool parts ---------------------------------------------------------------


def call(
    call_id: str = "toolu_01", name: str = "github__search", **arguments: object
) -> ToolCallPart:
    return ToolCallPart(call_id=call_id, name=name, arguments=arguments or {"q": "robinauts"})


def result(call_id: str = "toolu_01", text: str = "found 3", **changes: object) -> ToolResultPart:
    fields: dict[str, object] = {"call_id": call_id, "text": text}
    fields.update(changes)
    return ToolResultPart(**fields)  # type: ignore[arg-type]


def test_a_tool_call_names_the_call_the_tool_and_its_arguments() -> None:
    made = call("toolu_01", "github__search", q="x", limit=3)
    assert (made.call_id, made.name, made.arguments) == (
        "toolu_01",
        "github__search",
        {"q": "x", "limit": 3},
    )
    assert made.kind is PartKind.TOOL_CALL
    assert ToolCallPart("c", "t").arguments == {}


def test_a_tool_calls_arguments_are_a_copy_of_plain_data() -> None:
    written = {"nested": {"list": [1, "two", None, True]}}
    made = ToolCallPart("c", "t", written)
    written["nested"]["list"].append("later")  # type: ignore[index]
    assert made.arguments == {"nested": {"list": [1, "two", None, True]}}
    assert isinstance(made.arguments, dict)
    # A mapping of another kind and a tuple are taken, and kept as dict and list.
    read_only = MappingProxyType({"inner": MappingProxyType({"pair": (1, 2)})})
    assert ToolCallPart("c", "t", read_only).arguments == {"inner": {"pair": [1, 2]}}
    assert isinstance(ToolCallPart("c", "t", read_only).arguments["inner"], dict)


@pytest.mark.parametrize(
    "arguments",
    ["q=x", ["a", "list"], 7, None, {"a": object()}, {7: "keys"}, {"a": float("nan")}],
)
def test_a_tool_calls_arguments_are_an_object_of_plain_data(arguments: object) -> None:
    with pytest.raises(InvalidValueError):
        ToolCallPart("c", "t", arguments)  # type: ignore[arg-type]


def test_a_tool_calls_arguments_are_bounded_as_extras_are() -> None:
    with pytest.raises(InvalidValueError, match="at most"):
        ToolCallPart("c", "t", {"big": "x" * MAX_EXTRAS_BYTES})
    with pytest.raises(InvalidValueError, match="storable"):
        ToolCallPart("c", "t", {"a": "\x00"})


@pytest.mark.parametrize("name", ["search", "github__search", "a-b_c9", "x" * MAX_TOOL_NAME_CHARS])
def test_a_tool_name_is_what_the_vendors_accept(name: str) -> None:
    assert is_tool_name(name)
    assert checked_tool_name(name, "a tool's name") == name
    assert ToolCallPart("c", name).name == name


@pytest.mark.parametrize(
    "name", ["", "with space", "x" * (MAX_TOOL_NAME_CHARS + 1), "é", "a.b", 7, None]
)
def test_anything_else_is_no_tool_name(name: object) -> None:
    assert not is_tool_name(name)
    with pytest.raises(InvalidValueError):
        checked_tool_name(name, "a tool's name")
    with pytest.raises(InvalidValueError):
        ToolCallPart("c", name)  # type: ignore[arg-type]


@pytest.mark.parametrize("call_id", ["toolu_01A09q90", "call_abc", "1", "x" * MAX_CALL_ID_CHARS])
def test_a_call_id_is_one_word_of_printable_text(call_id: str) -> None:
    assert checked_call_id(call_id, "a call's id") == call_id
    assert result(call_id).call_id == call_id


@pytest.mark.parametrize(
    "call_id", ["", " ", "two words", "a\nb", "x" * (MAX_CALL_ID_CHARS + 1), 7, None]
)
def test_anything_else_is_no_call_id(call_id: object) -> None:
    with pytest.raises(InvalidValueError):
        checked_call_id(call_id, "a call's id")
    with pytest.raises(InvalidValueError):
        ToolCallPart(call_id, "t")  # type: ignore[arg-type]
    with pytest.raises(InvalidValueError):
        ToolResultPart(call_id, "found")  # type: ignore[arg-type]


def test_a_tool_result_answers_one_call_with_text_and_says_if_it_went_wrong() -> None:
    assert result().is_error is False
    assert result(is_error=True).is_error is True
    assert result().kind is PartKind.TOOL_RESULT
    assert ToolResultPart("c", "a" * MAX_PART_CHARS).text
    with pytest.raises(InvalidValueError):
        ToolResultPart("c", "a" * (MAX_PART_CHARS + 1))
    with pytest.raises(InvalidValueError):
        ToolResultPart("c", "a\x00b")
    with pytest.raises(InvalidValueError, match="yes or no"):
        result(is_error="yes")
    with pytest.raises(InvalidValueError, match="yes or no"):
        result(is_error=1)


def test_the_tool_parts_are_frozen() -> None:
    with pytest.raises(FrozenInstanceError):
        call().name = "other"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result().text = "other"  # type: ignore[misc]


# --- provenance -------------------------------------------------------------


def test_provenance_names_the_agent_the_engine_the_model_and_the_run() -> None:
    said = provenance()
    assert (said.agent, said.engine, said.model, said.run_id) == (
        AGENT,
        Engine.PYDANTIC_AI,
        MODEL,
        RUN,
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"agent": "No Such Agent"},
        {"agent": ""},
        {"model": "Sonnet 5"},
        {"engine": "pydantic-ai"},
        {"engine": None},
        {"run_id": str(RUN)},
        {"run_id": None},
    ],
)
def test_provenance_refuses_what_it_could_not_show(changes: dict[str, object]) -> None:
    with pytest.raises(InvalidValueError):
        provenance(**changes)


# --- messages ---------------------------------------------------------------


def test_a_question_has_no_provenance_and_an_answer_must_have_one() -> None:
    assert question().provenance is None
    assert answer(question()).provenance == provenance()


def test_an_answer_without_provenance_is_refused() -> None:
    with pytest.raises(InvalidValueError, match="agent, engine, model and run"):
        answer(question(), provenance=None)


def test_a_question_with_provenance_is_refused() -> None:
    with pytest.raises(InvalidValueError, match="only an assistant message"):
        question(provenance=provenance())


def test_what_this_version_keeps_of_an_answer() -> None:
    """Reasoning is dropped rather than refused, and a message always has
    content: a model that said nothing answered with nothing, which is a
    thing a conversation should record rather than skip. A tool call is
    kept; a tool result is an engine that executed a tool, and refused."""
    assert kept_parts((TextPart("Hi"), ReasoningPart("thinking"))) == (TextPart("Hi"),)
    assert kept_parts((ReasoningPart("thinking"),)) == (TextPart(""),)
    assert kept_parts((TextPart(""),)) == (TextPart(""),)
    assert kept_parts([TextPart("a"), TextPart("b")]) == (TextPart("a"), TextPart("b"))
    assert kept_parts((ReasoningPart("hmm"), TextPart("Let me look."), call())) == (
        TextPart("Let me look."),
        call(),
    )
    assert kept_parts((call(),)) == (call(),)
    with pytest.raises(InvalidValueError, match="never with a result"):
        kept_parts((TextPart("hi"), result()))
    with pytest.raises(InvalidValueError):
        kept_parts(())
    with pytest.raises(InvalidValueError):
        kept_parts(("not a part",))


def test_an_answer_of_nothing_is_a_message_like_any_other() -> None:
    said = answer(question(), parts=kept_parts((ReasoningPart("thinking"),)))
    assert said.text == ""
    assert said.parts == (TextPart(""),)


@pytest.mark.parametrize(
    "when",
    [
        datetime(1970, 1, 1, tzinfo=timezone(timedelta(hours=14))),
        datetime(1969, 12, 31, 23, 59, tzinfo=UTC),
        datetime(9999, 1, 1, tzinfo=UTC),
        datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=5))),
    ],
)
def test_a_record_holds_no_time_that_could_not_be_written_back(when: datetime) -> None:
    """The window is on the record, not only on the encoder: a row that
    cannot be encoded is a row nothing can read back."""
    for build in (
        lambda: question(created_at=when),
        lambda: conversation(created_at=when),
        lambda: conversation(updated_at=when),
    ):
        with pytest.raises(InvalidValueError, match="once it is UTC"):
            build()


@pytest.mark.parametrize(
    "when",
    [
        datetime(EARLIEST_YEAR, 1, 1, tzinfo=UTC),
        datetime(EARLIEST_YEAR, 1, 1, 14, tzinfo=timezone(timedelta(hours=14))),
        datetime(LATEST_YEAR, 12, 31, tzinfo=UTC),
    ],
)
def test_the_ends_of_the_window_are_records_like_any_other(when: datetime) -> None:
    assert question(created_at=when).created_at == when


def test_the_rule_about_parts_is_one_rule_anything_reading_a_message_can_ask() -> None:
    assert checked_parts([TextPart("a")]) == (TextPart("a"),)
    with pytest.raises(InvalidValueError, match="at least one part"):
        checked_parts([])


def test_a_message_holds_at_least_one_part_and_at_most_the_bound() -> None:
    with pytest.raises(InvalidValueError, match="at least one part"):
        question(parts=())
    with pytest.raises(InvalidValueError, match=f"at most {MAX_PARTS} parts"):
        question(parts=tuple(TextPart("x") for _ in range(MAX_PARTS + 1)))
    assert len(question(parts=tuple(TextPart("x") for _ in range(MAX_PARTS))).parts) == MAX_PARTS


@pytest.mark.parametrize("parts", ["hello", 7, ("hello",), (None,), ({"kind": "text"},)])
def test_a_message_holds_only_the_content_the_format_carries(parts: object) -> None:
    with pytest.raises(InvalidValueError):
        question(parts=parts)


def test_parts_are_kept_as_a_tuple_whatever_they_arrived_in() -> None:
    assert question(parts=[TextPart("a"), TextPart("b")]).parts == (TextPart("a"), TextPart("b"))


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "not a uuid"},
        {"conversation_id": str(CONVERSATION)},
        {"parent_id": "not a uuid"},
        {"role": "user"},
        {"role": None},
        {"channel": "web"},
        {"created_at": NAIVE},
        {"created_at": "2026-09-21T09:00:00+00:00"},
    ],
)
def test_a_message_refuses_a_field_of_the_wrong_kind(changes: dict[str, object]) -> None:
    with pytest.raises(InvalidValueError):
        question(**changes)


def test_a_message_is_not_its_own_parent() -> None:
    message_id = uuid.uuid4()
    with pytest.raises(InvalidValueError, match="its own parent"):
        question(id=message_id, parent=message_id)


def tool_message(parent: Message, *results: ToolResultPart, **changes: object) -> Message:
    fields: dict[str, object] = {
        "id": uuid.uuid4(),
        "conversation_id": CONVERSATION,
        "parent_id": parent.id,
        "role": Role.TOOL,
        "parts": results or (result(),),
        "created_at": parent.created_at + timedelta(seconds=1),
        "channel": Channel.WEB,
    }
    fields.update(changes)
    return Message(**fields)  # type: ignore[arg-type]


def test_a_tool_message_holds_results_and_nothing_else() -> None:
    asked = answer(question(), parts=(call(),))
    results = tool_message(asked, result("toolu_01"), result("toolu_02", is_error=True))
    assert results.role is Role.TOOL
    assert results.tool_results == (result("toolu_01"), result("toolu_02", is_error=True))
    assert results.tool_calls == ()
    assert results.text == ""
    assert results.provenance is None
    for parts in ((TextPart("hi"),), (result(), TextPart("")), (call(),), (ReasoningPart("x"),)):
        with pytest.raises(InvalidValueError, match="tool results and nothing else"):
            tool_message(asked, *parts)  # type: ignore[arg-type]
    with pytest.raises(InvalidValueError, match="only an assistant message has provenance"):
        tool_message(asked, provenance=provenance())


def test_an_answer_holds_its_calls_and_never_a_result() -> None:
    asked = answer(question(), parts=(TextPart("Let me look."), call("toolu_01"), call("toolu_02")))
    assert asked.tool_calls == (call("toolu_01"), call("toolu_02"))
    assert asked.tool_results == ()
    assert asked.text == "Let me look."
    assert answer(question(), parts=(call(),)).text == ""
    with pytest.raises(InvalidValueError, match="holds no tool result"):
        answer(question(), parts=(TextPart("hi"), result()))


def test_a_question_holds_no_tool_part() -> None:
    for part in (call(), result()):
        with pytest.raises(InvalidValueError, match="no tool call and no tool result"):
            question(parts=(TextPart("hi"), part))


def test_a_message_names_each_call_once() -> None:
    with pytest.raises(InvalidValueError, match="each tool call once"):
        answer(question(), parts=(call("same"), call("same", "other__tool")))
    asked = answer(question(), parts=(call("same"),))
    with pytest.raises(InvalidValueError, match="each tool call once"):
        tool_message(asked, result("same"), result("same", "again"))


def test_a_message_carries_a_vendors_extras_unread_and_bounded() -> None:
    assert question().extras == {}
    kept = answer(question(), extras={"anthropic": {"thinking": [{"signature": "sig"}]}})
    assert kept.extras == {"anthropic": {"thinking": [{"signature": "sig"}]}}
    assert isinstance(kept.extras, dict)
    given: dict[str, object] = {"vendor": {"a": 1}}
    kept = question(extras=given)
    given["vendor"] = "changed"  # the record keeps a copy
    assert kept.extras == {"vendor": {"a": 1}}
    with pytest.raises(InvalidValueError, match="at most"):
        question(extras={"vendor": "x" * MAX_EXTRAS_BYTES})
    for broken in ("text", ["a", "list"], {"a": object()}, {7: "keys"}):
        with pytest.raises(InvalidValueError):
            question(extras=broken)  # type: ignore[arg-type]


def test_a_messages_text_is_its_text_parts_and_not_its_reasoning() -> None:
    message = answer(
        question(), parts=(ReasoningPart("thinking"), TextPart("Hi "), TextPart("Ada"))
    )
    assert message.text == "Hi Ada"


def test_a_message_comes_from_the_web_unless_it_says_otherwise() -> None:
    assert question().channel is Channel.WEB
    assert {channel.value for channel in Channel} == {"web"}


# --- conversations ----------------------------------------------------------


def test_a_conversation_has_an_owner_and_an_agent() -> None:
    assert (conversation().owner_id, conversation().agent) == (OWNER, AGENT)


def test_a_conversation_names_the_model_it_runs_on() -> None:
    """Always, and never "whatever the agent says": the agent's default is
    copied in when the conversation starts, so there is no "no model"."""
    assert conversation().model == MODEL
    with pytest.raises(TypeError):
        Conversation(  # type: ignore[call-arg]
            id=CONVERSATION,
            owner_id=OWNER,
            agent=AGENT,
            created_at=datetime(2026, 9, 21, 9, 0, tzinfo=UTC),
            updated_at=datetime(2026, 9, 21, 9, 0, tzinfo=UTC),
        )


def test_a_new_conversation_has_no_title_yet() -> None:
    assert (
        Conversation(
            id=CONVERSATION,
            owner_id=OWNER,
            agent=AGENT,
            model=MODEL,
            created_at=datetime(2026, 9, 21, 9, 0, tzinfo=UTC),
            updated_at=datetime(2026, 9, 21, 9, 0, tzinfo=UTC),
        ).title
        == ""
    )


@pytest.mark.parametrize(
    "title", ["two\nlines", "a\ttab", "a\x00null", "​" + "zero width", "a" * (MAX_TITLE_CHARS + 1)]
)
def test_a_title_is_one_line_of_printable_text(title: str) -> None:
    with pytest.raises(InvalidValueError):
        conversation(title=title)


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "not a uuid"},
        {"owner_id": None},
        {"agent": "No Such Agent"},
        {"model": "Claude Sonnet 5"},
        {"model": None},
        {"model": ""},
        {"created_at": NAIVE},
        {"updated_at": NAIVE},
        {"title": 7},
    ],
)
def test_a_conversation_refuses_a_field_of_the_wrong_kind(changes: dict[str, object]) -> None:
    with pytest.raises(InvalidValueError):
        conversation(**changes)


def test_the_records_are_frozen() -> None:
    with pytest.raises(FrozenInstanceError):
        question().role = Role.ASSISTANT  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        provenance().model = "haiku"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        conversation().title = "renamed"  # type: ignore[misc]


# --- the calls of a path no tool message answers --------------------------------


def calling(parent: Message, *call_ids: str, seconds: float) -> Message:
    """An answer that made those calls."""
    return answer(
        parent,
        seconds=seconds,
        parts=tuple(
            ToolCallPart(call_id, "github__search", {"q": call_id}) for call_id in call_ids
        ),
    )


def answering(parent: Message, *call_ids: str) -> Message:
    """The tool message answering that answer's calls."""
    return Message(
        id=uuid.uuid4(),
        conversation_id=parent.conversation_id,
        parent_id=parent.id,
        role=Role.TOOL,
        parts=tuple(ToolResultPart(call_id, "found") for call_id in call_ids),
        created_at=parent.created_at,
    )


def test_a_calls_answer_is_answered_when_the_next_message_on_the_path_is_a_tool_message() -> None:
    asked = question("look", seconds=0)
    first = calling(asked, "toolu_01", "toolu_02", seconds=1)
    results = answering(first, "toolu_01", "toolu_02")
    second = calling(results, "toolu_03", seconds=2)
    more = answering(second, "toolu_03")
    done = answer(more, "Found.", seconds=3)
    again = question("thanks", parent=done, seconds=4)

    assert unanswered_calls((asked, first, results, second, more, done, again)) == {}


def test_a_request_defines_the_runs_tools_and_a_stub_per_other_name_the_history_calls() -> None:
    """The vendor refuses tool blocks its request defines no tool for, so a
    name the history calls and the run lacks is defined as a stub the model
    is told not to call: once per name, after the run's own, in the order
    the history first named them."""
    asked = question("look", seconds=0)
    first = calling(asked, "toolu_01", "toolu_02", seconds=1)
    results = answering(first, "toolu_01", "toolu_02")
    other = answer(
        results,
        seconds=2,
        parts=(
            ToolCallPart("toolu_03", "jira__find", {}),
            ToolCallPart("toolu_04", "github__search", {"q": "again"}),
        ),
    )
    offered = ToolDefinition(
        name="github__search", description="Search.", input_schema={"type": "object"}
    )

    assert tools_for_request((offered,), (asked, first, results, other)) == (
        offered,
        ToolDefinition(
            name="jira__find", description=NO_LONGER_OFFERED, input_schema={"type": "object"}
        ),
    )
    assert tools_for_request((), (asked, first, results, other)) == (
        ToolDefinition(
            name="github__search", description=NO_LONGER_OFFERED, input_schema={"type": "object"}
        ),
        ToolDefinition(
            name="jira__find", description=NO_LONGER_OFFERED, input_schema={"type": "object"}
        ),
    )
    # A history with no calls adds nothing, tools or none.
    assert tools_for_request((offered,), (asked,)) == (offered,)
    assert tools_for_request((), (asked, answer(asked, "Hi.", seconds=1))) == ()


def test_a_calls_answer_followed_by_anything_else_is_unanswered_by_every_call() -> None:
    asked = question("look", seconds=0)
    stopped = calling(asked, "toolu_01", "toolu_02", seconds=1)
    again = question("and now?", parent=stopped, seconds=2)

    found = unanswered_calls((asked, stopped, again))

    assert found == {stopped.id: stopped.tool_calls}
    assert [call.call_id for call in found[stopped.id]] == ["toolu_01", "toolu_02"]


def test_an_earlier_unanswered_round_stays_unanswered_however_the_path_goes_on() -> None:
    asked = question("look", seconds=0)
    stopped = calling(asked, "toolu_01", seconds=1)
    again = question("and now?", parent=stopped, seconds=2)
    later = calling(again, "toolu_02", seconds=3)
    results = answering(later, "toolu_02")
    done = answer(results, "Found.", seconds=4)

    assert unanswered_calls((asked, stopped, again, later, results, done)) == {
        stopped.id: stopped.tool_calls
    }


def test_an_answer_that_made_no_calls_and_a_path_with_none_are_not_in_it() -> None:
    asked = question("hi", seconds=0)
    replied = answer(asked, "Hello.", seconds=1)

    assert unanswered_calls((asked, replied)) == {}
    assert unanswered_calls(()) == {}


def test_a_calls_answer_that_ends_the_path_is_unanswered() -> None:
    # The port forbids the shape (a path ends in the message being answered),
    # and the one vendor-valid history for it is still "no result yet".
    asked = question("look", seconds=0)
    last = calling(asked, "toolu_01", seconds=1)

    assert unanswered_calls((asked, last)) == {last.id: last.tool_calls}

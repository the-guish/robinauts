// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import { act, renderHook, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import { json, refusal, type Call } from "../../test/api";
import {
  calling,
  conversation,
  id,
  message,
  opened,
  results,
} from "../../test/conversations";
import { event, streamed, streamHeaders, writable } from "../../test/stream";
import type { ChatProps } from "../index";
import type { AguiEvent } from "./agui/events";
import {
  AGENT_GONE,
  asRepository,
  asThreadMessage,
  LOST_TOUCH,
  MODEL_GONE,
  MODEL_GONE_NEW_CHAT,
  STILL_ANSWERING,
  useChat,
} from "./runtime";
import {
  EMPTY,
  reduce,
  ONE_AT_A_TIME,
  ONE_AT_A_TIME_ANSWER,
  saidFor,
  STOP_DID_NOT_ARRIVE,
  storedParent,
  turnStart,
  under,
  type ChatAction,
  type ChatMessage,
  type ChatState,
} from "./state";

const RUN = "11111111-2222-4333-8444-555555555555";
const CONVERSATION = id(1);
const ANSWER = "aaaaaaaa-0000-4000-8000-000000000001";

// ---------------------------------------------------------------- the reducer

/** Every one of those actions, in order, from nothing. */
function after(...actions: ChatAction[]): ChatState {
  return actions.reduce(reduce, EMPTY);
}

/** One AG-UI event, as the action that carries it. */
const sent = (event: AguiEvent): ChatAction => ({ kind: "event", event });

const asked = { kind: "asked", id: "q", after: null, text: "why?" } as const;
const started = {
  kind: "started",
  runId: RUN,
  conversationId: CONVERSATION,
} as const;

/** What a message says, by kind of part: its text, or the tool it called. */
function parts(state: ChatState, messageId: string) {
  const found = state.messages.find((each) => each.id === messageId);
  return (found?.parts ?? []).map((part) => [
    part.kind,
    part.kind === "tool-call" ? part.name : part.text,
  ]);
}

/** That call of that message, as the chat holds it. */
function call(state: ChatState, messageId: string, callId: string) {
  const found = state.messages.find((each) => each.id === messageId);
  return found?.parts.find(
    (part) => part.kind === "tool-call" && part.id === callId,
  );
}

const RESULTS = "bbbbbbbb-0000-4000-8000-000000000001";
const AFTER = "aaaaaaaa-0000-4000-8000-000000000002";

/** One tool round on the wire: the answer that calls, its result, the answer after. */
function toolRound(): ChatAction[] {
  return [
    sent({ type: "TEXT_MESSAGE_START", messageId: ANSWER, role: "assistant" }),
    sent({
      type: "TEXT_MESSAGE_CONTENT",
      messageId: ANSWER,
      delta: "Let me look.",
    }),
    sent({
      type: "TOOL_CALL_START",
      toolCallId: "toolu_01",
      toolCallName: "github__search",
      parentMessageId: ANSWER,
    }),
    sent({ type: "TOOL_CALL_ARGS", toolCallId: "toolu_01", delta: '{"q": ' }),
    sent({ type: "TOOL_CALL_ARGS", toolCallId: "toolu_01", delta: '"x"}' }),
    sent({ type: "TOOL_CALL_END", toolCallId: "toolu_01" }),
    sent({ type: "TEXT_MESSAGE_END", messageId: ANSWER }),
    sent({
      type: "TOOL_CALL_RESULT",
      messageId: RESULTS,
      toolCallId: "toolu_01",
      content: "found 3",
      isError: false,
    }),
    sent({ type: "TEXT_MESSAGE_START", messageId: AFTER, role: "assistant" }),
    sent({ type: "TEXT_MESSAGE_CONTENT", messageId: AFTER, delta: "Three." }),
    sent({ type: "TEXT_MESSAGE_END", messageId: AFTER }),
  ];
}

test("a whole turn, from the run starting to the run finishing", () => {
  const state = after(
    asked,
    started,
    sent({ type: "RUN_STARTED", threadId: CONVERSATION, runId: RUN }),
    sent({ type: "TEXT_MESSAGE_START", messageId: ANSWER, role: "assistant" }),
    sent({
      type: "TEXT_MESSAGE_CONTENT",
      messageId: ANSWER,
      delta: "Because ",
    }),
    sent({ type: "TEXT_MESSAGE_CONTENT", messageId: ANSWER, delta: "it is." }),
    sent({ type: "TEXT_MESSAGE_END", messageId: ANSWER }),
    sent({ type: "RUN_FINISHED", runId: RUN, cancelled: false }),
  );
  expect(state.messages.map((each) => [each.role, each.state])).toEqual([
    ["user", "stored"],
    ["assistant", "stored"],
  ]);
  // The deltas are one text part, in the order they arrived.
  expect(parts(state, ANSWER)).toEqual([["text", "Because it is."]]);
  expect(state.messages.map((each) => each.id)).toEqual(["q", ANSWER]);
  expect(state.runId).toBeNull();
  expect(state.sending).toBe(false);
  expect(state.ended).toBeNull();
});

test("thinking is collected as its own part, around what was said", () => {
  const thought = `${ANSWER}:reasoning:3`;
  const again = `${ANSWER}:reasoning:9`;
  const state = after(
    asked,
    started,
    sent({ type: "TEXT_MESSAGE_START", messageId: ANSWER, role: "assistant" }),
    sent({ type: "REASONING_MESSAGE_START", messageId: thought }),
    sent({
      type: "REASONING_MESSAGE_CONTENT",
      messageId: thought,
      delta: "so ",
    }),
    sent({
      type: "REASONING_MESSAGE_CONTENT",
      messageId: thought,
      delta: "far",
    }),
    sent({ type: "REASONING_MESSAGE_END", messageId: thought }),
    sent({ type: "TEXT_MESSAGE_CONTENT", messageId: ANSWER, delta: "One." }),
    // **A turn may think more than once**, and each stretch is a message of
    // its own on the wire, so it is a part of its own here (`api/agui.py`).
    sent({ type: "REASONING_MESSAGE_START", messageId: again }),
    sent({
      type: "REASONING_MESSAGE_CONTENT",
      messageId: again,
      delta: "more",
    }),
    sent({ type: "REASONING_MESSAGE_END", messageId: again }),
    sent({ type: "TEXT_MESSAGE_CONTENT", messageId: ANSWER, delta: " Two." }),
    sent({ type: "TEXT_MESSAGE_END", messageId: ANSWER }),
  );
  expect(parts(state, ANSWER)).toEqual([
    ["reasoning", "so far"],
    ["text", "One."],
    ["reasoning", "more"],
    ["text", " Two."],
  ]);
  expect(state.thinking).toBeNull();
});

test("the three no-ops a re-attach relies on", () => {
  const thought = `${ANSWER}:reasoning:3`;
  const open = after(
    asked,
    started,
    sent({ type: "TEXT_MESSAGE_START", messageId: ANSWER, role: "assistant" }),
    sent({ type: "TEXT_MESSAGE_CONTENT", messageId: ANSWER, delta: "half" }),
  );

  // 1. A `*_START` for a message the client already holds open.
  const again = reduce(
    open,
    sent({ type: "TEXT_MESSAGE_START", messageId: ANSWER, role: "assistant" }),
  );
  expect(again).toBe(open);
  expect(parts(again, ANSWER)).toEqual([["text", "half"]]);

  // ...and one held complete is not opened a second time either: an id
  // stands for one message.
  const stored = after(
    {
      kind: "opened",
      conversationId: CONVERSATION,
      messages: [message(ANSWER, "assistant", "whole")],
      runId: null,
      endedBadly: null,
    },
    sent({ type: "TEXT_MESSAGE_START", messageId: ANSWER, role: "assistant" }),
  );
  expect(stored.messages).toHaveLength(1);

  // 2. A `*_END` for one it does not hold.
  const ending = reduce(
    open,
    sent({ type: "TEXT_MESSAGE_END", messageId: "never-seen" }),
  );
  expect(ending).toBe(open);
  expect(
    reduce(open, sent({ type: "REASONING_MESSAGE_END", messageId: thought })),
  ).toBe(open);

  // ...and a delta for a message that is complete adds nothing to it: the
  // answer is the store's now, and appending would say it twice.
  const complete = reduce(
    open,
    sent({ type: "TEXT_MESSAGE_END", messageId: ANSWER }),
  );
  expect(
    reduce(
      complete,
      sent({ type: "TEXT_MESSAGE_CONTENT", messageId: ANSWER, delta: "more" }),
    ),
  ).toBe(complete);
  expect(parts(complete, ANSWER)).toEqual([["text", "half"]]);

  // 3. The terminal event of a run it has already seen end -- which is
  //    reachable **only from a re-attach at or past the last position**: a
  //    stream of this chat's own run sets `runId` from the response's
  //    headers before any event arrives, so the first ending always lands
  //    on a run the state knows about.
  const over = reduce(
    open,
    sent({ type: "RUN_FINISHED", runId: RUN, cancelled: false }),
  );
  expect(over.runId).toBeNull();
  expect(
    reduce(over, sent({ type: "RUN_FINISHED", runId: RUN, cancelled: false })),
  ).toBe(over);
  expect(
    reduce(over, sent({ type: "RUN_ERROR", code: "failed", message: "x" })),
  ).toBe(over);
});

test("a tool round: the call inside the answer, its result on the call, the answer after", () => {
  const state = after(asked, started, ...toolRound());
  expect(state.messages.map((each) => [each.role, each.state])).toEqual([
    ["user", "stored"],
    ["assistant", "stored"],
    ["assistant", "stored"],
  ]);
  expect(parts(state, ANSWER)).toEqual([
    ["text", "Let me look."],
    ["tool-call", "github__search"],
  ]);
  expect(call(state, ANSWER, "toolu_01")).toEqual({
    kind: "tool-call",
    id: "toolu_01",
    name: "github__search",
    argsText: '{"q": "x"}',
    args: { q: "x" },
    result: "found 3",
    isError: false,
  });
  // The tool message is no bubble of its own; the answer remembers it as
  // what the next message hangs under.
  expect(state.messages.map((each) => each.id)).toEqual(["q", ANSWER, AFTER]);
  expect(state.messages[1]?.resultsId).toBe(RESULTS);
  expect(parts(state, AFTER)).toEqual([["text", "Three."]]);
  expect(under(state)).toBe(AFTER);
});

test("a result that is an error is marked, and its text is kept as it came", () => {
  const state = after(
    asked,
    started,
    ...toolRound().slice(0, 7),
    sent({
      type: "TOOL_CALL_RESULT",
      messageId: RESULTS,
      toolCallId: "toolu_01",
      content: "<b>no</b> such [repository](x)",
      isError: true,
    }),
  );
  expect(call(state, ANSWER, "toolu_01")).toMatchObject({
    result: "<b>no</b> such [repository](x)",
    isError: true,
  });
});

test("the no-ops a re-attach relies on hold for a call too", () => {
  const round = toolRound();
  const open = after(asked, started, ...round.slice(0, 4));
  // A start for a call already held.
  expect(reduce(open, round[2]!)).toBe(open);
  // Arguments for a call never announced, and for one whose answer is complete.
  expect(
    reduce(
      open,
      sent({ type: "TOOL_CALL_ARGS", toolCallId: "never", delta: "{" }),
    ),
  ).toBe(open);
  // Complete, its results in: an answer with a batch still open is running.
  const complete = after(asked, started, ...round.slice(0, 8));
  expect(reduce(complete, round[4]!)).toBe(complete);
  // A start for a call of a message that is complete: the store has it.
  expect(reduce(complete, round[2]!)).toBe(complete);
  // An end twice, and a result twice.
  const ended = reduce(open, round[5]!);
  expect(reduce(ended, round[5]!)).toBe(ended);
  const answered = after(asked, started, ...round.slice(0, 8));
  expect(reduce(answered, round[7]!)).toBe(answered);
  // A result for a call nothing here holds.
  expect(
    reduce(
      answered,
      sent({
        type: "TOOL_CALL_RESULT",
        messageId: RESULTS,
        toolCallId: "never",
        content: "x",
        isError: false,
      }),
    ),
  ).toBe(answered);
});

test("a call whose answer's start went missing opens the answer", () => {
  const state = after(asked, started, toolRound()[2]!, toolRound()[5]!);
  expect(state.writing).toBe(ANSWER);
  expect(parts(state, ANSWER)).toEqual([["tool-call", "github__search"]]);
  // A call that names no answer belongs to the one being written.
  const unnamed = after(
    asked,
    started,
    toolRound()[0]!,
    sent({
      type: "TOOL_CALL_START",
      toolCallId: "toolu_09",
      toolCallName: "github__echo",
      parentMessageId: null,
    }),
  );
  expect(parts(unnamed, ANSWER)).toEqual([["tool-call", "github__echo"]]);
  // An end for a call nothing here holds changes nothing.
  expect(
    reduce(unnamed, sent({ type: "TOOL_CALL_END", toolCallId: "never" })),
  ).toBe(unnamed);
  // Arguments that never parse as an object are left to the renderer as text.
  const odd = after(
    asked,
    started,
    toolRound()[2]!,
    sent({ type: "TOOL_CALL_ARGS", toolCallId: "toolu_01", delta: "[1" }),
    toolRound()[5]!,
  );
  expect(call(odd, ANSWER, "toolu_01")).toMatchObject({ argsText: "[1" });
  expect(call(odd, ANSWER, "toolu_01")).not.toHaveProperty("args");
});

test("an answer that asked for tools is running until its last result has landed", () => {
  // The backend completes the answer's text before it runs the calls, and
  // the results land on it afterwards: drawn as in progress until then.
  const round = toolRound();
  const ended = after(asked, started, ...round.slice(0, 7));
  expect(ended.writing).toBeNull();
  expect(ended.messages[1]?.state).toBe("running");
  const answered = reduce(ended, round[7]!);
  expect(answered.messages[1]?.state).toBe("stored");
  // With two calls, the first result does not end it; the second does.
  const two = after(
    asked,
    started,
    ...round.slice(0, 6),
    sent({
      type: "TOOL_CALL_START",
      toolCallId: "toolu_02",
      toolCallName: "github__echo",
      parentMessageId: ANSWER,
    }),
    sent({ type: "TOOL_CALL_END", toolCallId: "toolu_02" }),
    sent({ type: "TEXT_MESSAGE_END", messageId: ANSWER }),
    round[7]!,
  );
  expect(two.messages[1]?.state).toBe("running");
  expect(
    reduce(
      two,
      sent({
        type: "TOOL_CALL_RESULT",
        messageId: RESULTS,
        toolCallId: "toolu_02",
        content: "echoed",
        isError: false,
      }),
    ).messages[1]?.state,
  ).toBe("stored");
});

test("a run cancelled in the middle of a batch leaves the call without a result", () => {
  const state = after(
    asked,
    started,
    ...toolRound().slice(0, 7),
    sent({ type: "RUN_FINISHED", runId: RUN, cancelled: true }),
  );
  expect(call(state, ANSWER, "toolu_01")).not.toHaveProperty("result");
  // The answer was still running -- its calls were unanswered -- so the
  // ending marks it, and its calls are drawn as cancelled.
  expect(state.messages[1]?.state).toBe("cancelled");
  expect(state.messages[1]?.resultsId).toBeUndefined();
  // The question after it hangs under the answer itself: there is no tool
  // message (`docs/specs/runs.md`).
  expect(under(state)).toBe(ANSWER);
  // A failure in the middle of a batch says so on the answer.
  const failed = after(
    asked,
    started,
    ...toolRound().slice(0, 7),
    sent({ type: "RUN_ERROR", code: "failed", message: "x" }),
  );
  expect(failed.messages[1]?.state).toBe("failed");
  expect(failed.messages[1]?.detail).toBe(saidFor("failed"));
});

test("opened, an answer that holds its own results has them on its calls", () => {
  const answer = calling("m2", "", [
    { call_id: "call-1", name: "echo", arguments: { text: "hi" } },
  ]);
  answer.parts.push(
    { kind: "tool_result", call_id: "call-1", text: "hi", is_error: false },
    { kind: "text", text: "The tool said: hi" },
  );
  const stored = after({
    kind: "opened",
    conversationId: CONVERSATION,
    messages: [message("m1", "user", "hi"), answer],
    runId: null,
    endedBadly: null,
  });
  expect(stored.messages[1]?.state).toBe("stored");
  expect(call(stored, "m2", "call-1")).toMatchObject({ result: "hi" });
  expect(parts(stored, "m2")).toEqual([
    ["tool-call", "echo"],
    ["text", "The tool said: hi"],
  ]);
});

test("opened, an answer whose calls were never answered is shown as the run left it", () => {
  const stored = [
    message("m1", "user", "why?"),
    calling("m2", "", [
      { call_id: "toolu_01", name: "github__search", arguments: {} },
    ]),
  ];
  // The last message, with a run in flight: its results are on their way.
  const running = after({
    kind: "opened",
    conversationId: CONVERSATION,
    messages: stored,
    runId: RUN,
    endedBadly: null,
  });
  expect(running.messages[1]?.state).toBe("running");
  // No run: the batch was cancelled, or the run failed and says so.
  const cancelled = after({
    kind: "opened",
    conversationId: CONVERSATION,
    messages: stored,
    runId: null,
    endedBadly: "stopped",
    endedState: "cancelled",
  });
  expect(cancelled.messages[1]?.state).toBe("cancelled");
  const failed = after({
    kind: "opened",
    conversationId: CONVERSATION,
    messages: stored,
    runId: null,
    endedBadly: "went wrong",
    endedState: "failed",
  });
  expect(failed.messages[1]?.state).toBe("failed");
  expect(failed.messages[1]?.detail).toBe("went wrong");
  // Earlier in the thread, whatever the last run did: that batch is over.
  const earlier = after({
    kind: "opened",
    conversationId: CONVERSATION,
    messages: [...stored, message("m3", "user", "and?")],
    runId: RUN,
    endedBadly: null,
  });
  expect(earlier.messages[1]?.state).toBe("cancelled");
  // A batch that was answered is an answer like any other.
  const whole = after({
    kind: "opened",
    conversationId: CONVERSATION,
    messages: [...stored, results("t1", [{ call_id: "toolu_01", text: "ok" }])],
    runId: null,
    endedBadly: null,
  });
  expect(whole.messages[1]?.state).toBe("stored");
  // A tool message after anything but an answer is dropped, not folded.
  const odd = after({
    kind: "opened",
    conversationId: CONVERSATION,
    messages: [
      message("m1", "user", "why?"),
      results("t1", [{ call_id: "toolu_01", text: "ok" }]),
    ],
    runId: null,
    endedBadly: null,
  });
  expect(odd.messages.map((each) => each.id)).toEqual(["m1"]);
  expect(odd.messages[0]?.resultsId).toBeUndefined();
});

test("a regeneration replaces the turn: the cut is at its question", () => {
  const state = after({
    kind: "opened",
    conversationId: CONVERSATION,
    messages: [
      message("m1", "user", "why?"),
      calling("m2", "Let me look.", [
        { call_id: "toolu_01", name: "github__search", arguments: {} },
      ]),
      results("t1", [{ call_id: "toolu_01", text: "found 3" }]),
      message("m3", "assistant", "Three."),
      message("m4", "user", "and?"),
      message("m5", "assistant", "Four."),
    ],
    runId: null,
    endedBadly: null,
  });
  // The answer after the round, whose screen-parent is the calling answer.
  expect(turnStart(state, "m3")).toBe("m1");
  expect(turnStart(state, "m2")).toBe("m1");
  expect(turnStart(state, "m5")).toBe("m4");
  expect(turnStart(state, "m1")).toBe("m1");
  expect(turnStart(state, "never")).toBeNull();
  const again = reduce(state, { kind: "again", after: turnStart(state, "m3") });
  expect(again.messages.map((each) => each.id)).toEqual(["m1"]);
});

test("opening a conversation folds each tool message into the answer before it", () => {
  const state = after({
    kind: "opened",
    conversationId: CONVERSATION,
    messages: [
      message("m1", "user", "why?"),
      calling("m2", "Let me look.", [
        { call_id: "toolu_01", name: "github__search", arguments: { q: "x" } },
        { call_id: "toolu_02", name: "github__echo", arguments: {} },
      ]),
      results("t1", [
        { call_id: "toolu_02", text: "nothing", is_error: true },
        { call_id: "toolu_01", text: "found 3" },
      ]),
      message("m3", "assistant", "Three."),
    ],
    runId: null,
    endedBadly: null,
  });
  expect(state.messages.map((each) => each.id)).toEqual(["m1", "m2", "m3"]);
  expect(parts(state, "m2")).toEqual([
    ["text", "Let me look."],
    ["tool-call", "github__search"],
    ["tool-call", "github__echo"],
  ]);
  expect(call(state, "m2", "toolu_01")).toEqual({
    kind: "tool-call",
    id: "toolu_01",
    name: "github__search",
    argsText: '{"q":"x"}',
    args: { q: "x" },
    result: "found 3",
    isError: false,
  });
  expect(call(state, "m2", "toolu_02")).toMatchObject({
    result: "nothing",
    isError: true,
  });
  expect(state.messages[1]?.resultsId).toBe("t1");

  // A thread that ends on the tool message: a question after it is its
  // child, and a cut at it is a cut after the answer that holds it.
  const ending = after({
    kind: "opened",
    conversationId: CONVERSATION,
    messages: [
      message("m1", "user", "why?"),
      calling("m2", "", [
        { call_id: "toolu_01", name: "github__search", arguments: {} },
      ]),
      results("t1", [{ call_id: "toolu_01", text: "found 3" }]),
    ],
    runId: null,
    endedBadly: null,
  });
  expect(under(ending)).toBe("t1");
  expect(storedParent(ending, "m2")).toBe("t1");
  expect(storedParent(ending, "m1")).toBe("m1");
  expect(storedParent(ending, null)).toBeNull();
  const cut = reduce(ending, {
    kind: "asked",
    id: "unsent:q",
    after: "t1",
    text: "and?",
  });
  expect(cut.messages.map((each) => each.id)).toEqual(["m1", "m2", "unsent:q"]);
  // And in the middle of a thread: an edit under the tool message cuts
  // after the answer that holds it, not at the end.
  const longer = after({
    kind: "opened",
    conversationId: CONVERSATION,
    messages: [
      message("m1", "user", "why?"),
      calling("m2", "", [
        { call_id: "toolu_01", name: "github__search", arguments: {} },
      ]),
      results("t1", [{ call_id: "toolu_01", text: "found 3" }]),
      message("m3", "user", "and?"),
      message("m4", "assistant", "Four."),
    ],
    runId: null,
    endedBadly: null,
  });
  const edited = reduce(longer, {
    kind: "asked",
    id: "unsent:edit",
    after: "t1",
    text: "and then?",
  });
  expect(edited.messages.map((each) => each.id)).toEqual([
    "m1",
    "m2",
    "unsent:edit",
  ]);

  // Read again unchanged, the messages are the same objects: nothing is
  // converted twice over one answer.
  const again = reduce(state, {
    kind: "opened",
    conversationId: CONVERSATION,
    messages: [
      message("m1", "user", "why?"),
      calling("m2", "Let me look.", [
        { call_id: "toolu_01", name: "github__search", arguments: { q: "x" } },
        { call_id: "toolu_02", name: "github__echo", arguments: {} },
      ]),
      results("t1", [
        { call_id: "toolu_02", text: "nothing", is_error: true },
        { call_id: "toolu_01", text: "found 3" },
      ]),
      message("m3", "assistant", "Three."),
    ],
    runId: null,
    endedBadly: null,
  });
  expect(again.messages[1]).toBe(state.messages[1]);
  // And a result that changed is a message that changed.
  const changed = reduce(state, {
    kind: "opened",
    conversationId: CONVERSATION,
    messages: [
      message("m1", "user", "why?"),
      calling("m2", "Let me look.", [
        { call_id: "toolu_01", name: "github__search", arguments: { q: "x" } },
        { call_id: "toolu_02", name: "github__echo", arguments: {} },
      ]),
      results("t1", [
        { call_id: "toolu_02", text: "nothing", is_error: true },
        { call_id: "toolu_01", text: "found 4" },
      ]),
      message("m3", "assistant", "Three."),
    ],
    runId: null,
    endedBadly: null,
  });
  expect(changed.messages[1]).not.toBe(state.messages[1]);
});

test("a call is handed to assistant-ui as its tool-call part, as data", () => {
  const state = after(asked, started, ...toolRound());
  const handed = asThreadMessage(state.messages[1]!);
  expect(handed.content).toEqual([
    { type: "text", text: "Let me look." },
    {
      type: "tool-call",
      toolCallId: "toolu_01",
      toolName: "github__search",
      argsText: '{"q": "x"}',
      args: { q: "x" },
      result: "found 3",
      isError: false,
    },
  ]);
  // A call still streaming has no arguments as data yet, and no result.
  const streaming = after(asked, started, ...toolRound().slice(0, 4));
  expect(asThreadMessage(streaming.messages[1]!).content).toEqual([
    { type: "text", text: "Let me look." },
    {
      type: "tool-call",
      toolCallId: "toolu_01",
      toolName: "github__search",
      argsText: '{"q": ',
    },
  ]);
});

test("content for a message whose start went missing still arrives", () => {
  const state = after(
    asked,
    started,
    sent({ type: "TEXT_MESSAGE_CONTENT", messageId: ANSWER, delta: "rest" }),
  );
  expect(parts(state, ANSWER)).toEqual([["text", "rest"]]);
  expect(state.writing).toBe(ANSWER);
});

test("a run that failed says so on the answer, one sentence per code", () => {
  for (const code of ["failed", "interrupted", "quiet", "gone", "internal"]) {
    const state = after(
      asked,
      started,
      sent({
        type: "TEXT_MESSAGE_START",
        messageId: ANSWER,
        role: "assistant",
      }),
      sent({ type: "RUN_ERROR", code, message: "whatever the backend said" }),
    );
    const failed = state.messages.find((each) => each.id === ANSWER);
    expect(failed?.state).toBe("failed");
    expect(failed?.detail).toBe(saidFor(code));
    // Never the backend's own sentence, and never the run's stored error.
    expect(failed?.detail).not.toContain("whatever");
    expect(state.runId).toBeNull();
  }
  // A code this build does not know still says that something went wrong.
  expect(saidFor("something-new")).toBe("This answer did not finish.");
});

test("a run that failed before it said anything is said by the thread", () => {
  const state = after(
    asked,
    started,
    sent({ type: "RUN_ERROR", code: "failed", message: "x" }),
  );
  expect(state.ended).toBe(saidFor("failed"));
  expect(state.messages.map((each) => each.state)).toEqual(["stored"]);
});

test("a run that ends closes every message still open", () => {
  // One answer at a time is the rule and the platform keeps it, so there is
  // never more than one; a run that ended leaving a second spinning would
  // leave it spinning for ever.
  const second = "aaaaaaaa-0000-4000-8000-000000000002";
  const state = after(
    asked,
    started,
    sent({ type: "TEXT_MESSAGE_START", messageId: ANSWER, role: "assistant" }),
    sent({ type: "TEXT_MESSAGE_CONTENT", messageId: ANSWER, delta: "one" }),
    // A second announcement without the first having been completed: not
    // something the backend does, and not something to leave open either.
    sent({ type: "TEXT_MESSAGE_START", messageId: second, role: "assistant" }),
    sent({ type: "RUN_ERROR", code: "failed", message: "x" }),
  );
  expect(
    state.messages
      .filter((each) => each.role === "assistant")
      .map((each) => each.state),
  ).toEqual(["failed", "failed"]);
  expect(state.messages.every((each) => each.state !== "running")).toBe(true);
});

test("a cancellation is not a failure", () => {
  const state = after(
    asked,
    started,
    sent({ type: "TEXT_MESSAGE_START", messageId: ANSWER, role: "assistant" }),
    sent({ type: "TEXT_MESSAGE_CONTENT", messageId: ANSWER, delta: "half" }),
    sent({ type: "RUN_FINISHED", runId: RUN, cancelled: true }),
  );
  const stopped = state.messages.find((each) => each.id === ANSWER);
  expect(stopped?.state).toBe("cancelled");
  // What was produced before it stays (`docs/specs/runs.md`).
  expect(parts(state, ANSWER)).toEqual([["text", "half"]]);
  expect(state.ended).toBe("This answer was stopped before it was finished.");
});

test("opening a conversation is the thread, in the order it was sent", () => {
  const state = after({
    kind: "opened",
    conversationId: CONVERSATION,
    messages: [
      message("m1", "user", "why?"),
      message("m2", "assistant", "because"),
      message("m3", "user", "and then?"),
    ],
    runId: null,
    endedBadly: null,
  });
  expect(state.messages.map((each) => each.id)).toEqual(["m1", "m2", "m3"]);
  expect(state.runId).toBeNull();
});

test("opening during a regeneration shows the new answer in place of the old", () => {
  // The thread was m1 m2 m3 m4, and m2 is being regenerated. The server
  // sends the thread up to what the run is extending -- `resume.follows`,
  // here m1 -- so what streams in is appended where the old answer was, and
  // not after m4 (`docs/specs/wire.md`).
  const showing = after({
    kind: "opened",
    conversationId: CONVERSATION,
    messages: [
      message("m1", "user", "why?"),
      message("m2", "assistant", "because"),
      message("m3", "user", "and then?"),
      message("m4", "assistant", "then this"),
    ],
    runId: null,
    endedBadly: null,
  });
  const state = [
    {
      kind: "opened",
      conversationId: CONVERSATION,
      messages: [message("m1", "user", "why?")],
      runId: RUN,
      endedBadly: null,
    } as const,
    sent({ type: "TEXT_MESSAGE_START", messageId: ANSWER, role: "assistant" }),
    sent({ type: "TEXT_MESSAGE_CONTENT", messageId: ANSWER, delta: "since" }),
    sent({ type: "TEXT_MESSAGE_END", messageId: ANSWER }),
    sent({ type: "RUN_FINISHED", runId: RUN, cancelled: false }),
  ].reduce<ChatState>(reduce, showing);
  expect(state.messages.map((each) => each.id)).toEqual(["m1", ANSWER]);
  expect(parts(state, ANSWER)).toEqual([["text", "since"]]);
  expect(state.runId).toBeNull();
});

test("an edit cuts the thread after the edited message's parent", () => {
  const opened = after({
    kind: "opened",
    conversationId: CONVERSATION,
    messages: [
      message("m1", "user", "why?"),
      message("m2", "assistant", "because"),
      message("m3", "user", "and then?"),
      message("m4", "assistant", "then this"),
    ],
    runId: null,
    endedBadly: null,
  });
  // Editing `m3`, whose parent is `m2`: `m3` and everything after it go.
  const edited = reduce(opened, {
    kind: "asked",
    id: "unsent:edit",
    after: "m2",
    text: "and at night?",
  });
  expect(edited.messages.map((each) => each.id)).toEqual([
    "m1",
    "m2",
    "unsent:edit",
  ]);
  // Editing the first message: nothing before it, so nothing is kept.
  const root = reduce(opened, {
    kind: "asked",
    id: "unsent:root",
    after: null,
    text: "why, really?",
  });
  expect(root.messages.map((each) => each.id)).toEqual(["unsent:root"]);
  // A regeneration cuts the answer off and everything after it.
  const again = reduce(opened, { kind: "again", after: "m1" });
  expect(again.messages.map((each) => each.id)).toEqual(["m1"]);
  expect(again.sending).toBe(true);
});

test("a refused turn puts the thread back exactly as it was", () => {
  const opened = after({
    kind: "opened",
    conversationId: CONVERSATION,
    messages: [
      message("m1", "user", "why?"),
      message("m2", "assistant", "because"),
      message("m3", "user", "and then?"),
    ],
    runId: null,
    endedBadly: null,
  });
  const was = opened.messages;
  // An edit that cut the thread: the cut is undone with the question.
  const edited = reduce(
    reduce(opened, {
      kind: "asked",
      id: "unsent:edit",
      after: "m2",
      text: "and at night?",
    }),
    { kind: "refused", detail: "no" },
  );
  expect(edited.messages).toBe(was);
  expect(edited.sending).toBe(false);
  expect(edited.ended).toBe("no");
  // A regeneration that cut the answer off: the answer is back.
  const again = reduce(reduce(opened, { kind: "again", after: "m1" }), {
    kind: "refused",
    detail: "no",
  });
  expect(again.messages).toBe(was);
  // A turn refused on an empty chat goes back to nothing.
  const first = reduce(reduce(EMPTY, asked), { kind: "refused", detail: "no" });
  expect(first.messages).toEqual([]);
  // A refusal while an earlier answer is still arriving takes only the
  // refused question off: the shape that once took the interface down.
  const answering = after(
    asked,
    started,
    sent({ type: "TEXT_MESSAGE_START", messageId: ANSWER, role: "assistant" }),
    sent({ type: "TEXT_MESSAGE_CONTENT", messageId: ANSWER, delta: "half" }),
  );
  const forgotten = { ...answering, runId: null, sending: false };
  const second = reduce(forgotten, {
    kind: "asked",
    id: "unsent:second",
    after: ANSWER,
    text: "impatient",
  });
  const refused = reduce(second, { kind: "refused", detail: "no" });
  expect(refused.messages.map((each) => each.id)).toEqual(["q", ANSWER]);
  expect(parts(refused, ANSWER)).toEqual([["text", "half"]]);
});

test("the thread is handed over as a chain, each message under the one before", () => {
  const messages: ChatMessage[] = ["m1", "m2", "m3"].map((id) => ({
    id,
    role: "user",
    parts: [{ kind: "text", text: id }],
    state: "stored",
  }));
  const chain = asRepository(messages);
  expect(chain.messages.map((each) => each.parentId)).toEqual([
    null,
    "m1",
    "m2",
  ]);
  expect(chain.headId).toBe("m3");
  // Nothing at all is no head, rather than a head of `null`.
  expect(asRepository([]).headId).toBeUndefined();
  expect(asRepository([]).messages).toEqual([]);
});

test("a stream that could not be picked up again is said, not hidden", () => {
  const state = after(
    asked,
    started,
    sent({ type: "TEXT_MESSAGE_START", messageId: ANSWER, role: "assistant" }),
    sent({ type: "TEXT_MESSAGE_CONTENT", messageId: ANSWER, delta: "half" }),
    { kind: "lost", detail: "the connection went" },
  );
  expect(state.runId).toBeNull();
  expect(state.sending).toBe(false);
  expect(state.ended).toBe("the connection went");
  // **And the answer it was writing is closed.** Nothing is watching the
  // run, so nothing else will ever say that message is over.
  expect(state.messages.every((each) => each.state !== "running")).toBe(true);
  expect(state.messages.find((each) => each.id === ANSWER)?.state).toBe(
    "cancelled",
  );
});

test("an ending is never ignored while an answer is still open", () => {
  // The no-op for "a run this client has already seen end" must not swallow
  // the one event that would close a message still being written.
  const open = after(
    asked,
    started,
    sent({ type: "TEXT_MESSAGE_START", messageId: ANSWER, role: "assistant" }),
  );
  const forgotten = { ...open, runId: null };
  const over = reduce(
    forgotten,
    sent({ type: "RUN_FINISHED", runId: RUN, cancelled: false }),
  );
  expect(over.messages.find((each) => each.id === ANSWER)?.state).toBe(
    "stored",
  );
  // With nothing open it really is a no-op.
  expect(
    reduce(over, sent({ type: "RUN_FINISHED", runId: RUN, cancelled: false })),
  ).toBe(over);
});

test("opening another conversation shows nothing of the one before it", () => {
  const some = after(asked, started);
  const other = reduce(some, { kind: "opening", conversationId: id(2) });
  expect(other.messages).toEqual([]);
  expect(other.loading).toBe(true);
  // Reading the same one again keeps what is on the screen.
  const reread = reduce(
    reduce(some, { kind: "started", runId: RUN, conversationId: id(2) }),
    { kind: "opening", conversationId: id(2) },
  );
  expect(reread.messages).toHaveLength(1);
});

// -------------------------------------------------------------- the whole hook

/** A conversation with one exchange in it, and no run. */
const TREE = [
  message("m1", "user", "why?"),
  message("m2", "assistant", "because"),
];

function stub(answer: (call: Call) => Response | undefined) {
  const fetch = vi.fn<typeof globalThis.fetch>((input, init) => {
    const raw = init?.body;
    const call: Call = {
      url: String(input),
      method: (init?.method ?? "GET").toUpperCase(),
      body: typeof raw === "string" ? JSON.parse(raw) : undefined,
    };
    const given = answer(call);
    if (given === undefined) {
      throw new Error(`nothing answers ${call.method} ${call.url}`);
    }
    return Promise.resolve(given);
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

/** The chat, with props a test can change. */
function chatting(props: Partial<ChatProps> = {}) {
  const started = vi.fn();
  const ended = vi.fn();
  const drawn = renderHook((given: ChatProps) => useChat(given), {
    initialProps: {
      conversationId: null,
      agentId: "helper",
      modelId: "sonnet",
      onConversationStarted: started,
      onTurnEnded: ended,
      ...props,
    } satisfies ChatProps,
  });
  return { ...drawn, started, ended };
}

/** Let everything that is already resolved run. */
const settle = () =>
  act(async () => new Promise((done) => setTimeout(done, 0)));

test("a first message begins a conversation and says which one", async () => {
  const fetch = stub((call) => {
    if (call.url === "/api/turns") {
      return streamed(
        [
          event(
            "TEXT_MESSAGE_START",
            { messageId: "m2", role: "assistant" },
            2,
          ),
          event(
            "TEXT_MESSAGE_CONTENT",
            { messageId: "m2", delta: "because" },
            3,
          ),
          event("TEXT_MESSAGE_END", { messageId: "m2" }, 4),
          event("RUN_FINISHED", { threadId: CONVERSATION, runId: RUN }, 5),
        ],
        { headers: streamHeaders(RUN, CONVERSATION) },
      );
    }
    if (call.url === `/api/conversations/${CONVERSATION}`) {
      return json(opened(conversation(1), TREE));
    }
    return undefined;
  });
  const { result, started, ended } = chatting();
  await settle();

  await act(async () => {
    await result.current.runtime.thread.append("why?");
  });
  expect(JSON.parse(String(fetch.mock.calls[0]?.[1]?.body))).toEqual({
    agent_id: "helper",
    model_id: "sonnet",
    text: "why?",
  });
  // The id comes from the response's headers, before any event arrives.
  expect(started).toHaveBeenCalledWith(CONVERSATION);

  await waitFor(() => {
    expect(result.current.state.runId).toBeNull();
  });
  await settle();
  // The turn is over, so the store is read again: the ids are the server's.
  expect(result.current.state.messages.map((each) => each.id)).toEqual([
    "m1",
    "m2",
  ]);
  expect(ended).toHaveBeenCalled();
});

test("a conversation with a run in flight is attached to at resume.after", async () => {
  const fetch = stub((call) => {
    if (call.url === `/api/conversations/${CONVERSATION}`) {
      return json(
        opened(conversation(1), [TREE[0]!], {
          run_id: RUN,
          resume: { after: 4, follows: "m1" },
        }),
      );
    }
    if (call.url === `/api/conversations/${CONVERSATION}/runs/${RUN}/events`) {
      return streamed(
        [
          event(
            "TEXT_MESSAGE_START",
            { messageId: "m2", role: "assistant" },
            5,
          ),
          event("TEXT_MESSAGE_CONTENT", { messageId: "m2", delta: "be" }, 6),
        ],
        { headers: streamHeaders(RUN, CONVERSATION) },
      );
    }
    return undefined;
  });
  const { result } = chatting({ conversationId: CONVERSATION });
  await waitFor(() => {
    expect(result.current.state.messages).toHaveLength(2);
  });
  const attaching = fetch.mock.calls.find(
    ([url]) =>
      String(url) === `/api/conversations/${CONVERSATION}/runs/${RUN}/events`,
  );
  expect(
    (attaching?.[1]?.headers as Record<string, string>)["last-event-id"],
  ).toBe("4");
  // The message still being produced is at the end of the thread.
  expect(result.current.state.messages[1]?.id).toBe("m2");
});

test("editing is a new message under the parent of the one it replaces", async () => {
  const posts: Call[] = [];
  // Three deep, so the question being edited has a parent that is not the
  // root: an edit hangs under the parent of the message it replaces, and
  // the thread on the screen is cut after that parent (`state.ts`).
  const deeper = [...TREE, message("m3", "user", "and then?")];
  stub((call) => {
    if (call.url === `/api/conversations/${CONVERSATION}`) {
      return json(opened(conversation(1), deeper));
    }
    if (call.url === `/api/conversations/${CONVERSATION}/turns`) {
      posts.push(call);
      return streamed(
        [event("RUN_FINISHED", { threadId: CONVERSATION, runId: RUN }, 9)],
        { headers: streamHeaders(RUN, CONVERSATION) },
      );
    }
    return undefined;
  });
  const { result } = chatting({ conversationId: CONVERSATION });
  await waitFor(() => {
    expect(result.current.state.messages).toHaveLength(3);
  });

  // The question being replaced is `m3`, whose parent is the answer `m2`:
  // an edit hangs under the parent of the message it replaces, and that is
  // not usually the root (`docs/specs/conversations.md`). Nothing is sent
  // for `m3` itself: the server keeps it, off the visible thread.
  await act(async () => {
    result.current.runtime.thread.append({
      role: "user",
      content: [{ type: "text", text: "and at night?" }],
      parentId: "m2",
      sourceId: "m3",
    });
    await settle();
  });
  expect(posts[0]?.body).toEqual({
    text: "and at night?",
    parent_id: "m2",
    model_id: "sonnet",
  });

  // The root's own edit still hangs under nothing.
  await act(async () => {
    result.current.runtime.thread.append({
      role: "user",
      content: [{ type: "text", text: "why really?" }],
      parentId: null,
      sourceId: "m1",
    });
    await settle();
  });
  expect(posts[1]?.body).toEqual({
    text: "why really?",
    parent_id: null,
    model_id: "sonnet",
  });
});

test("an edit after a tool round hangs under the tool message, not the answer", async () => {
  // The thread on the screen is m1, m2 (whose calls t1 answered), m3; the
  // runtime names m2 as m3's parent, and the store's is t1.
  const posts: Call[] = [];
  stub((call) => {
    if (call.url === `/api/conversations/${CONVERSATION}`) {
      return json(
        opened(conversation(1), [
          message("m1", "user", "why?"),
          calling("m2", "", [
            { call_id: "toolu_01", name: "github__search", arguments: {} },
          ]),
          results("t1", [{ call_id: "toolu_01", text: "found 3" }]),
          message("m3", "user", "and then?"),
        ]),
      );
    }
    if (call.url === `/api/conversations/${CONVERSATION}/turns`) {
      posts.push(call);
      return streamed(
        [event("RUN_FINISHED", { threadId: CONVERSATION, runId: RUN }, 9)],
        { headers: streamHeaders(RUN, CONVERSATION) },
      );
    }
    return undefined;
  });
  const { result } = chatting({ conversationId: CONVERSATION });
  await waitFor(() => {
    expect(result.current.state.messages).toHaveLength(3);
  });

  await act(async () => {
    result.current.runtime.thread.append({
      role: "user",
      content: [{ type: "text", text: "and at night?" }],
      parentId: "m2",
      sourceId: "m3",
    });
    await settle();
  });
  expect(posts[0]?.body).toEqual({
    text: "and at night?",
    parent_id: "t1",
    model_id: "sonnet",
  });
});

test("after a turn that went wrong, asking again replaces the question", async () => {
  // The answer a failed run was producing is in no conversation
  // (`docs/specs/runs.md`), so the thread ends on the question. The format
  // refuses a question under a question, and asking again is how the turn is
  // retried: the new one takes the old one's place.
  const posts: Call[] = [];
  stub((call) => {
    if (call.url === `/api/conversations/${CONVERSATION}`) {
      return json(
        opened(conversation(1), [TREE[0]!], {
          ended_badly: {
            run_id: RUN,
            state: "failed",
            ended_at: "2026-09-22T10:00:00Z",
          },
        }),
      );
    }
    if (call.url === `/api/conversations/${CONVERSATION}/turns`) {
      posts.push(call);
      return streamed(
        [event("RUN_FINISHED", { threadId: CONVERSATION, runId: RUN }, 9)],
        { headers: streamHeaders(RUN, CONVERSATION) },
      );
    }
    return undefined;
  });
  const { result } = chatting({ conversationId: CONVERSATION });
  await waitFor(() => {
    expect(result.current.state.messages).toHaveLength(1);
  });
  await act(async () => {
    result.current.runtime.thread.append("why, really?");
    await settle();
  });
  expect(posts[0]?.body).toEqual({
    text: "why, really?",
    parent_id: null,
    model_id: "sonnet",
  });
});

test("opening a conversation says what the server says it is", async () => {
  stub((call) => {
    if (call.url === `/api/conversations/${CONVERSATION}`) {
      return json(opened(conversation(1, "Robins"), TREE));
    }
    return undefined;
  });
  const openedWith = vi.fn();
  const { result } = chatting({
    conversationId: CONVERSATION,
    onConversationOpened: openedWith,
  });
  await waitFor(() => {
    expect(result.current.state.messages).toHaveLength(2);
  });
  expect(openedWith).toHaveBeenCalledWith(conversation(1, "Robins"));
});

test("regenerating names the answer to produce again, and sends no message", async () => {
  const posts: Call[] = [];
  stub((call) => {
    if (call.url === `/api/conversations/${CONVERSATION}`) {
      return json(opened(conversation(1), TREE));
    }
    if (call.url === `/api/conversations/${CONVERSATION}/turns`) {
      posts.push(call);
      return streamed(
        [event("RUN_FINISHED", { threadId: CONVERSATION, runId: RUN }, 9)],
        { headers: streamHeaders(RUN, CONVERSATION) },
      );
    }
    return undefined;
  });
  const { result } = chatting({ conversationId: CONVERSATION });
  await waitFor(() => {
    expect(result.current.state.messages).toHaveLength(2);
  });

  await act(async () => {
    result.current.runtime.thread.startRun({ parentId: "m1", sourceId: "m2" });
    await settle();
  });
  expect(posts[0]?.body).toEqual({ regenerate: "m2", model_id: "sonnet" });
});

test("regenerating the answer after a tool round takes the whole turn off the screen", async () => {
  // The screen-parent of m3 is the calling answer m2; the turn's question is
  // m1, and a regeneration replaces the turn (`docs/specs/conversations.md`),
  // so m2 goes too -- or the new answer would stream in under it.
  const { response, write, close } = writable({
    headers: streamHeaders(RUN, CONVERSATION),
  });
  const posts: Call[] = [];
  stub((call) => {
    if (call.url === `/api/conversations/${CONVERSATION}`) {
      return json(
        opened(conversation(1), [
          message("m1", "user", "why?"),
          calling("m2", "", [
            { call_id: "toolu_01", name: "github__search", arguments: {} },
          ]),
          results("t1", [{ call_id: "toolu_01", text: "found 3" }]),
          message("m3", "assistant", "Three."),
        ]),
      );
    }
    if (call.url === `/api/conversations/${CONVERSATION}/turns`) {
      posts.push(call);
      return response;
    }
    return undefined;
  });
  const { result } = chatting({ conversationId: CONVERSATION });
  await waitFor(() => {
    expect(result.current.state.messages).toHaveLength(3);
  });

  await act(async () => {
    result.current.runtime.thread.startRun({ parentId: "m2", sourceId: "m3" });
    await settle();
  });
  expect(posts[0]?.body).toEqual({ regenerate: "m3", model_id: "sonnet" });
  expect(result.current.state.messages.map((each) => each.id)).toEqual(["m1"]);
  await act(async () => {
    write(event("RUN_FINISHED", { threadId: CONVERSATION, runId: RUN }, 9));
    close();
    await settle();
  });
});

test("cancelling posts to the run and waits for the stream to say so", async () => {
  const { response, write, close } = writable({
    headers: streamHeaders(RUN, CONVERSATION),
  });
  const cancels: Call[] = [];
  stub((call) => {
    if (call.url === `/api/conversations/${CONVERSATION}`) {
      return json(opened(conversation(1), TREE));
    }
    if (call.url === `/api/conversations/${CONVERSATION}/turns`)
      return response;
    if (call.url.endsWith("/cancel")) {
      cancels.push(call);
      return json({
        id: RUN,
        state: "cancelled",
        started_at: "2026-09-22T10:00:00Z",
        ended_at: "2026-09-22T10:01:00Z",
      });
    }
    return undefined;
  });
  const { result } = chatting({ conversationId: CONVERSATION });
  await waitFor(() => {
    expect(result.current.state.messages).toHaveLength(2);
  });
  await act(async () => {
    result.current.runtime.thread.append("and then?");
    await settle();
  });
  write(event("TEXT_MESSAGE_START", { messageId: "m4", role: "assistant" }, 2));
  write(event("TEXT_MESSAGE_CONTENT", { messageId: "m4", delta: "half" }, 3));
  await settle();
  expect(result.current.state.runId).toBe(RUN);

  await act(async () => {
    result.current.runtime.thread.cancelRun();
    await settle();
  });
  expect(cancels[0]?.url).toBe(
    `/api/conversations/${CONVERSATION}/runs/${RUN}/cancel`,
  );
  // Still running: the run's own end is what ends it, and it comes as the
  // stream's `RUN_FINISHED` with the cancelled outcome.
  expect(result.current.state.runId).toBe(RUN);
  write(
    event(
      "RUN_FINISHED",
      { threadId: CONVERSATION, runId: RUN, outcome: { type: "cancelled" } },
      4,
    ),
  );
  close();
  await waitFor(() => {
    expect(result.current.state.runId).toBeNull();
  });
});

test("a turn a conversation that is answering refuses is said plainly", async () => {
  stub((call) => {
    if (call.url === `/api/conversations/${CONVERSATION}`) {
      return json(opened(conversation(1), TREE));
    }
    if (call.url === `/api/conversations/${CONVERSATION}/turns`) {
      return refusal(
        409,
        "RunAlreadyActiveError",
        `run ${RUN} is running in conversation ${CONVERSATION}; cancel it`,
      );
    }
    return undefined;
  });
  const { result } = chatting({ conversationId: CONVERSATION });
  await waitFor(() => {
    expect(result.current.state.messages).toHaveLength(2);
  });
  await act(async () => {
    result.current.runtime.thread.append("again");
    await settle();
  });
  // Not the backend's own detail, which names a run for an operator's log.
  expect(result.current.state.ended).toContain("still answering");
  expect(result.current.state.ended).not.toContain(RUN);
});

/**
 * The streams and the reads a lost connection walks through.
 *
 * `answers` is consulted in order for the calls to one url, so a route that
 * has to say one thing and then another can.
 */
function inTurn(answers: Record<string, (() => Response)[]>) {
  const at: Record<string, number> = {};
  return stub((call) => {
    const key = call.url.split("?")[0] ?? "";
    const queue = answers[key];
    if (queue === undefined) return undefined;
    const index = Math.min(at[key] ?? 0, queue.length - 1);
    at[key] = (at[key] ?? 0) + 1;
    return queue[index]?.();
  });
}

const streamOf = (...written: string[]) =>
  streamed(written, { headers: streamHeaders(RUN, CONVERSATION) });

const ANSWERING = {
  run_id: RUN,
  resume: { after: 2, follows: "m1" },
};

test("a stream that is lost reads the store and watches the run again", async () => {
  // The bug this is about: a connection that went used to fall through into a
  // reading whose answer put `run_id` back with nothing watching it, so the
  // thread said it was answering for ever.
  const fetch = inTurn({
    "/api/turns": [
      // Two events and then the connection goes: nothing said the run ended.
      () =>
        streamOf(
          event(
            "TEXT_MESSAGE_START",
            { messageId: "m2", role: "assistant" },
            2,
          ),
          event("TEXT_MESSAGE_CONTENT", { messageId: "m2", delta: "half" }, 3),
        ),
    ],
    [`/api/conversations/${CONVERSATION}/runs/${RUN}/events`]: [
      // The client's own tries are spent: a refusal is not one it repeats.
      () => refusal(404, "NotFoundError", "no"),
      // Read again, the run is still in flight, and this watch delivers.
      () =>
        streamOf(
          event(
            "TEXT_MESSAGE_START",
            { messageId: "m2", role: "assistant" },
            2,
          ),
          event("TEXT_MESSAGE_CONTENT", { messageId: "m2", delta: "whole" }, 3),
          event("TEXT_MESSAGE_END", { messageId: "m2" }, 4),
          event("RUN_FINISHED", { threadId: CONVERSATION, runId: RUN }, 5),
        ),
    ],
    [`/api/conversations/${CONVERSATION}`]: [
      () => json(opened(conversation(1), [TREE[0]!], ANSWERING)),
      () => json(opened(conversation(1), TREE)),
    ],
  });
  const { result } = chatting();
  await settle();
  await act(async () => {
    await result.current.runtime.thread.append("why?");
  });
  await waitFor(() => {
    expect(result.current.state.messages.map((each) => each.id)).toEqual([
      "m1",
      "m2",
    ]);
  });
  // The answer is the store's, nothing is running, and nothing was said
  // about a connection that came back.
  expect(result.current.state.runId).toBeNull();
  expect(result.current.state.ended).toBeNull();
  expect(
    fetch.mock.calls.filter(
      ([url]) =>
        String(url) === `/api/conversations/${CONVERSATION}/runs/${RUN}/events`,
    ),
  ).toHaveLength(2);
});

test("a second loss is said rather than retried for ever", async () => {
  inTurn({
    "/api/turns": [() => streamOf()],
    [`/api/conversations/${CONVERSATION}/runs/${RUN}/events`]: [
      () => refusal(404, "NotFoundError", "no"),
    ],
    [`/api/conversations/${CONVERSATION}`]: [
      () => json(opened(conversation(1), [TREE[0]!], ANSWERING)),
    ],
  });
  const { result } = chatting();
  await settle();
  await act(async () => {
    await result.current.runtime.thread.append("why?");
  });
  await waitFor(() => {
    expect(result.current.state.ended).not.toBeNull();
  });
  // In the chat's own words, not the client's.
  expect(result.current.state.ended).toBe(LOST_TOUCH);
  // **Not still answering.** The box works again, and so does the "try
  // again" on the last answer.
  expect(result.current.state.runId).toBeNull();
  expect(result.current.state.sending).toBe(false);
});

test("leaving a conversation stops a turn whose request is still in the air", async () => {
  // A stream is adopted only once its request has been answered, so between
  // the two there is a watcher-to-be; going away must stop that one too, or
  // a stream begins for a page nobody is on.
  const { response, write, close } = writable({
    headers: streamHeaders(RUN, CONVERSATION),
  });
  let arrived: (given: Response) => void = () => undefined;
  const fetch = stub((call) => {
    if (call.url.startsWith("/api/conversations/")) {
      return json(opened(conversation(1), TREE));
    }
    return undefined;
  });
  const { result, rerender, started, ended } = chatting({
    conversationId: CONVERSATION,
  });
  await waitFor(() => {
    expect(result.current.state.messages).toHaveLength(2);
  });
  fetch.mockImplementationOnce(
    () => new Promise<Response>((done) => (arrived = done)),
  );
  await act(async () => {
    void result.current.runtime.thread.append("and then?");
    await settle();
  });

  // "New chat" while the POST is in the air.
  await act(async () => {
    rerender({
      conversationId: null,
      agentId: "helper",
      modelId: "sonnet",
      onConversationStarted: started,
      onTurnEnded: ended,
    });
    await settle();
  });
  expect(result.current.state.messages).toHaveLength(0);

  await act(async () => {
    arrived(response);
    await settle();
  });
  // The stream that arrived late is not adopted: the empty chat is still
  // the empty chat.
  expect(result.current.state.runId).toBeNull();
  expect(result.current.state.messages).toHaveLength(0);
  write(event("TEXT_MESSAGE_START", { messageId: "m4", role: "assistant" }, 2));
  await settle();
  expect(result.current.state.messages).toHaveLength(0);
  close();
});

test("a first turn that lands after the person moved on does not take them back", async () => {
  // The POST of a first message resolves while somebody is reading another
  // conversation: the shell must not be routed to the new one, and the one
  // they opened must be read rather than skipped as "ours".
  const other = id(2);
  const { response, write, close } = writable({
    headers: streamHeaders(RUN, CONVERSATION),
  });
  let arrived: (given: Response) => void = () => undefined;
  const posts: Call[] = [];
  const fetch = stub((call) => {
    if (call.url === `/api/conversations/${other}`) {
      return json(opened(conversation(2, "Elsewhere"), TREE));
    }
    if (call.url === `/api/conversations/${other}/turns`) {
      posts.push(call);
      return streamOf(
        event("RUN_FINISHED", { threadId: other, runId: RUN }, 9),
      );
    }
    return undefined;
  });
  const { result, rerender, started, ended } = chatting();
  await settle();
  fetch.mockImplementationOnce(
    () => new Promise<Response>((done) => (arrived = done)),
  );
  await act(async () => {
    void result.current.runtime.thread.append("why?");
    await settle();
  });

  // Another conversation is opened while that POST is in the air.
  await act(async () => {
    rerender({
      conversationId: other,
      agentId: "helper",
      modelId: "sonnet",
      onConversationStarted: started,
      onTurnEnded: ended,
    });
    await settle();
  });
  await act(async () => {
    arrived(response);
    await settle();
  });

  // The shell is not sent anywhere, and the conversation that *was* opened
  // is the one on the screen.
  expect(started).not.toHaveBeenCalled();
  await waitFor(() => {
    expect(result.current.state.conversationId).toBe(other);
  });
  expect(result.current.state.messages.map((each) => each.id)).toEqual([
    "m1",
    "m2",
  ]);
  expect(result.current.state.runId).toBeNull();

  // And the next message goes to the conversation on the screen.
  await act(async () => {
    await result.current.runtime.thread.append("and then?");
  });
  expect(posts.map((each) => each.url)).toEqual([
    `/api/conversations/${other}/turns`,
  ]);
  write(event("RUN_FINISHED", { threadId: CONVERSATION, runId: RUN }, 2));
  close();
});

test("a read that finds a run in flight watches it, whichever read it is", async () => {
  // Another tab began a run between this one's turn ending and its read.
  // Taking the id without watching it would leave the thread answering for
  // ever with nothing reading the answer.
  const OTHER_RUN = "22222222-3333-4444-8555-666666666666";
  const fetch = inTurn({
    [`/api/conversations/${CONVERSATION}/turns`]: [
      () =>
        streamOf(
          event(
            "TEXT_MESSAGE_START",
            { messageId: "m4", role: "assistant" },
            2,
          ),
          event("TEXT_MESSAGE_END", { messageId: "m4" }, 3),
          event("RUN_FINISHED", { threadId: CONVERSATION, runId: RUN }, 4),
        ),
    ],
    [`/api/conversations/${CONVERSATION}`]: [
      () => json(opened(conversation(1), TREE)),
      // The read that ends the turn: somebody else is answering now.
      () =>
        json(
          opened(conversation(1), TREE, {
            run_id: OTHER_RUN,
            resume: { after: 1, follows: "m2" },
          }),
        ),
      () => json(opened(conversation(1), TREE)),
    ],
    [`/api/conversations/${CONVERSATION}/runs/${OTHER_RUN}/events`]: [
      () =>
        streamed(
          [
            event(
              "RUN_FINISHED",
              { threadId: CONVERSATION, runId: OTHER_RUN },
              5,
            ),
          ],
          { headers: streamHeaders(OTHER_RUN, CONVERSATION) },
        ),
    ],
  });
  const { result, ended } = chatting({ conversationId: CONVERSATION });
  await waitFor(() => {
    expect(result.current.state.messages).toHaveLength(2);
  });
  await act(async () => {
    await result.current.runtime.thread.append("and then?");
  });
  await waitFor(() => {
    expect(result.current.state.runId).toBeNull();
  });
  // The run somebody else began was watched, and its end left nothing
  // answering here.
  expect(
    fetch.mock.calls.filter(
      ([url]) =>
        String(url) ===
        `/api/conversations/${CONVERSATION}/runs/${OTHER_RUN}/events`,
    ),
  ).toHaveLength(1);
  // And the panel was still told, though the turn's own watcher was stopped
  // to make room for that one.
  expect(ended).toHaveBeenCalled();
});

test("stopping before the first token leaves the box empty", async () => {
  // assistant-ui's own stop would take the trailing question out of its
  // repository and put its text into the box -- but only for a host that
  // offers `setMessages`, which this one does not (`runtime.tsx`). The
  // backend stored that question when the turn began, so it stays.
  const { response, write, close } = writable({
    headers: streamHeaders(RUN, CONVERSATION),
  });
  stub((call) => {
    if (call.url === `/api/conversations/${CONVERSATION}`) {
      return json(opened(conversation(1), TREE));
    }
    if (call.url.endsWith("/cancel")) return json({ id: RUN });
    if (call.url === `/api/conversations/${CONVERSATION}/turns`)
      return response;
    return undefined;
  });
  const { result } = chatting({ conversationId: CONVERSATION });
  await waitFor(() => {
    expect(result.current.state.messages).toHaveLength(2);
  });
  await act(async () => {
    void result.current.runtime.thread.append("and then?");
    await settle();
  });
  // The run exists and has said nothing yet.
  expect(result.current.state.runId).toBe(RUN);
  await act(async () => {
    result.current.runtime.thread.cancelRun();
    await settle();
  });
  expect(result.current.runtime.thread.composer.getState().text).toBe("");
  // The question is still in the thread, not back in the box.
  expect(
    result.current.state.messages.some(
      (each) =>
        each.role === "user" &&
        each.parts[0]?.kind === "text" &&
        each.parts[0].text === "and then?",
    ),
  ).toBe(true);
  write(
    event(
      "RUN_FINISHED",
      { threadId: CONVERSATION, runId: RUN, outcome: { type: "cancelled" } },
      2,
    ),
  );
  close();
  await waitFor(() => {
    expect(result.current.state.runId).toBeNull();
  });
});

test("a refused retry leaves the question it was retrying on the thread", async () => {
  // The thread ends on a question nobody answered, so the retry goes after
  // that question's *parent* (`under`) and takes its place on the screen. A
  // refusal must put the question back.
  inTurn({
    [`/api/conversations/${CONVERSATION}`]: [
      () =>
        json(
          opened(conversation(1), [TREE[0]!], {
            ended_badly: {
              run_id: RUN,
              state: "failed",
              ended_at: "2026-09-23T10:00:00Z",
            },
          }),
        ),
    ],
    [`/api/conversations/${CONVERSATION}/turns`]: [
      () => refusal(409, "RunAlreadyActiveError", `run ${RUN} is running`),
    ],
  });
  const { result } = chatting({ conversationId: CONVERSATION });
  await waitFor(() => {
    expect(result.current.state.messages).toHaveLength(1);
  });
  await act(async () => {
    await result.current.runtime.thread.append("why, really?");
  });
  // The question that went unanswered is a message the conversation really
  // has: it is still there, and still the end of the thread.
  expect(result.current.state.messages.map((each) => each.id)).toEqual(["m1"]);
});

test("the model goes with every turn", async () => {
  const posts: Call[] = [];
  const fetch = stub((call) => {
    if (call.url === "/api/turns") {
      return streamed(
        [event("RUN_FINISHED", { threadId: CONVERSATION, runId: RUN }, 2)],
        { headers: streamHeaders(RUN, CONVERSATION) },
      );
    }
    if (call.url === `/api/conversations/${CONVERSATION}/turns`) {
      posts.push(call);
      return streamed(
        [event("RUN_FINISHED", { threadId: CONVERSATION, runId: RUN }, 9)],
        { headers: streamHeaders(RUN, CONVERSATION) },
      );
    }
    if (call.url === `/api/conversations/${CONVERSATION}`) {
      return json(opened(conversation(1), TREE));
    }
    return undefined;
  });
  const { result, rerender, started, ended } = chatting({ modelId: "opus" });
  await settle();
  await act(async () => {
    await result.current.runtime.thread.append("why?");
  });
  expect(
    (JSON.parse(String(fetch.mock.calls[0]?.[1]?.body)) as { model_id: string })
      .model_id,
  ).toBe("opus");
  // The shell routes to the conversation, as it does (`Shell.tsx`).
  rerender({
    conversationId: CONVERSATION,
    agentId: "helper",
    modelId: "opus",
    onConversationStarted: started,
    onTurnEnded: ended,
  });
  await waitFor(() => {
    expect(result.current.state.runId).toBeNull();
    expect(result.current.state.messages).toHaveLength(2);
  });
  await act(async () => {
    await result.current.runtime.thread.append("and then?");
  });
  await waitFor(() => {
    expect(result.current.state.runId).toBeNull();
  });
  await act(async () => {
    result.current.runtime.thread.startRun({ parentId: "m1", sourceId: "m2" });
    await settle();
  });
  // A continued turn and a regeneration name the model too: it goes with
  // every turn.
  expect(posts).toHaveLength(2);
  for (const post of posts) {
    expect(post.body).toHaveProperty("model_id", "opus");
  }
});

test("a turn refused because the model has gone says so", async () => {
  // The backend names the refusal (`docs/specs/wire.md`): the conversation's
  // model, and not the conversation, is what stands in the way.
  const answers = (refused: () => Response) =>
    stub((call) => {
      if (call.url === `/api/conversations/${CONVERSATION}`) {
        return json(opened(conversation(1), TREE));
      }
      if (call.url === `/api/conversations/${CONVERSATION}/turns`) {
        return refused();
      }
      return undefined;
    });
  answers(() =>
    refusal(409, "ModelNotOfferedError", "this conversation's model is gone"),
  );
  const gone = chatting({ conversationId: CONVERSATION });
  await waitFor(() => {
    expect(gone.result.current.state.messages).toHaveLength(2);
  });
  await act(async () => {
    await gone.result.current.runtime.thread.append("and then?");
    await settle();
  });
  expect(gone.result.current.state.ended).toBe(MODEL_GONE);
  // What is asked of them is to pick another model and send again, so what
  // they wrote is back in the box to send.
  expect(gone.result.current.runtime.thread.composer.getState().text).toBe(
    "and then?",
  );
  expect(gone.result.current.state.messages.map((each) => each.id)).toEqual([
    "m1",
    "m2",
  ]);
  gone.unmount();

  // A 404 is what the backend says it is, and nothing is put back.
  answers(() =>
    refusal(404, "NotFoundError", "there is nothing here of that id"),
  );
  const there = chatting({ conversationId: CONVERSATION });
  await waitFor(() => {
    expect(there.result.current.state.messages).toHaveLength(2);
  });
  await act(async () => {
    await there.result.current.runtime.thread.append("and then?");
    await settle();
  });
  expect(there.result.current.state.ended).toBe(
    "there is nothing here of that id",
  );
  expect(there.result.current.runtime.thread.composer.getState().text).toBe("");
});

test("an edit refused because the model has gone goes back into its edit box", async () => {
  stub((call) => {
    if (call.url === `/api/conversations/${CONVERSATION}`) {
      return json(opened(conversation(1), TREE));
    }
    if (call.url === `/api/conversations/${CONVERSATION}/turns`) {
      return refusal(409, "ModelNotOfferedError", "the model is gone");
    }
    return undefined;
  });
  const { result } = chatting({ conversationId: CONVERSATION });
  await waitFor(() => {
    expect(result.current.state.messages).toHaveLength(2);
  });
  await act(async () => {
    result.current.runtime.thread.append({
      role: "user",
      content: [{ type: "text", text: "why, really?" }],
      parentId: null,
      sourceId: "m1",
    });
    await settle();
  });
  expect(result.current.state.ended).toBe(MODEL_GONE);
  // Still an edit of the message it was of, so that sending it again once
  // another model is picked replaces that message rather than adding one.
  const edit = result.current.runtime.thread.getMessageById("m1").composer;
  expect(edit.getState().isEditing).toBe(true);
  expect(edit.getState().text).toBe("why, really?");
  expect(result.current.runtime.thread.composer.getState().text).toBe("");
  expect(result.current.state.messages.map((each) => each.id)).toEqual([
    "m1",
    "m2",
  ]);
});

test("a regeneration refused because the model has gone says so", async () => {
  stub((call) => {
    if (call.url === `/api/conversations/${CONVERSATION}`) {
      return json(opened(conversation(1), TREE));
    }
    if (call.url === `/api/conversations/${CONVERSATION}/turns`) {
      return refusal(409, "ModelNotOfferedError", "the model is gone");
    }
    return undefined;
  });
  const { result } = chatting({ conversationId: CONVERSATION });
  await waitFor(() => {
    expect(result.current.state.messages).toHaveLength(2);
  });
  await act(async () => {
    result.current.runtime.thread.startRun({ parentId: "m1", sourceId: "m2" });
    await settle();
  });
  expect(result.current.state.ended).toBe(MODEL_GONE);
  // The answer it would have replaced is back where it was.
  expect(result.current.state.messages.map((each) => each.id)).toEqual([
    "m1",
    "m2",
  ]);
});

test("an edit refused because the conversation is answering goes back into its edit box", async () => {
  // Another tab began a run: the server refuses the edit (409), and sending
  // it again once that answer is done has to be an edit still.
  stub((call) => {
    if (call.url === `/api/conversations/${CONVERSATION}`) {
      return json(opened(conversation(1), TREE));
    }
    if (call.url === `/api/conversations/${CONVERSATION}/turns`) {
      return refusal(409, "RunAlreadyActiveError", `run ${RUN} is running`);
    }
    return undefined;
  });
  const { result } = chatting({ conversationId: CONVERSATION });
  await waitFor(() => {
    expect(result.current.state.messages).toHaveLength(2);
  });
  await act(async () => {
    result.current.runtime.thread.append({
      role: "user",
      content: [{ type: "text", text: "why, really?" }],
      parentId: null,
      sourceId: "m1",
    });
    await settle();
  });
  expect(result.current.state.ended).toBe(STILL_ANSWERING);
  const edit = result.current.runtime.thread.getMessageById("m1").composer;
  expect(edit.getState().isEditing).toBe(true);
  expect(edit.getState().text).toBe("why, really?");
  expect(result.current.runtime.thread.composer.getState().text).toBe("");
});

/** A conversation of `TREE` with a run going in it, as a chat sees it. */
async function answering() {
  const { response } = writable({
    headers: streamHeaders(RUN, CONVERSATION),
  });
  stub((call) => {
    if (call.url === `/api/conversations/${CONVERSATION}`) {
      return json(opened(conversation(1), TREE));
    }
    if (call.url === `/api/conversations/${CONVERSATION}/turns`) {
      return response;
    }
    return undefined;
  });
  const chat = chatting({ conversationId: CONVERSATION });
  await waitFor(() => {
    expect(chat.result.current.state.messages).toHaveLength(2);
  });
  await act(async () => {
    void chat.result.current.runtime.thread.append("and then?");
    await settle();
  });
  expect(chat.result.current.state.runId).toBe(RUN);
  return chat;
}

test("an edit not sent while a run is going goes back into its edit box", async () => {
  const { result } = await answering();
  await act(async () => {
    void result.current.runtime.thread.append({
      role: "user",
      content: [{ type: "text", text: "why, really?" }],
      parentId: null,
      sourceId: "m1",
    });
    await settle();
  });
  expect(result.current.state.notice).toBe(ONE_AT_A_TIME);
  const edit = result.current.runtime.thread.getMessageById("m1").composer;
  expect(edit.getState().isEditing).toBe(true);
  expect(edit.getState().text).toBe("why, really?");
  expect(result.current.runtime.thread.composer.getState().text).toBe("");
});

test("an edit whose message has left the thread is kept in the main box", async () => {
  // Nothing to reopen: asking the runtime for it would throw, and the chat
  // would be replaced by the error boundary. The text is kept all the same.
  const { result } = await answering();
  await act(async () => {
    void result.current.runtime.thread.append({
      role: "user",
      content: [{ type: "text", text: "why, really?" }],
      parentId: null,
      sourceId: "gone",
    });
    await settle();
  });
  expect(result.current.state.notice).toBe(ONE_AT_A_TIME);
  expect(result.current.runtime.thread.composer.getState().text).toBe(
    "why, really?",
  );
});

test("a first message refused for its model says so, and hands the model back", async () => {
  stub((call) =>
    call.url === "/api/turns"
      ? refusal(422, "UnknownModelError", "body.model_id: is not a model")
      : undefined,
  );
  const onModelRefused = vi.fn();
  const { result } = chatting({ modelId: "retired", onModelRefused });
  await settle();
  await act(async () => {
    await result.current.runtime.thread.append("why?");
    await settle();
  });
  expect(result.current.state.ended).toBe(MODEL_GONE_NEW_CHAT);
  expect(result.current.state.messages).toHaveLength(0);
  expect(result.current.runtime.thread.composer.getState().text).toBe("why?");
  expect(onModelRefused).toHaveBeenCalledWith("retired");
});

test("a first message refused as not there is about its agent, and keeps the text", async () => {
  // No conversation yet, so a 404 is the agent: nothing about the model.
  stub((call) =>
    call.url === "/api/turns"
      ? refusal(404, "NotFoundError", "there is nothing here")
      : undefined,
  );
  const onModelRefused = vi.fn();
  const { result } = chatting({ modelId: "sonnet", onModelRefused });
  await settle();
  await act(async () => {
    await result.current.runtime.thread.append("why?");
    await settle();
  });
  expect(result.current.state.ended).toBe(AGENT_GONE);
  expect(result.current.runtime.thread.composer.getState().text).toBe("why?");
  expect(onModelRefused).not.toHaveBeenCalled();
});

test("a stop that does not reach the server leaves the answer alone", async () => {
  const { response, write, close } = writable({
    headers: streamHeaders(RUN, CONVERSATION),
  });
  stub((call) => {
    if (call.url === `/api/conversations/${CONVERSATION}`) {
      return json(opened(conversation(1), TREE));
    }
    if (call.url.endsWith("/cancel")) {
      return refusal(500, "InternalError", "something went wrong");
    }
    if (call.url === `/api/conversations/${CONVERSATION}/turns`)
      return response;
    return undefined;
  });
  const { result } = chatting({ conversationId: CONVERSATION });
  await waitFor(() => {
    expect(result.current.state.messages).toHaveLength(2);
  });
  await act(async () => {
    void result.current.runtime.thread.append("and then?");
    await settle();
  });
  write(event("TEXT_MESSAGE_START", { messageId: "m4", role: "assistant" }, 2));
  await settle();

  await act(async () => {
    result.current.runtime.thread.cancelRun();
    await settle();
  });
  // **Nothing was cancelled**, so the run is still the run and the stream is
  // still watching it: the answer carries on arriving.
  expect(result.current.state.notice).toBe(STOP_DID_NOT_ARRIVE);
  expect(result.current.state.runId).toBe(RUN);
  write(event("TEXT_MESSAGE_CONTENT", { messageId: "m4", delta: "still" }, 3));
  await settle();
  expect(
    result.current.state.messages.find((each) => each.id === "m4")?.parts,
  ).toEqual([{ kind: "text", text: "still" }]);
  write(event("RUN_FINISHED", { threadId: CONVERSATION, runId: RUN }, 4));
  close();
  await waitFor(() => {
    expect(result.current.state.runId).toBeNull();
  });
});

test("a turn the backend refuses takes its question back off the screen", async () => {
  // The 409 a conversation that is already answering makes: another tab
  // started a run between this one's reading and its send.
  inTurn({
    [`/api/conversations/${CONVERSATION}`]: [
      () => json(opened(conversation(1), TREE)),
    ],
    [`/api/conversations/${CONVERSATION}/turns`]: [
      () =>
        refusal(409, "RunAlreadyActiveError", `run ${RUN} is running in it`),
    ],
  });
  const { result } = chatting({ conversationId: CONVERSATION });
  await waitFor(() => {
    expect(result.current.state.messages).toHaveLength(2);
  });
  await act(async () => {
    await result.current.runtime.thread.append("and then?");
  });
  expect(result.current.state.ended).toContain("still answering");
  // Not left on the screen as though it were in the conversation: the
  // thread is back as it was.
  expect(result.current.state.messages.map((each) => each.id)).toEqual([
    "m1",
    "m2",
  ]);
  expect(result.current.state.sending).toBe(false);
  expect(result.current.state.runId).toBeNull();
});

test("a second turn while one is in flight is said, not swallowed", async () => {
  // The box empties itself when it hands a message over, so a turn dropped
  // in silence is a message somebody believes they sent.
  const { response, write, close } = writable({
    headers: streamHeaders(RUN, CONVERSATION),
  });
  const fetch = stub((call) => {
    if (call.url === `/api/conversations/${CONVERSATION}`) {
      return json(opened(conversation(1), TREE));
    }
    if (call.url === `/api/conversations/${CONVERSATION}/turns`)
      return response;
    return undefined;
  });
  const posted = () =>
    fetch.mock.calls.filter(
      ([url]) => String(url) === `/api/conversations/${CONVERSATION}/turns`,
    ).length;
  const { result } = chatting({ conversationId: CONVERSATION });
  await waitFor(() => {
    expect(result.current.state.messages).toHaveLength(2);
  });

  // While the first turn's request is still in the air.
  let arrived: (given: Response) => void = () => undefined;
  fetch.mockImplementationOnce(
    () => new Promise<Response>((done) => (arrived = done)),
  );
  await act(async () => {
    void result.current.runtime.thread.append("and then?");
    await settle();
  });
  await act(async () => {
    void result.current.runtime.thread.append("impatient");
    await settle();
  });
  expect(result.current.state.notice).toBe(ONE_AT_A_TIME);
  // The turn that *is* on its way keeps its question, and only it was sent.
  expect(
    result.current.state.messages.filter((each) => each.role === "user"),
  ).toHaveLength(2);
  expect(posted()).toBe(1);

  await act(async () => {
    arrived(response);
    await settle();
  });
  // **The run starting does not wipe it.** It is about what the person did,
  // not about the run, and a notice gone within milliseconds is the only
  // trace of a message that went nowhere disappearing before it is read.
  expect(result.current.state.notice).toBe(ONE_AT_A_TIME);
  write(event("RUN_FINISHED", { threadId: CONVERSATION, runId: RUN }, 2));
  close();
  await waitFor(() => {
    expect(result.current.state.runId).toBeNull();
  });
  expect(result.current.state.notice).toBe(ONE_AT_A_TIME);

  // Their next turn is what clears it.
  await act(async () => {
    await result.current.runtime.thread.append("now then?");
  });
  expect(result.current.state.notice).toBeNull();
});

test("a second turn while one is in flight is not sent at all", async () => {
  const { response, write, close } = writable({
    headers: streamHeaders(RUN, CONVERSATION),
  });
  let posted = 0;
  stub((call) => {
    if (call.url === `/api/conversations/${CONVERSATION}`) {
      return json(opened(conversation(1), TREE));
    }
    if (call.url === `/api/conversations/${CONVERSATION}/turns`) {
      posted += 1;
      return response;
    }
    return undefined;
  });
  const { result } = chatting({ conversationId: CONVERSATION });
  await waitFor(() => {
    expect(result.current.state.messages).toHaveLength(2);
  });
  await act(async () => {
    void result.current.runtime.thread.append("and then?");
    await settle();
  });
  write(event("TEXT_MESSAGE_START", { messageId: "m4", role: "assistant" }, 2));
  await settle();
  expect(result.current.state.runId).toBe(RUN);

  // One run at a time (`docs/specs/runs.md`), and the run that is going is
  // left exactly as it is.
  await act(async () => {
    void result.current.runtime.thread.append("impatient");
    await settle();
  });
  expect(result.current.state.notice).toBe(ONE_AT_A_TIME);
  // A regeneration carries no message, so it is told so without being told
  // that a message was not sent.
  await act(async () => {
    void result.current.runtime.thread.startRun({
      parentId: "m1",
      sourceId: "m2",
    });
    await settle();
  });
  expect(result.current.state.notice).toBe(ONE_AT_A_TIME_ANSWER);
  expect(ONE_AT_A_TIME_ANSWER).not.toContain("was not sent");
  expect(posted).toBe(1);
  expect(result.current.state.runId).toBe(RUN);
  expect(result.current.state.messages.some((each) => each.id === "m4")).toBe(
    true,
  );

  write(event("TEXT_MESSAGE_CONTENT", { messageId: "m4", delta: "still" }, 3));
  await settle();
  expect(
    result.current.state.messages.find((each) => each.id === "m4")?.parts,
  ).toEqual([{ kind: "text", text: "still" }]);
  write(event("RUN_FINISHED", { threadId: CONVERSATION, runId: RUN }, 4));
  close();
  await waitFor(() => {
    expect(result.current.state.runId).toBeNull();
  });
});

test("nothing is running until the run exists", async () => {
  // The library's own stop takes a trailing question out of the thread and
  // puts its text back into the box. Between the send and the response's
  // headers there is no run to stop, so the thread must not offer one --
  // `isRunning` is a run this chat knows the id of and nothing else.
  const { response, write, close } = writable({
    headers: streamHeaders(RUN, CONVERSATION),
  });
  const cancels: Call[] = [];
  const fetch = stub((call) => {
    if (call.url === `/api/conversations/${CONVERSATION}`) {
      return json(opened(conversation(1), TREE));
    }
    if (call.url.endsWith("/cancel")) {
      cancels.push(call);
      return json({ id: RUN, state: "cancelled" });
    }
    if (call.url === `/api/conversations/${CONVERSATION}/turns`)
      return response;
    return undefined;
  });
  const { result } = chatting({ conversationId: CONVERSATION });
  await waitFor(() => {
    expect(result.current.state.messages).toHaveLength(2);
  });

  // Held: the POST has been made and its headers have not arrived.
  let arrived: (given: Response) => void = () => undefined;
  fetch.mockImplementationOnce(
    () => new Promise<Response>((done) => (arrived = done)),
  );
  await act(async () => {
    void result.current.runtime.thread.append("and then?");
    await settle();
  });
  expect(result.current.state.sending).toBe(true);
  expect(result.current.state.runId).toBeNull();
  expect(result.current.runtime.thread.getState().isRunning).toBe(false);
  // And there is nothing to stop: no request goes out for one.
  await act(async () => {
    result.current.runtime.thread.cancelRun();
    await settle();
  });
  expect(cancels).toHaveLength(0);

  await act(async () => {
    arrived(response);
    await settle();
  });
  expect(result.current.state.runId).toBe(RUN);
  expect(result.current.runtime.thread.getState().isRunning).toBe(true);
  write(event("RUN_FINISHED", { threadId: CONVERSATION, runId: RUN }, 2));
  close();
  await waitFor(() => {
    expect(result.current.state.runId).toBeNull();
  });
});

test("an edit under a question the server has never seen is not sent", async () => {
  const posts: Call[] = [];
  stub((call) => {
    if (call.url === `/api/conversations/${CONVERSATION}`) {
      return json(opened(conversation(1), TREE));
    }
    if (call.url === `/api/conversations/${CONVERSATION}/turns`) {
      posts.push(call);
      return streamOf(
        event("RUN_FINISHED", { threadId: CONVERSATION, runId: RUN }, 9),
      );
    }
    return undefined;
  });
  const { result } = chatting({ conversationId: CONVERSATION });
  await waitFor(() => {
    expect(result.current.state.messages).toHaveLength(2);
  });
  await act(async () => {
    void result.current.runtime.thread.append({
      role: "user",
      content: [{ type: "text", text: "under nothing" }],
      parentId: "unsent:whatever",
      sourceId: "unsent:whatever",
    });
    await settle();
  });
  expect(posts).toHaveLength(0);
});

test("the tree is converted once per message, however many deltas arrive", () => {
  // A delta rebuilds the repository, and a conversation is as long as it is.
  const messages = Array.from({ length: 4 }, (_, index) => ({
    id: `m${index}`,
    role: "user" as const,
    parts: [{ kind: "text" as const, text: "hello" }],
    state: "stored" as const,
  }));
  const first = asRepository(messages);
  const again = asRepository(messages);
  expect(again.messages.map((each) => each.message)).toEqual(
    first.messages.map((each) => each.message),
  );
  for (const [at, item] of again.messages.entries()) {
    // The same object, not an equal one: nothing was built a second time,
    // and `createdAt` did not move.
    expect(item.message).toBe(first.messages[at]?.message);
  }
  // A message the reducer touched is a new object, and only that one is
  // converted again.
  const touched = [...messages];
  touched[2] = { ...messages[2]!, parts: [{ kind: "text", text: "changed" }] };
  const third = asRepository(touched);
  expect(third.messages[1]?.message).toBe(first.messages[1]?.message);
  expect(third.messages[2]?.message).not.toBe(first.messages[2]?.message);
});

test("a conversation that is not here is one answer for two reasons", async () => {
  stub(() => refusal(404, "NotFoundError", "there is nothing here of that id"));
  const { result } = chatting({ conversationId: CONVERSATION });
  await waitFor(() => {
    expect(result.current.state.failure?.missing).toBe(true);
  });
  // And nothing is being watched behind that page.
  expect(result.current.state.runId).toBeNull();
  expect(result.current.state.sending).toBe(false);
});

test("a read that says the same thing hands back the same messages", () => {
  // Identity is what the chat converts for assistant-ui by, so a read that
  // ends a turn must not rebuild a whole conversation over one answer.
  const stored = [
    message("m1", "user", "why?"),
    message("m2", "assistant", "because"),
  ];
  const first = after({
    kind: "opened",
    conversationId: CONVERSATION,
    messages: stored,
    runId: null,
    endedBadly: null,
  });
  const again = reduce(first, {
    kind: "opened",
    conversationId: CONVERSATION,
    // The same conversation with one more answer, as a turn leaves it.
    messages: [...stored, message("m3", "user", "and then?")],
    runId: null,
    endedBadly: null,
  });
  expect(again.messages[0]).toBe(first.messages[0]);
  expect(again.messages[1]).toBe(first.messages[1]);
  expect(again.messages[2]?.id).toBe("m3");
  // Which is what keeps the conversions, and `createdAt` with them.
  expect(asRepository(again.messages).messages[0]?.message).toBe(
    asRepository(first.messages).messages[0]?.message,
  );
  // A message the store now says something else about is rebuilt.
  const edited = reduce(first, {
    kind: "opened",
    conversationId: CONVERSATION,
    messages: [stored[0]!, message("m2", "assistant", "because of this")],
    runId: null,
    endedBadly: null,
  });
  expect(edited.messages[0]).toBe(first.messages[0]);
  expect(edited.messages[1]).not.toBe(first.messages[1]);
});

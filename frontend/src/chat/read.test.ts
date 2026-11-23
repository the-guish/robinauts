// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import { expect, test } from "vitest";

import type { ThreadToolCall } from "./index";
import { lastRead, sameRead, sameToolCalls, type Read } from "./state";

const SHOWN: Read = { conversationId: "c", lastId: "m2", runId: null };

test.each<[string, Read | null, Read, boolean]>([
  ["the same read", SHOWN, { ...SHOWN }, true],
  ["nothing shown yet", null, SHOWN, false],
  ["a new last message", SHOWN, { ...SHOWN, lastId: "m3" }, false],
  ["a turn now running", SHOWN, { ...SHOWN, runId: "r" }, false],
  ["another conversation", SHOWN, { ...SHOWN, conversationId: "d" }, false],
])("%s: redraw skipped is %s", (_, before, after, same) => {
  expect(sameRead(before, after)).toBe(same);
});

test.each<[{ id: string }[], string | null, string | null]>([
  [[], null, null],
  [[{ id: "m1" }, { id: "m2" }], "r", "m2"],
])("a read of %j with run %s ends at %s", (messages, runId, lastId) => {
  expect(lastRead("c", { messages, run_id: runId })).toEqual({
    conversationId: "c",
    lastId,
    runId,
  });
});

const ARGS = { shape: "box" };
const CALL: ThreadToolCall = {
  messageId: "m2",
  callId: "t1",
  name: "draw",
  arguments: ARGS,
  state: "running",
  live: true,
};

test.each<[string, ThreadToolCall[], ThreadToolCall[], boolean]>([
  ["none and none", [], [], true],
  ["the same call, made again", [CALL], [{ ...CALL }], true],
  // Read again from the store: other objects that say the same.
  ["equal arguments", [CALL], [{ ...CALL, arguments: { ...ARGS } }], true],
  ["other arguments", [CALL], [{ ...CALL, arguments: { shape: "o" } }], false],
  ["arguments now whole", [{ ...CALL, arguments: null }], [CALL], false],
  ["a call answered", [CALL], [{ ...CALL, state: "answered" }], false],
  ["a call no longer live", [CALL], [{ ...CALL, live: false }], false],
  ["another call", [CALL], [{ ...CALL, callId: "t2" }], false],
  ["another answer", [CALL], [{ ...CALL, messageId: "m4" }], false],
  ["another tool", [CALL], [{ ...CALL, name: "show" }], false],
  ["one more call", [CALL], [CALL, { ...CALL, callId: "t2" }], false],
])("%s: the same calls is %s", (_, one, other, same) => {
  expect(sameToolCalls(one, other)).toBe(same);
  expect(sameToolCalls(other, one)).toBe(same);
});

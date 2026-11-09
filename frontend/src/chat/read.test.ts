// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import { expect, test } from "vitest";

import { lastRead, sameRead, type Read } from "./state";

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

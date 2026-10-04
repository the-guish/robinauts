// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import { expect, test, vi } from "vitest";

import { message } from "../test/conversations";
import { cancelRun, shownTitle, textOf } from "./conversation";

test("a message says its text parts, joined", () => {
  expect(textOf(message("m", "user", "Hello"))).toBe("Hello");
  expect(
    textOf({
      ...message("m", "assistant", ""),
      parts: [
        { kind: "text", text: "A long " },
        { kind: "text", text: "answer." },
      ],
    }),
  ).toBe("A long answer.");
});

test("a conversation nobody named is shown as Untitled", () => {
  expect(shownTitle("Named")).toBe("Named");
  expect(shownTitle("")).toBe("Untitled");
  expect(shownTitle("   ")).toBe("Untitled");
});

test("a stop the server accepts is a stop, whatever its body", async () => {
  for (const [status, body] of [
    [202, null],
    [202, "not json"],
    [204, null],
  ] as const) {
    vi.stubGlobal(
      "fetch",
      vi
        .fn<typeof globalThis.fetch>()
        .mockResolvedValue(new Response(body, { status })),
    );
    await expect(cancelRun("c-1", "r-1")).resolves.toBeUndefined();
  }
  vi.stubGlobal(
    "fetch",
    vi
      .fn<typeof globalThis.fetch>()
      .mockResolvedValue(new Response("{}", { status: 404 })),
  );
  await expect(cancelRun("c-1", "r-1")).rejects.toThrow();
  vi.unstubAllGlobals();
});

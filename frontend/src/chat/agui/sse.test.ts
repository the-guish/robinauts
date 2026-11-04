// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import { expect, test } from "vitest";

import { ApiError } from "../../api/client";
import { streamed, writable } from "../../test/stream";
import { blocks, MAX_BLOCK, type SseBlock } from "./sse";

/** Every block of that body, with the chunks arriving as they are written. */
async function read(
  chunks: readonly string[],
  signal?: AbortSignal,
): Promise<SseBlock[]> {
  const body = streamed(chunks).body;
  if (body === null) throw new Error("no body");
  const seen: SseBlock[] = [];
  for await (const block of blocks(body, signal)) seen.push(block);
  return seen;
}

test("one event, in one chunk", async () => {
  expect(await read(["id: 3\nevent: RUN_STARTED\ndata: {}\n\n"])).toEqual([
    { id: "3", event: "RUN_STARTED", data: "{}" },
  ]);
});

test("a chunk may end anywhere, including in the middle of a line", async () => {
  const whole = "id: 7\nevent: TEXT\ndata: hello\n\nevent: END\ndata: x\n\n";
  // Every way of cutting it in two says the same thing.
  for (let at = 1; at < whole.length; at += 1) {
    const seen = await read([whole.slice(0, at), whole.slice(at)]);
    expect(seen).toEqual([
      { id: "7", event: "TEXT", data: "hello" },
      { id: null, event: "END", data: "x" },
    ]);
  }
});

test("\\r\\n and a bare \\r end a line, even split across chunks", async () => {
  expect(await read(["event: A\r\ndata: one\r\n\r\n"])).toEqual([
    { id: null, event: "A", data: "one" },
  ]);
  // The `\r` arrives at the end of one chunk and its `\n` at the start of the
  // next: read as a line ending each, this would dispatch an empty block.
  expect(await read(["event: A\r", "\ndata: one\r", "\n\r", "\n"])).toEqual([
    { id: null, event: "A", data: "one" },
  ]);
  expect(await read(["event: A\rdata: one\r\r"])).toEqual([
    { id: null, event: "A", data: "one" },
  ]);
});

test("several data lines are one value, joined with newlines", async () => {
  expect(await read(["data: one\ndata: two\ndata:\n\n"])).toEqual([
    { id: null, event: null, data: "one\ntwo\n" },
  ]);
});

test("one optional space after the colon, and no more", async () => {
  expect(await read(["data:  two spaces\n\n"])).toEqual([
    { id: null, event: null, data: " two spaces" },
  ]);
  // A field with no colon at all is the field with an empty value.
  expect(await read(["data\n\n"])).toEqual([
    { id: null, event: null, data: "" },
  ]);
});

test("a comment carries no block: the heartbeat is invisible", async () => {
  expect(
    await read([": keep-alive\n\n", "data: x\n\n", ": keep-alive\n\n"]),
  ).toEqual([{ id: null, event: null, data: "x" }]);
});

test("a field nobody knows is ignored, as the format asks", async () => {
  expect(await read(["retry: 500\nfuture: x\ndata: x\n\n"])).toEqual([
    { id: null, event: null, data: "x" },
  ]);
});

test("only some events carry an id, and each block gets its own", async () => {
  const seen = await read([
    "event: A\ndata: 1\n\n",
    "id: 4\nevent: B\ndata: 2\n\n",
  ]);
  expect(seen.map((block) => block.id)).toEqual([null, "4"]);
});

test("a body that ended without its last blank line still says it", async () => {
  expect(await read(["data: x\n\n", "data: y\n"])).toEqual([
    { id: null, event: null, data: "x" },
    { id: null, event: null, data: "y" },
  ]);
});

test("a line longer than anything we send stops the stream", async () => {
  // The stream is ours: a line past the bound is a bug of ours or something
  // in front of the deployment writing something else, and holding it in a
  // string that grows without bound is how a tab runs out of memory.
  const huge = "x".repeat(MAX_BLOCK + 1);
  const failed = await read([`data: ${huge}\n\n`]).catch(
    (failure: unknown) => failure,
  );
  expect(failed).toBeInstanceOf(ApiError);
  expect((failed as ApiError).error).toBe("stream_overflow");
});

test("a block of many lines is bounded too, and a big one that ends is not", async () => {
  const line = "data: " + "x".repeat(1024 * 1024) + "\n";
  const many = await read([line, line, line, line, line]).catch(
    (failure: unknown) => failure,
  );
  expect((many as ApiError).error).toBe("stream_overflow");
  // Three of those is under the bound and is read like anything else.
  const read3 = await read([line, line, line, "\n"]);
  expect(read3).toHaveLength(1);
  expect(read3[0]?.data.length).toBe(3 * 1024 * 1024 + 2);
});

test("a line arriving in many chunks is scanned once, not once per chunk", async () => {
  // Without a starting point for each search, a line of n characters
  // arriving in n chunks costs n^2. A hundred thousand one-character chunks
  // is unremarkable at O(n) and minutes at O(n^2).
  const chunks = ["data: ", ..."y".repeat(60_000).split(""), "\n\n"];
  const began = Date.now();
  const seen = await read(chunks);
  expect(seen).toHaveLength(1);
  expect(seen[0]?.data).toHaveLength(60_000);
  expect(Date.now() - began).toBeLessThan(5_000);
});

test("aborting stops the reading and throws the signal's own reason", async () => {
  const { response, write } = writable();
  const body = response.body;
  if (body === null) throw new Error("no body");
  const stopping = new AbortController();
  const seen: SseBlock[] = [];
  const reading = (async () => {
    for await (const block of blocks(body, stopping.signal)) seen.push(block);
  })();
  write("data: first\n\n");
  await new Promise((done) => setTimeout(done, 0));
  stopping.abort(new Error("gone"));
  await expect(reading).rejects.toThrow("gone");
  expect(seen).toEqual([{ id: null, event: null, data: "first" }]);
});

test("a signal already aborted reads nothing", async () => {
  const stopping = new AbortController();
  stopping.abort(new Error("gone"));
  await expect(read(["data: x\n\n"], stopping.signal)).rejects.toThrow("gone");
});

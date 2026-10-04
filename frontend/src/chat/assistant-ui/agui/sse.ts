// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * Server-sent events, read off a `fetch` body as they arrive.
 *
 * Not `EventSource`: that opens a `GET` of its own, so it cannot carry the
 * body a turn is started with, and it reconnects on its own terms rather than
 * on ours (`docs/specs/wire.md`). What is left of it is this -- the format,
 * which is small -- and the reconnecting, which is `./client.ts`'s, where the
 * position to carry on from is known.
 *
 * The format, as the WHATWG specification has it and as
 * `backend/tests/sse.py` reads it on the other side:
 *
 * - a block is a run of lines ended by a blank one;
 * - a line is `field: value`, with **one** optional space after the colon;
 * - a line beginning with `:` is a comment, which is what the backend's
 *   `: keep-alive` is, and it carries no block of its own;
 * - `data:` may appear more than once in a block and the values are joined
 *   with newlines;
 * - lines end with `\n`, `\r\n` or `\r`, and a chunk may end between the two
 *   halves of a `\r\n`;
 * - a field this reader does not know is ignored, and so is a block that
 *   carried no `data:` at all.
 *
 * `id:` is not the SSE "last event id" state machine of a browser here: this
 * hands each block the id it carried, and the client above decides what the
 * last **real** position was -- the backend numbers only the last wire event
 * derived from each of the run's own events, so a block without an id is
 * never something to re-attach after (`docs/specs/wire.md`).
 */

import { ApiError } from "../../../api/client";

/**
 * How much of one line, and of one block, is read before giving up.
 *
 * **The stream is ours**, and the longest thing the backend ever writes in
 * one block is a message's deltas, which are small. A line or a block past
 * this is a bug of ours or something in front of the deployment writing
 * something else entirely, and either way holding it in a string that grows
 * without bound is how a tab runs out of memory over a connection nobody is
 * watching. Four mebibytes is four times the bound the wire puts on a
 * *request* body (`docs/specs/wire.md`), so nothing legitimate is near it.
 *
 * In UTF-16 units rather than bytes, because that is what a `string`
 * measures; for anything but astral characters the two are the same, and the
 * point of the bound is the order of magnitude.
 */
export const MAX_BLOCK = 4 * 1024 * 1024;

/** What a line or a block past that bound is reported as. */
export const OVERFLOW =
  "this answer's stream sent a block longer than anything we send";

/** One block of a stream that carried data. */
export interface SseBlock {
  /** The `id:` field: a position in the run, or nothing. */
  id: string | null;
  /** The `event:` field, which is the AG-UI type here. */
  event: string | null;
  /** Every `data:` line of the block, joined with newlines. */
  data: string;
}

/**
 * Every data block of that body, in order, until it ends.
 *
 * `signal` is how a caller stops reading: the reader is cancelled, this
 * generator stops, and whatever the signal gave as its reason is thrown --
 * an abort is the caller's own doing and must be told apart from a stream
 * that failed. `heard` is called whenever bytes arrive, a keep-alive comment
 * included: a connection that says anything is one that works.
 *
 * Whoever stops iterating early -- a `break`, or a `return` -- has the
 * generator's own cleanup cancel the reader, so a stream is never left open
 * behind a caller that has finished with it.
 */
export async function* blocks(
  body: ReadableStream<Uint8Array>,
  signal?: AbortSignal,
  heard?: () => void,
): AsyncGenerator<SseBlock> {
  // A caller that has already given up is not given a first block either.
  if (givenUp(signal)) throw signal?.reason;
  const reader = body.getReader();
  // Cancelling makes the pending `read()` resolve as done, which is how a
  // wait for a stream that has gone quiet is ended without a race.
  const stop = () => void reader.cancel().catch(() => undefined);
  signal?.addEventListener("abort", stop, { once: true });
  const decoder = new TextDecoder();
  let buffer = "";
  let pending: string[] = [];
  /** How long the block held so far is, for the bound above. */
  let held = 0;
  // **Where the last search stopped**, carried across chunks, so that a line
  // arriving in a hundred of them is scanned once rather than a hundred
  // times: nothing before this holds a line ending, and the only character
  // that can gain one is the `\r` held back at the very end.
  let from = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      heard?.();
      // `stream: true`, because a chunk may end in the middle of the bytes
      // of one character and the rest of them is in the next one.
      buffer += decoder.decode(value, { stream: true });
      for (;;) {
        const cut = split(buffer, from);
        if (cut === null) {
          from = buffer.length === 0 ? 0 : buffer.length - 1;
          break;
        }
        buffer = cut.rest;
        from = 0;
        if (cut.line !== "") {
          held += cut.line.length;
          if (held > MAX_BLOCK) throw overflowed();
          pending.push(cut.line);
          continue;
        }
        const block = dispatch(pending);
        pending = [];
        held = 0;
        if (block !== null) yield block;
      }
      if (held + buffer.length > MAX_BLOCK) throw overflowed();
      if (givenUp(signal)) break;
    }
  } finally {
    signal?.removeEventListener("abort", stop);
    // A caller that stopped early, and a body that ended: both leave the
    // reader to be let go of. `cancel` on a finished stream does nothing.
    stop();
  }
  if (givenUp(signal)) throw signal?.reason;
  // A stream that ended without its last blank line still said something.
  // The backend always writes one, and a proxy that cut the connection
  // mid-block is exactly the case a half-written block must not be read as
  // whole -- so what is left is used only when it is a complete block, which
  // here means it parses and carries data.
  const rest = dispatch(pending);
  if (rest !== null) yield rest;
}

/** A line or a block past `MAX_BLOCK`; an `ApiError` like any other failure. */
function overflowed(): ApiError {
  return new ApiError(0, "stream_overflow", OVERFLOW);
}

/**
 * Whether the caller has given up.
 *
 * A function, and not `signal?.aborted` written out three times: a signal is
 * aborted by something outside this loop, and a type checker that has read
 * one test of the flag takes it for settled for the rest of the block.
 */
function givenUp(signal: AbortSignal | undefined): boolean {
  return signal?.aborted === true;
}

/**
 * The first whole line of `buffer` at or after `from`, and what follows it.
 *
 * A `\r` at the very end is held back rather than read as a line ending: the
 * `\n` that would make it one ending rather than two may be the first byte of
 * the next chunk. `from` is where a previous search gave up, which is never
 * past a line ending for exactly that reason.
 */
function split(
  buffer: string,
  from: number,
): { line: string; rest: string } | null {
  for (let at = from; at < buffer.length; at += 1) {
    const character = buffer[at];
    if (character === "\n") {
      return { line: buffer.slice(0, at), rest: buffer.slice(at + 1) };
    }
    if (character === "\r") {
      if (at === buffer.length - 1) break;
      const after = buffer[at + 1] === "\n" ? at + 2 : at + 1;
      return { line: buffer.slice(0, at), rest: buffer.slice(after) };
    }
  }
  return null;
}

/** One block's lines as a block, or nothing where it carried no data. */
function dispatch(lines: readonly string[]): SseBlock | null {
  const data: string[] = [];
  let id: string | null = null;
  let event: string | null = null;
  for (const line of lines) {
    // A comment. The backend's heartbeat is one, and so is anything else a
    // deployment writes to keep a connection open.
    if (line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "data") data.push(value);
    else if (field === "id") id = value;
    else if (field === "event") event = value;
    // Anything else -- `retry:`, a field of a future version -- is ignored,
    // which is what the format asks of a client.
  }
  if (data.length === 0) return null;
  return { id, event, data: data.join("\n") };
}

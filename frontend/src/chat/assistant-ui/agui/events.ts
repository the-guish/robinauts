// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The AG-UI events this backend emits, as types and one decoder.
 *
 * The vocabulary is `docs/specs/wire.md` and what `api/agui.py` really
 * writes: a run started, a message opened, appended to and ended, the same
 * three for a stretch of thinking, a tool call started, its arguments,
 * its end and its result, and one event saying the run is over.
 *
 * **What it does not know, it ignores.** AG-UI is a protocol with more in it
 * than this build uses, and a deployment newer than the page in front of it
 * will send events this does not have a case for. Ignoring them is what lets
 * an answer carry on arriving rather than stopping at the first unfamiliar
 * word; a stream always ends with an event that says the run is over, and
 * those are the three cases below that can never be dropped.
 *
 * The wire is camel case -- it is the AG-UI package's own JSON on the other
 * side -- and this is the one file that knows it. The one thing it reads out
 * of `metadata` is the error flag on a result, which is the one thing the
 * backend puts there (`docs/specs/wire.md`).
 */

/** One event of a run, as this build understands one. */
export type AguiEvent =
  | { type: "RUN_STARTED"; threadId: string; runId: string }
  | { type: "TEXT_MESSAGE_START"; messageId: string; role: string }
  | { type: "TEXT_MESSAGE_CONTENT"; messageId: string; delta: string }
  | { type: "TEXT_MESSAGE_END"; messageId: string }
  | { type: "REASONING_MESSAGE_START"; messageId: string }
  | { type: "REASONING_MESSAGE_CONTENT"; messageId: string; delta: string }
  | { type: "REASONING_MESSAGE_END"; messageId: string }
  | {
      type: "TOOL_CALL_START";
      /** The call's id: the vendor's, carried as data, and what a result names. */
      toolCallId: string;
      /** The tool's full name, `<server>__<tool>`. */
      toolCallName: string;
      /** The answer the call is part of; AG-UI leaves it optional. */
      parentMessageId: string | null;
    }
  | { type: "TOOL_CALL_ARGS"; toolCallId: string; delta: string }
  | { type: "TOOL_CALL_END"; toolCallId: string }
  | {
      type: "TOOL_CALL_RESULT";
      /** The tool message the result is part of, which the next message hangs under. */
      messageId: string;
      toolCallId: string;
      /** The tool's text, to be rendered as data and never as markup. */
      content: string;
      /** Whether the tool said it failed (`metadata.isError` on the wire). */
      isError: boolean;
    }
  | { type: "RUN_FINISHED"; runId: string; cancelled: boolean }
  | { type: "RUN_ERROR"; code: string; message: string };

/**
 * The events that say a run is over, and after which nothing else arrives.
 *
 * Every stream ends with one (`docs/specs/wire.md`), which is what tells a
 * stream that finished from a connection that dropped -- the first is the end
 * of the run and the second is something to re-attach to.
 */
export function isTerminal(event: AguiEvent): boolean {
  return event.type === "RUN_FINISHED" || event.type === "RUN_ERROR";
}

/**
 * That `data:` line as an event, or `null` for one to ignore.
 *
 * `null` covers three things a client has to survive: a body that is not
 * JSON, an event type nobody here has a case for, and an event of a known
 * type whose fields are not what they should be. None of them is worth
 * stopping a stream for, and none of them is worth guessing at either.
 */
export function decode(data: string): AguiEvent | null {
  let body: unknown;
  try {
    body = JSON.parse(data);
  } catch {
    return null;
  }
  if (typeof body !== "object" || body === null) return null;
  const read = body as Record<string, unknown>;
  const type = text(read.type);
  if (type === null) return null;
  switch (type) {
    case "RUN_STARTED": {
      const threadId = text(read.threadId);
      const runId = text(read.runId);
      if (threadId === null || runId === null) return null;
      return { type, threadId, runId };
    }
    case "TEXT_MESSAGE_START": {
      const messageId = text(read.messageId);
      if (messageId === null) return null;
      return { type, messageId, role: text(read.role) ?? "assistant" };
    }
    case "TEXT_MESSAGE_CONTENT":
    case "REASONING_MESSAGE_CONTENT": {
      const messageId = text(read.messageId);
      const delta = text(read.delta);
      if (messageId === null || delta === null) return null;
      return { type, messageId, delta };
    }
    case "TEXT_MESSAGE_END":
    case "REASONING_MESSAGE_START":
    case "REASONING_MESSAGE_END": {
      const messageId = text(read.messageId);
      if (messageId === null) return null;
      return { type, messageId };
    }
    case "TOOL_CALL_START": {
      const toolCallId = text(read.toolCallId);
      const toolCallName = text(read.toolCallName);
      if (toolCallId === null || toolCallName === null) return null;
      return {
        type,
        toolCallId,
        toolCallName,
        parentMessageId: text(read.parentMessageId),
      };
    }
    case "TOOL_CALL_ARGS": {
      const toolCallId = text(read.toolCallId);
      const delta = text(read.delta);
      if (toolCallId === null || delta === null) return null;
      return { type, toolCallId, delta };
    }
    case "TOOL_CALL_END": {
      const toolCallId = text(read.toolCallId);
      if (toolCallId === null) return null;
      return { type, toolCallId };
    }
    case "TOOL_CALL_RESULT": {
      const messageId = text(read.messageId);
      const toolCallId = text(read.toolCallId);
      // AG-UI allows a list of parts here; this backend sends the text as
      // one string (`api/agui.py`), and anything else is a result this
      // build does not read. The conversation, read again when the run
      // ends, still shows it.
      const content = text(read.content);
      if (messageId === null || toolCallId === null || content === null) {
        return null;
      }
      const metadata = read.metadata;
      const isError =
        typeof metadata === "object" &&
        metadata !== null &&
        (metadata as Record<string, unknown>).isError === true;
      return { type, messageId, toolCallId, content, isError };
    }
    case "RUN_FINISHED": {
      const runId = text(read.runId);
      if (runId === null) return null;
      // **A cancellation is not a failure** (`docs/specs/wire.md`): a run
      // somebody stopped is this event with AG-UI's `cancelled` outcome, and
      // no outcome at all is a run that finished.
      const outcome = read.outcome;
      const named =
        typeof outcome === "object" && outcome !== null
          ? text((outcome as Record<string, unknown>).type)
          : null;
      return { type, runId, cancelled: named === "cancelled" };
    }
    case "RUN_ERROR": {
      // The code is what a client branches on and the message is a fixed
      // sentence of the backend's; neither is ever the run's stored error.
      return {
        type,
        code: text(read.code) ?? "",
        message: text(read.message) ?? "",
      };
    }
    default:
      return null;
  }
}

/** That value if it is a string, and `null` if it is anything else. */
function text(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

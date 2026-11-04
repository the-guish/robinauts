// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * How tool calls are drawn: a group of them behind one disclosure, and each
 * call behind its own.
 *
 * **Everything in a call is text the model or a tool wrote** -- the name the
 * model chose, the arguments it streamed, the tool's answer -- and every one
 * of them is put in a text node, never parsed as markup (`docs/specs/wire.md`:
 * rendered as data). The tool's name is the one thing in bold, in an element
 * of ours.
 *
 * A call stands one of four ways, read off the call and the answer it is part
 * of: **running** while it has no result and the answer is still being
 * written; **complete** once it has a result, or when the answer finished
 * without one; **cancelled** when the answer was stopped before its result;
 * **failed** when the answer failed before its result, or when the tool
 * answered that the call failed (`isError`, `docs/specs/wire.md`) -- which is
 * said in a line above the result, so that a result reading "no such
 * repository" is not drawn under a check mark like any other.
 */
import { useState } from "react";

import {
  CheckIcon,
  ChevronDownIcon,
  CircleXIcon,
  LoaderIcon,
} from "../../icons";
import type { ChatMessage, ChatToolCall } from "../state";
import { DisclosurePanel, useDisclosure } from "./Disclosure";

/** What is said above the result of a call the tool answered as failed. */
export const TOOL_FAILED = "The tool answered that this call failed.";

/** How a call stands, and why it did not finish when it did not. */
interface CallStatus {
  how: "running" | "complete" | "cancelled" | "failed";
  /** The answer's sentence, when the answer failed before the call's result. */
  reason?: string;
}

/**
 * How that call of that answer stands, before the tool's own say.
 *
 * A call with a result is complete, whatever happened to the answer after it;
 * one without takes the answer's state, because it ended with the answer.
 */
function callStatus(call: ChatToolCall, message: ChatMessage): CallStatus {
  if (call.result !== undefined) return { how: "complete" };
  switch (message.state) {
    case "running":
      return { how: "running" };
    case "stored":
      return { how: "complete" };
    case "cancelled":
      return { how: "cancelled" };
    case "failed":
      return message.detail === undefined || message.detail === ""
        ? { how: "failed" }
        : { how: "failed", reason: message.detail };
  }
}

/** A result as it is shown: a string as it came, anything else as JSON. */
function shown(result: unknown): string {
  if (typeof result === "string") return result;
  try {
    return JSON.stringify(result, null, 2) ?? String(result);
  } catch {
    return String(result);
  }
}

export function ToolCall({
  call,
  status,
}: Readonly<{ call: ChatToolCall; status: CallStatus }>) {
  const [open, setOpen] = useState(false);
  const disclosure = useDisclosure(open);
  const failed = call.isError === true;
  // The trigger says how the call went; the tool's own "it failed" outranks
  // the answer's, which is about the answer.
  const how = failed ? "failed" : status.how;
  const cancelled = how === "cancelled";
  const Icon =
    how === "running"
      ? LoaderIcon
      : how === "complete"
        ? CheckIcon
        : CircleXIcon;

  return (
    <div className="chat-tool" data-slot="tool-fallback-root" data-status={how}>
      <button
        type="button"
        className="chat-trigger chat-tool-trigger"
        data-slot="tool-fallback-trigger"
        {...disclosure.trigger}
        onClick={() => {
          setOpen(!open);
        }}
      >
        <Icon
          className={
            how === "running"
              ? "chat-trigger-icon chat-spin"
              : "chat-trigger-icon"
          }
        />
        <span className="chat-trigger-label chat-tool-label">
          {cancelled ? "Cancelled tool" : "Used tool"}: <b>{call.name}</b>
        </span>
        <ChevronDownIcon className="chat-chevron" />
      </button>
      <DisclosurePanel state={disclosure}>
        <div className="chat-tool-content">
          {status.reason !== undefined && (
            <div data-slot="tool-fallback-error">
              <p className="chat-tool-error-header">Error:</p>
              <p className="chat-tool-error-reason">{status.reason}</p>
            </div>
          )}
          {call.argsText !== "" && (
            <div data-slot="tool-fallback-args">
              <pre className="chat-tool-data">{call.argsText}</pre>
            </div>
          )}
          {failed && (
            <p data-slot="tool-call-failed" className="chat-tool-failed">
              {TOOL_FAILED}
            </p>
          )}
          {call.result !== undefined && (
            <div data-slot="tool-fallback-result">
              <p className="chat-tool-result-header">Result:</p>
              <pre className="chat-tool-data">{shown(call.result)}</pre>
            </div>
          )}
        </div>
      </DisclosurePanel>
    </div>
  );
}

/**
 * Consecutive calls of an answer behind one disclosure, named by how many
 * there are: "1 tool call", "2 tool calls". Closed until it is opened, with a
 * spinner while any of them is running.
 */
export function ToolGroup({
  calls,
  message,
}: Readonly<{ calls: readonly ChatToolCall[]; message: ChatMessage }>) {
  const [open, setOpen] = useState(false);
  const disclosure = useDisclosure(open);
  const drawn = calls.map((call) => ({
    call,
    status: callStatus(call, message),
  }));
  const active = drawn.some(({ status }) => status.how === "running");
  const count = calls.length;

  return (
    <div className="chat-tool-group" data-slot="tool-group-root">
      <button
        type="button"
        className="chat-trigger chat-tool-group-trigger"
        data-slot="tool-group-trigger"
        {...disclosure.trigger}
        onClick={() => {
          setOpen(!open);
        }}
      >
        {active && <LoaderIcon className="chat-group-icon chat-spin" />}
        <span className="chat-trigger-label chat-tool-group-label">
          {`${count} tool ${count === 1 ? "call" : "calls"}`}
        </span>
        <ChevronDownIcon className="chat-chevron chat-group-chevron" />
      </button>
      <DisclosurePanel state={disclosure}>
        <div className="chat-tool-group-content">
          {drawn.map(({ call, status }) => (
            <ToolCall key={call.id} call={call} status={status} />
          ))}
        </div>
      </DisclosurePanel>
    </div>
  );
}

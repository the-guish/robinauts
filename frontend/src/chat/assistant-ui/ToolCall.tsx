// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * How a tool call is drawn: the vendored fallback, with a failed result said.
 *
 * assistant-ui's `ToolFallback` (`./vendor/.../tool-fallback.aui.tsx`) draws
 * a call as a toggle -- the tool's name, then its arguments and its result
 * behind it, every one of them in a text node -- and takes its status from
 * the message's: running, complete, cancelled. What it does not read is
 * `isError`, the flag a tool answers with when the call failed
 * (`docs/specs/wire.md`), so a result that says "no such repository" would
 * be drawn under a check mark like any other. This composes the same parts
 * and says it: the trigger's icon is the one for a call that did not
 * succeed, and a line above the result says what the flag means. Composed
 * here rather than edited there, so the copy stays a copy (`vendor/README.md`).
 */
import type { ToolCallMessagePartComponent } from "@assistant-ui/react";

import { ToolFallback } from "./vendor/components/assistant-ui/elements/tool-fallback.aui";

/** What is said above the result of a call the tool answered as failed. */
export const TOOL_FAILED = "The tool answered that this call failed.";

export const ToolCall: ToolCallMessagePartComponent = ({
  toolName,
  argsText,
  result,
  isError,
  status,
}) => {
  const failed = isError === true;
  return (
    <ToolFallback.Root>
      <ToolFallback.Trigger
        toolName={toolName}
        status={failed ? { type: "incomplete", reason: "error" } : status}
      />
      <ToolFallback.Content>
        <ToolFallback.Error status={status} />
        <ToolFallback.Args argsText={argsText} />
        {failed && (
          <p
            data-slot="tool-call-failed"
            className="text-bad text-xs font-medium"
          >
            {TOOL_FAILED}
          </p>
        )}
        <ToolFallback.Result result={result} />
      </ToolFallback.Content>
    </ToolFallback.Root>
  );
};

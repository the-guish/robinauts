// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The box: where a message is written, and the one button beside it.
 *
 * The button is Send, and Stop while a run is going -- a run this chat knows
 * the id of, and not a turn that has merely been asked for, which there is
 * nothing yet to stop (`../runtime.tsx`). Enter sends and Shift+Enter breaks
 * the line; Escape stops a run that is going. Send is not offered for an
 * empty box, or while there is nobody to send a first message to; the box
 * still takes what is typed.
 */
import type { FormEvent } from "react";

import { ArrowUpIcon, SquareIcon } from "../../icons";
import { useChatContext } from "../runtime";
import { isSend, useAutosize } from "./textarea";

export function Composer() {
  const { composer, submit, running, sendDisabled, cancel } = useChatContext();
  const box = useAutosize(composer.text);
  const sendable = composer.text.trim() !== "" && !sendDisabled;

  function submitted(event: FormEvent) {
    event.preventDefault();
    submit();
  }

  return (
    <form className="chat-composer" onSubmit={submitted}>
      {/* A click anywhere on the box is a click into it, as it looks. */}
      <div
        className="chat-composer-shell"
        onClick={(event) => {
          if (event.target === event.currentTarget) box.current?.focus();
        }}
      >
        <textarea
          ref={box}
          className="chat-composer-input"
          aria-label="Message input"
          placeholder="Send a message..."
          rows={1}
          enterKeyHint="send"
          // The box is what the chat is for, so it has the keyboard when the
          // chat opens, as it always had.
          autoFocus
          value={composer.text}
          onChange={(event) => {
            composer.setText(event.target.value);
          }}
          onKeyDown={(event) => {
            if (event.key === "Escape" && running) {
              event.preventDefault();
              void cancel();
              return;
            }
            if (!isSend(event)) return;
            event.preventDefault();
            // Nothing is sent while a run is going: Enter is not a stop.
            if (!running) submit();
          }}
        />
        <div className="chat-composer-actions">
          {running ? (
            <button
              type="button"
              className="chat-send"
              aria-label="Stop generating"
              title="Stop generating"
              onClick={() => {
                void cancel();
              }}
            >
              <SquareIcon className="chat-stop-icon" />
            </button>
          ) : (
            <button
              type="submit"
              className="chat-send"
              aria-label="Send message"
              title="Send message"
              disabled={!sendable}
            >
              <ArrowUpIcon className="chat-send-icon" />
            </button>
          )}
        </div>
      </div>
    </form>
  );
}

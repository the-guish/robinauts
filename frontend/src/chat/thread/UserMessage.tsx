// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * A question: a bubble of text, and an Edit beside it.
 *
 * Edit turns the question into a box with its text in it. Sending that box is
 * an edit (`docs/specs/conversations.md`): a new question under the parent of
 * this one, and the thread on the screen cut there and going on from it. The
 * old question stays in the store, and is not a branch to come back to
 * (ADR 0003).
 */
import { memo, useEffect, useRef, type FormEvent } from "react";

import { PencilIcon } from "../../icons";
import type { ChatMessage } from "../state";
import { IconButton } from "../IconButton";
import { isSend, useAutosize } from "./textarea";

/** What a question can be asked to do, made once for the whole thread. */
export interface QuestionActions {
  beginEdit: (messageId: string) => void;
  setEditText: (text: string) => void;
  cancelEdit: () => void;
  submitEdit: () => void;
}

export const UserMessage = memo(function UserMessage({
  message,
  last,
  running,
  editing,
  actions,
}: Readonly<{
  message: ChatMessage;
  /** The last message of the thread, whose Edit is always shown. */
  last: boolean;
  /** A run is in flight: no edit is offered, and none is sent. */
  running: boolean;
  /** What this question's edit box says, while it is open. */
  editing: string | null;
  actions: QuestionActions;
}>) {
  // When the edit box closes, the keyboard goes back to the Edit that opened
  // it rather than to the top of the page.
  const editButton = useRef<HTMLDivElement>(null);
  const wasEditing = useRef(false);
  useEffect(() => {
    const closed = wasEditing.current && editing === null;
    wasEditing.current = editing !== null;
    if (closed && document.activeElement === document.body) {
      editButton.current?.querySelector("button")?.focus();
    }
  }, [editing]);

  if (editing !== null) {
    return (
      <EditComposer
        messageId={message.id}
        text={editing}
        running={running}
        actions={actions}
      />
    );
  }

  return (
    <div
      className="chat-message chat-question"
      data-role="user"
      data-message-id={message.id}
      data-last={last}
    >
      <div className="chat-question-content">
        <div className="chat-bubble">
          {message.parts.map((part, at) =>
            part.kind === "text" ? (
              <p key={at} className="chat-question-text">
                {part.text}
              </p>
            ) : null,
          )}
        </div>
        {!running && (
          <div ref={editButton} className="chat-actions chat-question-actions">
            <IconButton
              label="Edit"
              onClick={() => {
                actions.beginEdit(message.id);
              }}
            >
              <PencilIcon />
            </IconButton>
          </div>
        )}
      </div>
    </div>
  );
});

/**
 * The question's own box, in its place in the thread.
 *
 * Enter sends and Shift+Enter breaks the line, as in the main box, and
 * Escape closes it. Update is not offered for an empty box, nor while a run
 * is going: one turn at a time (`docs/specs/runs.md`).
 */
function EditComposer({
  messageId,
  text,
  running,
  actions,
}: Readonly<{
  messageId: string;
  text: string;
  running: boolean;
  actions: QuestionActions;
}>) {
  const box = useAutosize(text);
  // Opened with the caret after the text, which is where an edit begins.
  useEffect(() => {
    const element = box.current;
    if (element === null) return;
    element.focus();
    element.setSelectionRange(element.value.length, element.value.length);
  }, [box]);
  const sendable = text.trim() !== "" && !running;

  function submitted(event: FormEvent) {
    event.preventDefault();
    if (sendable) actions.submitEdit();
  }

  return (
    <div
      className="chat-message chat-edit"
      data-slot="edit-composer"
      data-message-id={messageId}
    >
      <form className="chat-edit-box" onSubmit={submitted}>
        <textarea
          ref={box}
          className="chat-edit-input"
          aria-label="Edit message"
          value={text}
          rows={1}
          onChange={(event) => {
            actions.setEditText(event.target.value);
          }}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              actions.cancelEdit();
              return;
            }
            if (!isSend(event)) return;
            event.preventDefault();
            if (sendable) actions.submitEdit();
          }}
        />
        <div className="chat-edit-footer">
          <button
            type="button"
            className="chat-button chat-button-quiet"
            onClick={actions.cancelEdit}
          >
            Cancel
          </button>
          <button
            type="submit"
            className="chat-button chat-button-primary"
            disabled={!sendable}
          >
            Update
          </button>
        </div>
      </form>
    </div>
  );
}

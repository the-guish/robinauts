// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The thread: a scrolling viewport of messages, and the box at its foot.
 *
 * Four states, read off the chat's own:
 *
 * - **the empty chat**: nothing to show and nothing being read. The welcome
 *   -- the shell's pickers -- sits directly above the box, the two together
 *   in the middle of the page;
 * - **being read**: a conversation opened and not yet answered for, drawn as
 *   the outline of a thread so that nothing jumps when it arrives;
 * - **a thread**: the messages, with the box docked to the bottom of the
 *   viewport and a button back down whenever it is not at the bottom;
 * - **answering**: a run in flight that has said nothing yet, which is a dot
 *   where its answer will be.
 *
 * Every message is drawn by a component that is given what it draws and
 * nothing else that changes, so that a keystroke in the box or a delta to the
 * last answer draws the one thing that changed.
 */
import { useEffect, useMemo, type ReactNode } from "react";

import { ArrowDownIcon } from "../../icons";
import { useChatContext } from "../runtime";
import type { ChatMessage } from "../state";
import { AssistantMessage, type AnswerActions } from "./AssistantMessage";
import { Composer } from "./Composer";
import { IconButton } from "../IconButton";
import { useStickToBottom } from "./scroll";
import { UserMessage, type QuestionActions } from "./UserMessage";

/** What stands where an answer will be, before it has said anything. */
const ANSWERING: ChatMessage = {
  id: "",
  role: "assistant",
  parts: [],
  state: "running",
};

export function Thread({ welcome }: Readonly<{ welcome: ReactNode }>) {
  const chat = useChatContext();
  const { state, running, editing } = chat;
  const messages = state.messages;
  const empty = messages.length === 0 && !state.loading;
  const reading = messages.length === 0 && state.loading;
  // A run that has not opened its answer yet: the one thing on the screen is
  // the question, and the dot stands where the answer will arrive.
  const answering = running && messages.at(-1)?.role !== "assistant";

  const { beginEdit, setEditText, cancelEdit, submitEdit, regenerate, resume } =
    chat;
  const questionActions: QuestionActions = useMemo(
    () => ({ beginEdit, setEditText, cancelEdit, submitEdit }),
    [beginEdit, setEditText, cancelEdit, submitEdit],
  );
  const answerActions: AnswerActions = useMemo(
    () => ({ regenerate, resume }),
    [regenerate, resume],
  );

  // Followed down while an answer arrives, from the send to its end.
  const { viewport, content, atBottom, toBottom } = useStickToBottom(
    running || state.sending,
  );
  // A conversation that has just been opened, and a message somebody has
  // just sent, are both read from the bottom. Opened, and not read again: the
  // read that ends a turn leaves the viewport where the person put it.
  useEffect(() => {
    if (!reading) toBottom();
  }, [state.conversationId, reading, toBottom]);
  useEffect(() => {
    if (state.sending) toBottom();
  }, [state.sending, toBottom]);

  return (
    <div className="chat-thread">
      <div
        ref={viewport}
        className="chat-viewport"
        role="region"
        aria-label="Chat"
        // A region that scrolls is one a keyboard has to be able to scroll.
        tabIndex={0}
      >
        <div
          ref={content}
          className="chat-column"
          data-empty={empty || undefined}
        >
          {empty && welcome}
          {reading && <Outline />}
          <div className="chat-messages">
            {messages.map((message, at) => {
              const last = at === messages.length - 1 && !answering;
              return message.role === "user" ? (
                <UserMessage
                  key={message.id}
                  message={message}
                  last={last}
                  running={running}
                  editing={editing?.id === message.id ? editing.text : null}
                  actions={questionActions}
                />
              ) : (
                <AssistantMessage
                  key={message.id}
                  message={message}
                  last={last}
                  running={running}
                  actions={answerActions}
                />
              );
            })}
            {answering && (
              <AssistantMessage
                key="answering"
                message={ANSWERING}
                last
                running={running}
                placeholder
                actions={answerActions}
              />
            )}
          </div>
          <div className="chat-footer" data-docked={!empty || undefined}>
            <IconButton
              label="Scroll to bottom"
              className="chat-to-bottom"
              disabled={atBottom}
              onClick={() => {
                toBottom(true);
              }}
            >
              <ArrowDownIcon />
            </IconButton>
            <Composer />
          </div>
        </div>
      </div>
    </div>
  );
}

/** The outline of a thread, while a conversation is being read. */
function Outline() {
  return (
    <div className="chat-outline" role="status">
      <span className="chat-visually-hidden">Loading conversation</span>
      <div className="chat-outline-block chat-outline-question" />
      <div className="chat-outline-lines">
        <div className="chat-outline-block chat-outline-line" />
        <div className="chat-outline-block chat-outline-line" />
        <div className="chat-outline-block chat-outline-line" />
      </div>
      <div className="chat-outline-block chat-outline-question" />
      <div className="chat-outline-lines">
        <div className="chat-outline-block chat-outline-line" />
        <div className="chat-outline-block chat-outline-line" />
      </div>
    </div>
  );
}

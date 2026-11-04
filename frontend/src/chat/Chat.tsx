// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The chat window: the thread, over our runtime.
 *
 * Everything drawn in the thread is `./thread/` -- the message list, the box,
 * the action bars, the collapsed blocks of thinking and of tool calls -- and
 * everything it is given is ours (`./runtime.tsx`). What is written in this
 * file is only what is between the two: the things about a conversation the
 * thread has no place for.
 *
 * - **A conversation that is not here**, which is a 404 and is what somebody
 *   else's conversation answers as well (`docs/specs/privacy.md`). There is
 *   no thread to show, so this stands in place of one.
 * - **A sentence about the last run** where no message carries it: a run that
 *   ended before it said anything, a cancellation, a turn the server refused
 *   (`docs/specs/wire.md`). Where there **is** a message, the thread shows it
 *   under that message itself and this says nothing.
 * - **A notice about what the person just did**, which is a different thing
 *   and outlives the run that follows it: a message that was not sent, a
 *   stop that did not reach the server. Only their next turn clears it.
 *
 * The welcome slot is the shell's agent and model pickers on an empty chat:
 * in the thread's empty state the welcome sits directly above the box, which
 * is where a choice of who you are about to talk to belongs
 * (`docs/specs/agents.md`).
 */
import type { ChatProps } from "./index";
import { ChatContext, useChat } from "./runtime";
import { Thread } from "./thread/Thread";
import "./thread/thread.css";

export function Chat(props: Readonly<ChatProps>) {
  const chat = useChat(props);
  const { state } = chat;
  // Only on the empty chat: a conversation with nothing in it yet is still a
  // conversation, and its agent is no longer a choice (`./index.ts`).
  const welcome = props.conversationId === null ? props.welcome : undefined;

  if (state.failure !== null) {
    return (
      <div className="chat-failure">
        <p role="alert" className="chat-failure-text">
          {state.failure.missing
            ? "This conversation is not here. It may have been deleted, or it was never yours."
            : `This conversation could not be opened: ${state.failure.detail}`}
        </p>
      </div>
    );
  }

  return (
    <ChatContext.Provider value={chat}>
      <div className="chat">
        {/* Its own box, which holds the thread's scrolling viewport: the
            viewport is what scrolls, not the page. */}
        <div className="chat-body">
          <Thread welcome={welcome} />
        </div>
        {state.ended !== null && (
          <p role="status" data-ended="" className="chat-said">
            {state.ended}
          </p>
        )}
        {state.notice !== null && (
          <p role="alert" data-notice="" className="chat-said chat-said-notice">
            {state.notice}
          </p>
        )}
      </div>
    </ChatContext.Provider>
  );
}

// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The chat seam (ADR 0001).
 *
 * This module is the only thing about the chat that the rest of the
 * application may import. The chat itself -- its runtime, its thread, its
 * Markdown -- lives beside this file, written in this repository over React
 * alone, and nothing outside `src/chat/` reaches into it.
 *
 * What goes here is a small interface this project owns -- in essence a
 * `<Chat>` taking a conversation id and callbacks -- whose types mention
 * nothing of how the chat is drawn. Replacing everything behind this file
 * must leave exactly one thing to change: the implementation it names.
 *
 * What is written here is what the application hands a `<Chat>` and what it
 * needs back from one; nothing in it names, or could name, a chat library.
 * The shell, the router and the history already use these names, so the seam
 * is the one vocabulary the two sides share rather than something invented
 * for the implementation.
 */
import type { ReactNode } from "react";

import type { Conversation } from "../conversation/conversation";

/**
 * The chat window: the messages, the box to write in, and the stream.
 *
 * This is the whole of what the application may reach for (ADR 0001).
 */
export { Chat } from "./Chat";

/**
 * A conversation, as the rest of the application refers to one: the id the
 * backend gave it, and nothing a chat library chose.
 */
export type ConversationId = string;

/**
 * An agent, as the configuration names it and the picker offers it
 * (`docs/specs/agents.md`).
 *
 * The id alone: which model an agent runs, what its prompt says and who its
 * vendor is are the operator's, and none of it reaches the browser.
 */
export type AgentId = string;

/**
 * A model, as the configuration names it and `GET /api/models` offers it
 * (`docs/specs/agents.md`). The id alone, like the agent's.
 */
export type ModelId = string;

/**
 * A piece of a message, as far as anything outside the chat may read it:
 * what it said, what it thought, or a call it made and what came back.
 *
 * All of it is text a person, a model or a tool wrote, and is data: it is
 * counted, never drawn as markup.
 */
export type TranscriptPart =
  | { readonly kind: "text" | "reasoning"; readonly text: string }
  | {
      readonly kind: "tool-call";
      /** The tool's full name, `<server>__<tool>`. */
      readonly name: string;
      /** The arguments as the model wrote them, JSON text. */
      readonly argsText: string;
      /** What the tool answered, once it has. */
      readonly result?: string;
    };

/** One message of the thread on the screen. */
export interface TranscriptMessage {
  readonly role: "user" | "assistant";
  readonly parts: readonly TranscriptPart[];
}

/**
 * The thread on the screen, oldest first, as the chat holds it: the
 * conversation as it was last read, and the answer still arriving at its end.
 */
export type Transcript = readonly TranscriptMessage[];

/**
 * What the chat is given, and the one thing it says back.
 *
 * `conversationId` is `null` on the empty chat -- the application opens on
 * one, ready for a first message (`docs/specs/frontend.md`) -- and the id of
 * the conversation being read otherwise. `agentId` is the agent a first
 * message will start the conversation with, which is a choice only while
 * there is no conversation: after that the agent is a fact about it
 * (`docs/specs/conversations.md`). It is `null` when the deployment has told
 * us no agents, or has not told us yet. `modelId` is the model that first
 * message runs on, and `null` leaves it to the agent's default; a
 * conversation's model is changed outside the chat, and no later turn names
 * one, because the server reads it off the conversation.
 *
 * `onConversationStarted` is the one thing the chat cannot decide for the
 * application: a first message creates a conversation
 * (`POST /api/turns`, `docs/specs/wire.md`), and the interface then has a
 * conversation to be on -- a route to go to and a row for the panel. The
 * chat reports the id; what to do about it is the application's.
 */
export interface ChatProps {
  conversationId: ConversationId | null;
  agentId: AgentId | null;
  modelId: ModelId | null;
  onConversationStarted: (id: ConversationId) => void;
  /**
   * What the server says the conversation is, each time it is read.
   *
   * The shell draws the title and the agent above the chat, and the panel's
   * pages do not hold every conversation a link can open. This is the one
   * read that always has them (`GET /api/conversations/{id}`).
   */
  onConversationOpened?: (conversation: Conversation) => void;
  /**
   * That a turn has finished and the conversation has been written to.
   *
   * The panel's list is ordered by when a conversation was last written to
   * and its title is the beginning of its first message
   * (`docs/specs/conversations.md`), so both are stale the moment an answer
   * lands. The chat says when; asking again is the application's
   * (`src/history/history.ts`).
   */
  onTurnEnded?: () => void;
  /**
   * That a first message was refused because the deployment does not offer
   * the model it named -- `modelId` above (`docs/specs/wire.md`).
   *
   * The chat says so and puts the message back; what to do about the model
   * is the shell's, which holds the choice.
   */
  onModelRefused?: (modelId: ModelId) => void;
  /**
   * What to draw above the box on an empty chat: the agent and model
   * pickers.
   *
   * The agent is a choice only while there is no conversation, and the
   * pickers are the shell's -- they are over `GET /api/agents` and
   * `GET /api/models`, they remember what was chosen, and they are what
   * `agentId` and `modelId` above come from. The chat is told
   * where it goes rather than how it is built, so nothing about an agent has
   * to cross this seam twice.
   */
  welcome?: ReactNode;
  /**
   * The thread on the screen, each time it changes: when a conversation is
   * read, as an answer streams in, and empty on an empty chat.
   *
   * The right side panel counts it (`src/side/`): messages, tool calls,
   * characters and an estimate of the tokens. It is called at every token
   * of a stream, so what it is handed to has to be cheap.
   */
  onTranscript?: (transcript: Transcript) => void;
}

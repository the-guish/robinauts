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
 * A model, as the configuration names it and `GET /robinauts/api/models` offers it
 * (`docs/specs/agents.md`). The id alone, like the agent's.
 */
export type ModelId = string;

/**
 * What was written in each conversation's box and not sent, by its id.
 *
 * Made and held by the application, for as long as somebody is signed in,
 * and handed to every chat it draws (`ChatProps.drafts`): "New chat" makes a
 * new chat, and the drafts of the conversations left behind outlive it. Only
 * in memory, never in the browser's storage, where it would outlive the
 * person's session.
 */
export type Drafts = Map<ConversationId, string>;

/**
 * One tool call of the thread on the screen, as the side panel reads one
 * (`docs/architecture/mcp-apps.md`): a call of a tool with a view is a
 * sub-tab there.
 */
export interface ThreadToolCall {
  /** The answer the call is part of, by the id the backend gave it. */
  messageId: string;
  /** The call's id, which its result is asked for by. */
  callId: string;
  /** The tool's name, as its server names it. */
  name: string;
  /** The arguments as data; `null` until the call is whole. */
  arguments: Readonly<Record<string, unknown>> | null;
  /**
   * `"running"` until the tool answers, `"answered"` once it has, and
   * `"unanswered"` for a call whose turn ended before it did.
   */
  state: "running" | "answered" | "unanswered";
  /**
   * This page saw the call arrive in a turn. One read from the store, on
   * opening a conversation, is not live.
   */
  live: boolean;
}

/** The tool calls of the thread on the screen, oldest first. */
export interface ThreadToolCalls {
  /** The conversation they are of; `null` on the empty chat. */
  conversationId: ConversationId | null;
  calls: readonly ThreadToolCall[];
}

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
 * (`POST /robinauts/api/turns`, `docs/specs/wire.md`), and the interface then has a
 * conversation to be on -- a route to go to and a row for the panel. The
 * chat reports the id; what to do about it is the application's.
 */
export interface ChatProps {
  conversationId: ConversationId | null;
  agentId: AgentId | null;
  modelId: ModelId | null;
  /**
   * The project a first message starts the conversation in, like `agentId`
   * a choice only while there is no conversation. Left out or `null`, the
   * backend puts it in the user's "Default" project.
   */
  projectId?: string | null;
  onConversationStarted: (id: ConversationId) => void;
  /**
   * What the server says the conversation is, each time it is read.
   *
   * The shell draws the title and the agent above the chat, and the panel's
   * pages do not hold every conversation a link can open. This is the one
   * read that always has them (`GET /robinauts/api/conversations/{id}`).
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
   * A count that grows each time something of this user changed elsewhere
   * (`src/changes/changes.ts`).
   *
   * Each new value reads the open conversation again. The read redraws
   * nothing while a turn is being sent or watched here, or when the last
   * message and the running turn are as they were. It attaches to a turn it
   * finds running, as opening the conversation does.
   */
  changes?: number;
  /**
   * That a first message was refused because the deployment does not offer
   * the model it named -- `modelId` above (`docs/specs/wire.md`).
   *
   * The chat says so and puts the message back; what to do about the model
   * is the shell's, which holds the choice.
   */
  onModelRefused?: (modelId: ModelId) => void;
  /**
   * The tool calls of the thread on the screen, each time they change: a
   * call starts, its arguments are whole, its tool answers, the thread is
   * read again. The side panel shows the calls of tools with a view
   * (`src/side/AppTab.tsx`).
   */
  onToolCalls?: (thread: ThreadToolCalls) => void;
  /**
   * What to draw above the box on an empty chat: the agent and model
   * pickers.
   *
   * The agent is a choice only while there is no conversation, and the
   * pickers are the shell's -- they are over `GET /robinauts/api/agents` and
   * `GET /robinauts/api/models`, they remember what was chosen, and they are what
   * `agentId` and `modelId` above come from. The chat is told
   * where it goes rather than how it is built, so nothing about an agent has
   * to cross this seam twice.
   */
  welcome?: ReactNode;
  /**
   * Where the box keeps what was written and not sent in each conversation
   * (`Drafts`). Opening another conversation shows that one's draft, and
   * coming back shows this one's again. Left out, the chat keeps its own,
   * which go with it.
   */
  drafts?: Drafts;
}

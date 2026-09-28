// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * Conversations to test with, in the shapes the API answers in.
 *
 * Shared by the tests of the history, of the panel's list and of the shell's
 * routing, so that one change to the wire is one change here.
 */
import type { components } from "../api/schema";
import type { Conversation, Message } from "../conversation/conversation";

/** What opening a conversation answers with. */
type Opened = components["schemas"]["OpenedConversationResponse"];

/** The run a conversation that is answering is answering with. */
export const RUN = "11111111-2222-4333-8444-555555555555";

/** A conversation id of the shape the router insists on. */
export function id(n: number): string {
  return `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
}

export function conversation(
  n: number,
  title = `Conversation ${n}`,
): Conversation {
  return {
    id: id(n),
    title,
    agent: "helper",
    model: "sonnet",
    created_at: "2026-09-01T10:00:00Z",
    updated_at: "2026-09-02T10:00:00Z",
  };
}

/** A page of them, newest first, with or without a cursor to the next. */
export function page(
  items: Conversation[],
  nextCursor: string | null = null,
): { items: Conversation[]; next_cursor: string | null } {
  return { items, next_cursor: nextCursor };
}

/** One message of a conversation's thread. */
export function message(
  messageId: string,
  role: "user" | "assistant",
  text: string,
  provenance: Message["provenance"] = null,
): Message {
  return {
    id: messageId,
    role,
    channel: "web",
    created_at: "2026-09-02T09:30:00Z",
    parts: [{ kind: "text", text }],
    provenance,
  };
}

/** One call an answer makes, as the API shows it. */
export interface Call {
  call_id: string;
  name: string;
  arguments: Record<string, unknown>;
}

/** An answer that made those calls, beside what it said. */
export function calling(
  messageId: string,
  text: string,
  calls: readonly Call[],
  provenance: Message["provenance"] = null,
): Message {
  return {
    id: messageId,
    role: "assistant",
    channel: "web",
    created_at: "2026-09-02T09:30:00Z",
    parts: [
      ...(text === "" ? [] : [{ kind: "text" as const, text }]),
      ...calls.map((call) => ({ kind: "tool_call" as const, ...call })),
    ],
    provenance,
  };
}

/** The tool message under an answer: one result per call it made. */
export function results(
  messageId: string,
  answers: readonly { call_id: string; text: string; is_error?: boolean }[],
): Message {
  return {
    id: messageId,
    role: "tool",
    channel: "web",
    created_at: "2026-09-02T09:30:01Z",
    parts: answers.map((answer) => ({
      kind: "tool_result" as const,
      call_id: answer.call_id,
      text: answer.text,
      is_error: answer.is_error ?? false,
    })),
    provenance: null,
  };
}

/** What an answer records, on an assistant message. */
export function madeBy(
  agent = "helper",
  engine: "langgraph" | "pydantic-ai" = "langgraph",
): Message["provenance"] {
  return { agent, engine, model: "sonnet", run_id: RUN };
}

/** A conversation as `GET /api/conversations/{id}` answers it. */
export function opened(
  summary: Conversation,
  messages: Message[],
  rest: Partial<Opened> = {},
): Opened {
  return {
    conversation: summary,
    messages,
    run_id: null,
    resume: null,
    ended_badly: null,
    ...rest,
  };
}

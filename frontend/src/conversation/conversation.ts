// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * What the rest of the application calls a conversation and a message.
 *
 * `GET /api/conversations/{id}` answers **one moment** of a conversation
 * (`docs/specs/conversations.md`, "The visible thread"): the one thread it
 * shows, oldest first, and the run in flight or the way the last one ended.
 * The chat is what reads it (`src/chat/assistant-ui/runtime.tsx`); what is
 * here is the shapes, the title rule, and the two calls about a conversation
 * that are neither a turn nor the panel's.
 */
import { ApiError, request } from "../api/client";
import type { components } from "../api/schema";
import type { ConversationId } from "../chat";

export type Conversation = components["schemas"]["ConversationSummary"];
export type Message = components["schemas"]["MessageView"];
export type Resume = components["schemas"]["ResumeView"];
export type EndedBadly = components["schemas"]["EndedBadlyView"];

/** What the panel and the view call a conversation nobody has named. */
export const UNTITLED = "Untitled";

/**
 * The title to show, which is never nothing.
 *
 * A conversation's title is the beginning of its first message and a message
 * with no text in it gives none (`docs/specs/conversations.md`, "Titles"), so
 * an empty title is a state the API really has. A blank line in the panel
 * would be a conversation nobody could aim at.
 */
export function shownTitle(title: string): string {
  return title.trim() === "" ? UNTITLED : title;
}

/** What a message says, as one string: its text parts, joined. */
export function textOf(message: Message): string {
  return message.parts
    .filter((part) => part.kind === "text")
    .map((part) => part.text)
    .join("");
}

/**
 * Stop the run that is in flight.
 *
 * **Any 2xx is the stop arriving**, whatever its body: the server accepts it
 * at once (202) and the pod running the turn stops it, which the stream
 * watching it then says.
 */
export async function cancelRun(
  id: ConversationId,
  runId: string,
): Promise<void> {
  try {
    await request(
      "post",
      "/api/conversations/{conversation_id}/runs/{run_id}/cancel",
      { path: { conversation_id: id, run_id: runId } },
    );
  } catch (failure) {
    const status = failure instanceof ApiError ? failure.status : 0;
    if (status < 200 || status > 299) throw failure;
  }
}

/**
 * Move it to another model; the conversation as it then is.
 *
 * Its next turn runs on that model (`docs/specs/agents.md`). A run in flight
 * is no reason to refuse: it keeps the model it started with.
 */
export async function setModel(
  id: ConversationId,
  modelId: string,
): Promise<Conversation> {
  return request("put", "/api/conversations/{conversation_id}/model", {
    path: { conversation_id: id },
    body: { model_id: modelId },
  });
}

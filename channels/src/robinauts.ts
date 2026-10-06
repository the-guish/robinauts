// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The agent a channel runs: the deployment's channels endpoint, through a stock AG-UI
 * `HttpAgent`.
 *
 * What the Channels SDK hands that agent is AG-UI's `RunAgentInput` for a thread of the SDK's
 * own naming, with the platform's history. The deployment wants three things instead
 * (backend/src/robinauts/web/channels.py): the conversation as the thread id, the question
 * alone, and the platform user it speaks for in `forwardedProps.robinauts`. The handler in
 * channel.ts passes the platform thread and the user as two context entries; the `fetch` here
 * turns them into those three, and learns a new conversation's id from the answer's headers.
 */

import { HttpAgent } from "@copilotkit/channels-core";
import type { ContextEntry } from "@copilotkit/channels-core";

import type { Config } from "./config.ts";
import type { Threads } from "./threads.ts";

export const THREAD_KEY = "robinauts.thread";
export const CHANNEL_USER = "robinauts.user";

/** Any thread id that is not a conversation's starts a new conversation. */
export const NEW_CONVERSATION = "new";

export interface ChannelUser {
  id: string;
  name: string;
}

/** The two context entries a run of this bridge carries. */
export function runContext(thread: string, user: ChannelUser): ContextEntry[] {
  return [
    { description: THREAD_KEY, value: thread },
    { description: CHANNEL_USER, value: JSON.stringify(user) },
  ];
}

export function robinautsAgent(
  config: Config,
  threads: Threads,
  send: typeof fetch = fetch,
): (threadId: string) => HttpAgent {
  const url = `${config.apiUrl}/api/channels/agents/${encodeURIComponent(config.agentId)}/agui`;
  const headers = { Authorization: `Bearer ${config.secret}` };
  const fetchRun = robinautsFetch(config, threads, send);
  return (threadId) =>
    new HttpAgent({ url, headers, fetch: fetchRun, threadId });
}

interface RunInput {
  messages?: Array<{ role?: string }>;
  context?: ContextEntry[];
  forwardedProps?: Record<string, unknown>;
  [field: string]: unknown;
}

export function robinautsFetch(
  config: Config,
  threads: Threads,
  send: typeof fetch = fetch,
): (url: string, init: RequestInit) => Promise<Response> {
  return async (url, init) => {
    const input = JSON.parse(String(init.body)) as RunInput;
    const context = input.context ?? [];
    const key = context.find(
      (entry) => entry.description === THREAD_KEY,
    )?.value;
    const user = context.find(
      (entry) => entry.description === CHANNEL_USER,
    )?.value;
    if (key === undefined || user === undefined) {
      throw new Error(
        "a run names its platform thread and user: start it with runContext()",
      );
    }
    const question = (input.messages ?? [])
      .filter((message) => message.role === "user")
      .at(-1);
    const body = (threadId: string): string =>
      JSON.stringify({
        ...input,
        threadId,
        // The deployment keeps the history; it reads the last question and nothing before it.
        messages: question === undefined ? [] : [question],
        tools: [],
        context: [],
        forwardedProps: {
          ...input.forwardedProps,
          robinauts: {
            user: JSON.parse(user) as ChannelUser,
            model_id: config.modelId,
          },
        },
      });

    const known = threads.get(key);
    let response = await send(url, {
      ...init,
      body: body(known ?? NEW_CONVERSATION),
    });
    if (known !== undefined && response.status === 404) {
      // Deleted, or another agent's since the bridge was last configured: start a new one.
      await response.body?.cancel();
      threads.delete(key);
      response = await send(url, { ...init, body: body(NEW_CONVERSATION) });
    }
    const conversation = response.headers.get("x-robinauts-conversation-id");
    if (response.ok && conversation) threads.set(key, conversation);
    return response;
  };
}

/**
 * Whether the deployment serves this bridge, asked once at start-up so that a wrong URL,
 * secret or agent stops the bridge with a sentence rather than failing every message. The
 * question has no text, which the endpoint refuses after checking everything else and before
 * it creates anything; `undefined` means all is well.
 */
export async function probe(
  config: Config,
  send: typeof fetch = fetch,
): Promise<string | undefined> {
  const url = `${config.apiUrl}/api/channels/agents/${encodeURIComponent(config.agentId)}/agui`;
  let response: Response;
  try {
    response = await send(url, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${config.secret}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        threadId: NEW_CONVERSATION,
        runId: "probe",
        messages: [],
        forwardedProps: { robinauts: { user: { id: "probe", name: "probe" } } },
      }),
      signal: AbortSignal.timeout(10_000),
    });
  } catch (error) {
    return (
      `no answer from ${config.apiUrl} (${String((error as Error).cause ?? error)}): is the` +
      " backend running, and is ROBINAUTS_API_URL the address this container reaches it at?"
    );
  }
  const refusal = (await response.json().catch(() => ({}))) as {
    error?: string;
  };
  if (response.status === 422 && refusal.error === "InvalidValueError")
    return undefined;
  if (response.status === 401) {
    return "the deployment refused ROBINAUTS_CHANNELS_SECRET: it was started with another one";
  }
  if (response.status === 404 && refusal.error === "UnknownAgentError") {
    return `the deployment has no agent ${config.agentId}: set ROBINAUTS_AGENT_ID to one it has`;
  }
  if (response.status === 404) {
    return (
      "the deployment does not serve the channels endpoint: start it with" +
      " ROBINAUTS_CHANNELS_SECRET set, to the same secret as this bridge's"
    );
  }
  return `the deployment answered ${response.status} to a probe of the channels endpoint`;
}

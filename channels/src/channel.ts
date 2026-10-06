// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * One Channel for every platform: who may talk to the agent, and a run of it for every
 * message. Everything else -- receiving messages, streaming the answer back as the platform
 * shows it, one turn at a time per conversation -- is the Channels SDK's.
 */

import { createChannel } from "@copilotkit/channels-core";
import type {
  Channel,
  ChannelIdentityContext,
  PlatformAdapter,
} from "@copilotkit/channels-core";

import type { Config } from "./config.ts";
import { robinautsAgent, runContext } from "./robinauts.ts";
import type { Threads } from "./threads.ts";

// The SDK's anonymous usage telemetry goes to CopilotKit; this bridge sends nothing anywhere
// but to the platforms and the deployment. Both switches the SDK reads, set before any channel
// starts, which is when it reads them; the image sets them too.
process.env.COPILOTKIT_TELEMETRY_DISABLED = "true";
process.env.DO_NOT_TRACK = "1";

export function robinautsChannel(
  config: Config,
  adapters: PlatformAdapter[],
  threads: Threads,
  send: typeof fetch = fetch,
): Channel {
  const channel = createChannel({
    name: "robinauts",
    identifyUser: (context) => identify(config, context),
    adapters,
    agent: robinautsAgent(config, threads, send),
    // The deployment runs one turn at a time per conversation: a second message waits.
    store: { concurrency: "serial" },
  });

  channel.onMessage(async ({ thread, message }) => {
    if (message.user === null) {
      if (message.actor.kind === "human")
        await thread.post(notAllowed(message.platform, message.actor));
      return;
    }
    // Per person as well as per thread: in a thread several people write in, each has a
    // conversation of their own, since a conversation is private to its owner.
    const key = `${message.user.id}@${thread.platform}:${thread.conversationKey}`;
    await thread.runAgent({ context: runContext(key, message.user) });
  });

  return channel;
}

/**
 * The person behind a message, as the deployment's user, or `null` for a bot or for somebody
 * the allow list leaves out. A Telegram id is the person's everywhere; a Slack id, within its
 * workspace.
 */
export function identify(
  config: Config,
  { provider, tenant, actor }: ChannelIdentityContext,
): { id: string; name: string } | null {
  if (actor.kind !== "human") return null;
  const name = actor.name ?? actor.handle ?? actor.id;
  if (provider === "telegram" && config.telegram) {
    const { allowed } = config.telegram;
    const handle = actor.handle?.toLowerCase();
    const admitted =
      allowed.has("*") ||
      allowed.has(actor.id) ||
      (handle !== undefined && allowed.has(handle));
    return admitted ? { id: `telegram:${actor.id}`, name } : null;
  }
  if (provider === "slack" && config.slack) {
    const { allowed } = config.slack;
    const admitted = allowed.size === 0 || allowed.has(actor.id.toLowerCase());
    return admitted ? { id: `slack:${tenant.id}:${actor.id}`, name } : null;
  }
  return null;
}

function notAllowed(
  platform: string,
  actor: { id: string; handle?: string },
): string {
  const who = actor.handle
    ? `@${actor.handle} (id ${actor.id})`
    : `id ${actor.id}`;
  const variable =
    platform === "slack" ? "SLACK_ALLOWED_USERS" : "TELEGRAM_ALLOWED_USERS";
  return `Sorry, you may not use this assistant yet. Ask its operator to add ${who} to ${variable}.`;
}

// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The bridge's configuration, from the environment alone. Every problem is collected and the
 * start is refused with all of them, as the backend's own start is.
 */

export interface TelegramConfig {
  token: string;
  /** Telegram user ids or usernames, lower case and without "@"; `"*"` lets everyone in. */
  allowed: ReadonlySet<string>;
}

export interface SlackConfig {
  botToken: string;
  appToken: string;
  /** Slack member ids; empty lets the whole workspace in. */
  allowed: ReadonlySet<string>;
}

export interface Config {
  /** The deployment's base URL, without a trailing slash. */
  apiUrl: string;
  secret: string;
  agentId: string;
  /** A model of the deployment's; unset, a conversation runs on its agent's default. */
  modelId: string | undefined;
  /** Where the bridge remembers which platform thread is which conversation. */
  stateFile: string;
  showToolStatus: boolean;
  telegram: TelegramConfig | undefined;
  slack: SlackConfig | undefined;
}

export class ConfigError extends Error {}

const SHORTEST_SECRET = 32;

export function readConfig(env: Record<string, string | undefined>): Config {
  const problems: string[] = [];
  const get = (name: string): string => (env[name] ?? "").trim();
  const required = (name: string, what: string): string => {
    const value = get(name);
    if (!value) problems.push(`${name} is not set: ${what}`);
    return value;
  };

  const apiUrl = required(
    "ROBINAUTS_API_URL",
    "the Robinauts deployment's base URL, such as http://host.docker.internal:8000",
  ).replace(/\/+$/, "");
  if (apiUrl && !/^https?:\/\/[^/]+/.test(apiUrl)) {
    problems.push(`ROBINAUTS_API_URL is not an http(s) URL: ${apiUrl}`);
  }
  const secret = required(
    "ROBINAUTS_CHANNELS_SECRET",
    "the secret the deployment was started with, in the same variable",
  );
  if (secret && secret.length < SHORTEST_SECRET) {
    problems.push(
      `ROBINAUTS_CHANNELS_SECRET is shorter than ${SHORTEST_SECRET} characters`,
    );
  }
  const agentId = required(
    "ROBINAUTS_AGENT_ID",
    "the id of the agent the chat platforms talk to",
  );

  const telegramToken = get("TELEGRAM_BOT_TOKEN");
  let telegram: TelegramConfig | undefined;
  if (telegramToken) {
    const allowed = list(get("TELEGRAM_ALLOWED_USERS"));
    if (allowed.size === 0) {
      problems.push(
        "TELEGRAM_ALLOWED_USERS is not set: anyone on Telegram can find a bot and spend the" +
          " deployment's model budget, so name the user ids or usernames that may use it," +
          ' comma-separated, or "*" for everyone. A stranger is told their id.',
      );
    }
    telegram = { token: telegramToken, allowed };
  }

  const botToken = get("SLACK_BOT_TOKEN");
  const appToken = get("SLACK_APP_TOKEN");
  let slack: SlackConfig | undefined;
  if (botToken || appToken) {
    if (!botToken.startsWith("xoxb-"))
      problems.push("SLACK_BOT_TOKEN is not a bot token (xoxb-…)");
    if (!appToken.startsWith("xapp-")) {
      problems.push(
        "SLACK_APP_TOKEN is not an app-level token (xapp-…), which Socket Mode needs",
      );
    }
    slack = { botToken, appToken, allowed: list(get("SLACK_ALLOWED_USERS")) };
  }

  if (!telegram && !slack) {
    problems.push(
      "no platform: set TELEGRAM_BOT_TOKEN, or SLACK_BOT_TOKEN and SLACK_APP_TOKEN",
    );
  }
  if (problems.length > 0) throw new ConfigError(problems.join("\n"));

  return {
    apiUrl,
    secret,
    agentId,
    modelId: get("ROBINAUTS_MODEL_ID") || undefined,
    stateFile: get("CHANNELS_STATE_FILE") || "state/threads.json",
    showToolStatus: get("CHANNELS_SHOW_TOOL_STATUS") !== "false",
    telegram,
    slack,
  };
}

function list(value: string): Set<string> {
  return new Set(
    value
      .split(",")
      .map((entry) => entry.trim().replace(/^@/, "").toLowerCase())
      .filter(Boolean),
  );
}

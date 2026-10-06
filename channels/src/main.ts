// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The bridge: the platforms the environment names, one Channel over them, running until it is
 * stopped. `README.md` says how to configure and run it.
 */

import type { PlatformAdapter } from "@copilotkit/channels-core";
import { slack } from "@copilotkit/channels-slack";
import { telegram } from "@copilotkit/channels-telegram";

import { robinautsChannel } from "./channel.ts";
import { ConfigError, readConfig } from "./config.ts";
import type { Config } from "./config.ts";
import { probe } from "./robinauts.ts";
import { ThreadFile } from "./threads.ts";

function adaptersFor(config: Config): PlatformAdapter[] {
  const adapters: PlatformAdapter[] = [];
  if (config.telegram) {
    adapters.push(
      telegram({
        token: config.telegram.token,
        showToolStatus: config.showToolStatus,
      }),
    );
  }
  if (config.slack) {
    adapters.push(
      slack({
        botToken: config.slack.botToken,
        appToken: config.slack.appToken,
        showToolStatus: config.showToolStatus,
      }),
    );
  }
  return adapters;
}

async function main(): Promise<number> {
  let config: Config;
  try {
    config = readConfig(process.env);
  } catch (error) {
    if (!(error instanceof ConfigError)) throw error;
    console.error(error.message);
    return 1;
  }
  const problem = await probe(config);
  if (problem !== undefined) {
    console.error(problem);
    return 1;
  }
  const adapters = adaptersFor(config);
  const channel = robinautsChannel(
    config,
    adapters,
    new ThreadFile(config.stateFile),
  );

  // A Channel's lifecycle is its runner's, and this process is the runner: CopilotKit's own
  // runner is its hosted service, which this bridge does not use. `ɵruntime` is the seam that
  // runner drives (channels-core's README, "Running a Channel"); the SDK version is pinned.
  try {
    await channel.ɵruntime.start();
  } catch (error) {
    // The SDK has logged each platform's own error above this line.
    console.error((error as Error).message);
    return 1;
  }
  const platforms = adapters.map((adapter) => adapter.platform).join(" and ");
  console.info(
    `robinauts-channels: ${platforms} to agent ${config.agentId} at ${config.apiUrl}`,
  );

  await new Promise<void>((resolve) => {
    process.once("SIGINT", resolve);
    process.once("SIGTERM", resolve);
  });
  console.info("robinauts-channels: stopping");
  await channel.ɵruntime.stop();
  return 0;
}

process.exitCode = await main();

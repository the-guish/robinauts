// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The bridge over the SDK's own fake platform and a fake deployment: what reaches the
 * channels endpoint, and how a platform thread keeps its conversation.
 */

import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { mkdtempSync, readFileSync } from "node:fs";
import { createServer } from "node:http";
import type { IncomingMessage, Server, ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { FakeAdapter } from "@copilotkit/channels-core";
import type { ProviderActor } from "@copilotkit/channels-core";

import { identify, robinautsChannel } from "../src/channel.ts";
import { probe } from "../src/robinauts.ts";
import { ConfigError, readConfig } from "../src/config.ts";
import type { Config } from "../src/config.ts";
import { ThreadFile } from "../src/threads.ts";

const SECRET = "s".repeat(40);

interface Received {
  authorization: string | undefined;
  path: string;
  body: {
    threadId: string;
    messages: Array<{ role: string; content: unknown }>;
    tools: unknown[];
    context: unknown[];
    forwardedProps: {
      robinauts: { user: { id: string; name: string }; model_id?: string };
    };
  };
}

/** The endpoint as the deployment serves it, reduced to what the bridge relies on. */
function fakeDeployment(): {
  server: Server;
  received: Received[];
  gone: Set<string>;
} {
  const received: Received[] = [];
  const conversations = new Set<string>();
  const gone = new Set<string>();
  const server = createServer(
    (request: IncomingMessage, response: ServerResponse) => {
      let text = "";
      request.on("data", (chunk: Buffer) => (text += chunk.toString()));
      request.on("end", () => {
        const body = JSON.parse(text) as Received["body"];
        received.push({
          authorization: request.headers.authorization,
          path: request.url ?? "",
          body,
        });
        if (request.headers.authorization !== `Bearer ${SECRET}`) {
          response.writeHead(401).end();
          return;
        }
        let conversation = body.threadId;
        if (gone.has(conversation)) {
          response.writeHead(404, { "content-type": "application/json" });
          response.end(
            JSON.stringify({
              error: "SessionNotFoundError",
              detail: conversation,
            }),
          );
          return;
        }
        if (!conversations.has(conversation)) {
          conversation = randomUUID();
          conversations.add(conversation);
        }
        response.writeHead(200, {
          "content-type": "text/event-stream",
          "x-robinauts-conversation-id": conversation,
        });
        const send = (event: object): boolean =>
          response.write(`data: ${JSON.stringify(event)}\n\n`);
        send({ type: "RUN_STARTED", threadId: conversation, runId: "r" });
        send({ type: "TEXT_MESSAGE_START", messageId: "a", role: "assistant" });
        send({
          type: "TEXT_MESSAGE_CONTENT",
          messageId: "a",
          delta: "an answer",
        });
        send({ type: "TEXT_MESSAGE_END", messageId: "a" });
        send({ type: "RUN_FINISHED", threadId: conversation, runId: "r" });
        response.end();
      });
    },
  );
  return { server, received, gone };
}

function configFor(apiUrl: string, overrides: Partial<Config> = {}): Config {
  return {
    apiUrl,
    secret: SECRET,
    agentId: "assistant",
    modelId: undefined,
    stateFile: join(mkdtempSync(join(tmpdir(), "channels-")), "threads.json"),
    showToolStatus: false,
    telegram: { token: "123:abc", allowed: new Set(["ada", "7"]) },
    slack: undefined,
    ...overrides,
  };
}

async function until(done: () => boolean): Promise<void> {
  for (let tries = 0; !done(); tries++) {
    assert.ok(
      tries < 200,
      "waited a second for the bridge to call the deployment",
    );
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

test("a platform thread is one conversation, asked one question at a time", async (t) => {
  const { server, received, gone } = fakeDeployment();
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => server.close());
  const config = configFor(
    `http://127.0.0.1:${(server.address() as AddressInfo).port}`,
  );
  const adapter = new FakeAdapter({ platform: "telegram" });
  const threads = new ThreadFile(config.stateFile);
  const channel = robinautsChannel(config, [adapter], threads);
  await channel.ɵruntime.start();
  t.after(() => channel.ɵruntime.stop());

  const ada: ProviderActor = {
    id: "42",
    kind: "human",
    name: "Ada Lovelace",
    handle: "ada",
  };
  const say = async (userText: string, actor = ada): Promise<void> => {
    const before = received.length;
    await adapter.getSink().onTurn({
      conversationKey: "tg:42:dm",
      replyTarget: {},
      userText,
      platform: "telegram",
      actor,
    });
    await until(() => received.length > before);
  };

  await say("hello");
  const first = received[0]!;
  assert.equal(first.authorization, `Bearer ${SECRET}`);
  assert.equal(first.path, "/api/channels/agents/assistant/agui");
  assert.equal(first.body.threadId, "new");
  assert.deepEqual(first.body.forwardedProps.robinauts.user, {
    id: "telegram:42",
    name: "Ada Lovelace",
  });
  assert.deepEqual(first.body.messages, [first.body.messages[0]]);
  assert.equal(first.body.messages[0]?.content, "hello");
  assert.deepEqual([first.body.tools, first.body.context], [[], []]);

  await until(() => threads.get("telegram:42@telegram:tg:42:dm") !== undefined);
  const conversation = threads.get("telegram:42@telegram:tg:42:dm")!;
  assert.deepEqual(JSON.parse(readFileSync(config.stateFile, "utf8")), {
    "telegram:42@telegram:tg:42:dm": conversation,
  });

  // The next message of the thread continues the conversation, with only its own question.
  await say("and then?");
  const second = received[1]!;
  assert.equal(second.body.threadId, conversation);
  assert.deepEqual(
    second.body.messages.map((message) => message.content),
    ["and then?"],
  );

  // A conversation the deployment no longer has is replaced by a new one.
  gone.add(conversation);
  await say("still there?");
  await until(() => received.length === 4);
  assert.deepEqual(
    received.slice(2).map((request) => request.body.threadId),
    [conversation, "new"],
  );
  await until(
    () => threads.get("telegram:42@telegram:tg:42:dm") !== conversation,
  );

  // Somebody else in the same thread has a conversation of their own.
  await say("me too", {
    id: "7",
    kind: "human",
    name: "Grace",
    handle: "grace",
  });
  assert.equal(received.at(-1)!.body.threadId, "new");
  await until(() => threads.get("telegram:7@telegram:tg:42:dm") !== undefined);
  assert.notEqual(
    threads.get("telegram:7@telegram:tg:42:dm"),
    threads.get("telegram:42@telegram:tg:42:dm"),
  );
});

test("only the people the allow list names are the deployment's users", () => {
  const config = configFor("http://deployment");
  const context = (actor: {
    id: string;
    handle?: string;
    kind?: "human" | "bot";
  }) => ({
    provider: "telegram",
    tenant: { id: "group" },
    installation: { id: "i" },
    conversation: { id: "c" },
    trigger: "message",
    event: {},
    raw: {},
    actor: { kind: "human" as const, ...actor },
  });
  assert.deepEqual(identify(config, context({ id: "1", handle: "Ada" })), {
    id: "telegram:1",
    name: "Ada",
  });
  assert.equal(identify(config, context({ id: "7" }))?.id, "telegram:7");
  assert.equal(identify(config, context({ id: "8", handle: "eve" })), null);
  assert.equal(identify(config, context({ id: "7", kind: "bot" })), null);
  const everyone = configFor("http://deployment", {
    telegram: { token: "t", allowed: new Set(["*"]) },
  });
  assert.equal(identify(everyone, context({ id: "8" }))?.id, "telegram:8");
  const slack = configFor("http://deployment", {
    slack: { botToken: "xoxb-", appToken: "xapp-", allowed: new Set() },
  });
  assert.equal(
    identify(slack, {
      ...context({ id: "U1" }),
      provider: "slack",
      tenant: { id: "T1" },
    })?.id,
    "slack:T1:U1",
  );
});

test("the configuration names every problem at once", () => {
  assert.throws(
    () => readConfig({}),
    (error: unknown) => {
      assert.ok(error instanceof ConfigError);
      assert.match(error.message, /ROBINAUTS_API_URL/);
      assert.match(error.message, /ROBINAUTS_CHANNELS_SECRET/);
      assert.match(error.message, /ROBINAUTS_AGENT_ID/);
      assert.match(error.message, /no platform/);
      return true;
    },
  );
  const telegram = {
    ROBINAUTS_API_URL: "http://host.docker.internal:8000/",
    ROBINAUTS_CHANNELS_SECRET: SECRET,
    ROBINAUTS_AGENT_ID: "assistant",
    TELEGRAM_BOT_TOKEN: "123:abc",
  };
  assert.throws(() => readConfig(telegram), /TELEGRAM_ALLOWED_USERS/);
  const config = readConfig({
    ...telegram,
    TELEGRAM_ALLOWED_USERS: "@Ada, 42",
  });
  assert.equal(config.apiUrl, "http://host.docker.internal:8000");
  assert.deepEqual([...config.telegram!.allowed], ["ada", "42"]);
  assert.equal(config.slack, undefined);
  assert.throws(
    () =>
      readConfig({
        ...telegram,
        TELEGRAM_ALLOWED_USERS: "*",
        SLACK_BOT_TOKEN: "xoxb-1",
      }),
    /SLACK_APP_TOKEN/,
  );
});

test("the start-up probe says what is wrong with the deployment or its settings", async () => {
  const config = configFor("http://deployment");
  const answering =
    (status: number, body: object): typeof fetch =>
    async () =>
      new Response(JSON.stringify(body), { status });
  assert.equal(
    await probe(config, answering(422, { error: "InvalidValueError" })),
    undefined,
  );
  assert.match(
    (await probe(config, answering(401, {})))!,
    /ROBINAUTS_CHANNELS_SECRET/,
  );
  assert.match(
    (await probe(config, answering(404, { error: "UnknownAgentError" })))!,
    /no agent assistant/,
  );
  assert.match(
    (await probe(config, answering(404, { detail: "Not Found" })))!,
    /does not serve the channels endpoint/,
  );
  const unreachable: typeof fetch = async () => {
    throw new TypeError("fetch failed");
  };
  assert.match((await probe(config, unreachable))!, /no answer from/);
});

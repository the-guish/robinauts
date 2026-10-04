// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import { expect, test, vi } from "vitest";

import { ApiError } from "../../../api/client";
import { refusal } from "../../../test/api";
import { event, streamed, streamHeaders, writable } from "../../../test/stream";
import {
  attach,
  backoff,
  reconnectNow,
  RETRIES,
  startNewConversation,
  startTurn,
  type Attached,
  type Numbered,
} from "./client";

const RUN = "11111111-2222-4333-8444-555555555555";
const CONVERSATION = "00000000-0000-4000-8000-000000000001";

/** Never waited for: the backoff is what a test would otherwise sit through. */
const at_once = { wait: () => Promise.resolve() };

/** Answer each call in turn, and remember what was asked. */
function answering(answers: (() => Response)[]) {
  let at = 0;
  const fetch = vi.fn<typeof globalThis.fetch>((input, init) => {
    const answer = answers[at];
    at += 1;
    if (answer === undefined) throw new Error(`no answer for ${String(input)}`);
    void init;
    return Promise.resolve(answer());
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

/** Everything that stream said. */
async function all(attached: Attached) {
  const seen = [];
  for await (const each of attached.events) seen.push(each);
  return seen;
}

const finished = (position?: number) =>
  event("RUN_FINISHED", { threadId: CONVERSATION, runId: RUN }, position);

/**
 * How a run that ended before the position asked from is answered.
 *
 * Read from the record and made up by the `api` layer, so it carries **no**
 * position: the platform numbered nothing there (`docs/specs/wire.md`).
 */
const alreadyOver = () => finished();

test("a new conversation: what is posted, and what comes back", async () => {
  const fetch = answering([
    () =>
      streamed(
        [
          event("RUN_STARTED", { threadId: CONVERSATION, runId: RUN }, 1),
          event("TEXT_MESSAGE_START", { messageId: "m", role: "assistant" }, 2),
          event("TEXT_MESSAGE_CONTENT", { messageId: "m", delta: "hi" }, 3),
          finished(9),
        ],
        { headers: streamHeaders(RUN, CONVERSATION) },
      ),
  ]);
  const attached = await startNewConversation("helper", "opus", "hello");
  expect(attached.runId).toBe(RUN);
  expect(attached.conversationId).toBe(CONVERSATION);
  const [url, init] = fetch.mock.calls[0] ?? [];
  expect(url).toBe("/api/turns");
  expect(init?.method).toBe("POST");
  // The model goes with the first message; `null` would be the agent's
  // default, which the backend copies in (`docs/specs/wire.md`).
  expect(JSON.parse(String(init?.body))).toEqual({
    agent_id: "helper",
    model_id: "opus",
    text: "hello",
  });
  const seen = await all(attached);
  expect(seen.map((each) => each.event.type)).toEqual([
    "RUN_STARTED",
    "TEXT_MESSAGE_START",
    "TEXT_MESSAGE_CONTENT",
    "RUN_FINISHED",
  ]);
  expect(seen.map((each) => each.position)).toEqual([1, 2, 3, 9]);
});

test("no model chosen is said as null: the agent's default", async () => {
  const fetch = answering([
    () =>
      streamed([finished(9)], { headers: streamHeaders(RUN, CONVERSATION) }),
  ]);
  await startNewConversation("helper", null, "hello");
  expect(JSON.parse(String(fetch.mock.calls[0]?.[1]?.body))).toEqual({
    agent_id: "helper",
    model_id: null,
    text: "hello",
  });
});

test("the two shapes of a turn in a conversation that exists", async () => {
  const fetch = answering([
    () =>
      streamed([finished(9)], { headers: streamHeaders(RUN, CONVERSATION) }),
    () =>
      streamed([finished(9)], { headers: streamHeaders(RUN, CONVERSATION) }),
  ]);
  await startTurn(CONVERSATION, { text: "more", parentId: "m" });
  await startTurn(CONVERSATION, { regenerate: "m" });
  expect(fetch.mock.calls[0]?.[0]).toBe(
    `/api/conversations/${CONVERSATION}/turns`,
  );
  // Neither names a model: a turn runs on the conversation's, which the
  // server reads (`docs/specs/wire.md`).
  expect(JSON.parse(String(fetch.mock.calls[0]?.[1]?.body))).toEqual({
    text: "more",
    parent_id: "m",
  });
  expect(JSON.parse(String(fetch.mock.calls[1]?.[1]?.body))).toEqual({
    regenerate: "m",
  });
});

test("a refusal comes before the stream, and is an ApiError", async () => {
  answering([
    () =>
      refusal(
        409,
        "RunAlreadyActiveError",
        `run ${RUN} is running in conversation ${CONVERSATION}`,
      ),
  ]);
  const refused = await startTurn(CONVERSATION, {
    text: "again",
    parentId: "m2",
  }).catch((failure: unknown) => failure);
  expect(refused).toBeInstanceOf(ApiError);
  expect((refused as ApiError).status).toBe(409);
  expect((refused as ApiError).error).toBe("RunAlreadyActiveError");
});

test("an answer that is not one of our streams is refused too", async () => {
  answering([() => streamed(["hello"], { headers: {} })]);
  await expect(
    startNewConversation("helper", null, "x"),
  ).rejects.toBeInstanceOf(ApiError);
});

test("the two ids a stream names itself by are uuids or it is not ours", async () => {
  // They go straight into a request path and into a route, so a header that
  // is not one of the backend's ids is not read as one (`src/router.ts`).
  for (const [run, conversation] of [
    ["../runs/other", CONVERSATION],
    [RUN, "#/c/somewhere"],
    ["", CONVERSATION],
    [RUN, `${CONVERSATION}/../..`],
  ]) {
    answering([
      () =>
        streamed([finished(9)], {
          headers: streamHeaders(run ?? "", conversation ?? ""),
        }),
    ]);
    const refusedBy = await startNewConversation("helper", null, "x").catch(
      (failure: unknown) => failure,
    );
    expect(refusedBy).toBeInstanceOf(ApiError);
    expect((refusedBy as ApiError).error).toBe("unreadable_stream");
  }
});

test("a dropped connection is picked up with Last-Event-ID", async () => {
  const fetch = answering([
    () =>
      // Two events, a position of 3, and then the connection goes: no event
      // in it said the run was over.
      streamed(
        [
          event("TEXT_MESSAGE_START", { messageId: "m", role: "assistant" }, 2),
          event("TEXT_MESSAGE_CONTENT", { messageId: "m", delta: "one " }, 3),
        ],
        { headers: streamHeaders(RUN, CONVERSATION) },
      ),
    () =>
      streamed(
        [
          event("TEXT_MESSAGE_CONTENT", { messageId: "m", delta: "two" }, 4),
          finished(9),
        ],
        { headers: streamHeaders(RUN, CONVERSATION) },
      ),
  ]);
  const attached = await startNewConversation("helper", null, "x", at_once);
  const seen = await all(attached);
  expect(seen.map((each) => each.event.type)).toEqual([
    "TEXT_MESSAGE_START",
    "TEXT_MESSAGE_CONTENT",
    "TEXT_MESSAGE_CONTENT",
    "RUN_FINISHED",
  ]);
  const [url, init] = fetch.mock.calls[1] ?? [];
  expect(url).toBe(`/api/conversations/${CONVERSATION}/runs/${RUN}/events`);
  expect(init?.method).toBe("GET");
  expect((init?.headers as Record<string, string>)["last-event-id"]).toBe("3");
});

test("the position to carry on from is the last one the backend numbered", async () => {
  // A bracket around thinking carries no id: re-attaching after one would
  // ask from a platform event the client has not had in full
  // (`docs/specs/wire.md`).
  const fetch = answering([
    () =>
      streamed(
        [
          event("TEXT_MESSAGE_CONTENT", { messageId: "m", delta: "a" }, 5),
          event("REASONING_MESSAGE_END", { messageId: "m:reasoning:4" }),
        ],
        { headers: streamHeaders(RUN, CONVERSATION) },
      ),
    () =>
      streamed([finished(9)], { headers: streamHeaders(RUN, CONVERSATION) }),
  ]);
  const seen = await all(
    await startNewConversation("helper", null, "x", at_once),
  );
  expect(seen.map((each) => each.position)).toEqual([5, null, 9]);
  expect(
    (fetch.mock.calls[1]?.[1]?.headers as Record<string, string>)[
      "last-event-id"
    ],
  ).toBe("5");
});

test("a terminal event stops it: no stream is opened again", async () => {
  const fetch = answering([
    () =>
      streamed([finished(9)], { headers: streamHeaders(RUN, CONVERSATION) }),
  ]);
  await all(await startNewConversation("helper", null, "x", at_once));
  expect(fetch).toHaveBeenCalledTimes(1);
});

test("a run that ended in an error is an ending too", async () => {
  const fetch = answering([
    () =>
      streamed([event("RUN_ERROR", { message: "no", code: "failed" })], {
        headers: streamHeaders(RUN, CONVERSATION),
      }),
  ]);
  const seen = await all(
    await startNewConversation("helper", null, "x", at_once),
  );
  expect(seen.map((each) => each.event.type)).toEqual(["RUN_ERROR"]);
  expect(fetch).toHaveBeenCalledTimes(1);
});

test("the tries are bounded, and giving up is something to say", async () => {
  const dropping = () =>
    streamed([], { headers: streamHeaders(RUN, CONVERSATION) });
  const fetch = answering(Array.from({ length: RETRIES + 2 }, () => dropping));
  const attached = await startNewConversation("helper", null, "x", at_once);
  const lost = await all(attached).catch((failure: unknown) => failure);
  expect(lost).toBeInstanceOf(ApiError);
  expect((lost as ApiError).error).toBe("stream_lost");
  // The first stream and one per retry, and not a sixth.
  expect(fetch).toHaveBeenCalledTimes(RETRIES + 1);
});

test("a re-attach that is refused stops it rather than trying for ever", async () => {
  // A refusal is the backend's answer, not a connection that went: 404 (not
  // this person's run), 422 (a position it has not reached), 401 (the session
  // ended). Asking again asks the same question.
  for (const [status, error] of [
    [404, "NotFoundError"],
    [422, "InvalidValueError"],
    [401, "NotSignedInError"],
  ] as const) {
    const fetch = answering([
      () => streamed([], { headers: streamHeaders(RUN, CONVERSATION) }),
      () => refusal(status, error, "no"),
    ]);
    const attached = await startNewConversation("helper", null, "x", at_once);
    const stopped = await all(attached).catch((failure: unknown) => failure);
    expect((stopped as ApiError).status).toBe(status);
    expect(fetch).toHaveBeenCalledTimes(2);
  }
});

test("a re-attach whose request never arrives is tried again", async () => {
  // **The request, not only the stream.** A dropped network's most likely
  // failure is the next `GET` not getting through; outside the retry that
  // would have been the one failure that ended the watch.
  let at = 0;
  const fetch = vi.fn<typeof globalThis.fetch>(() => {
    at += 1;
    if (at === 1) {
      return Promise.resolve(
        streamed([], { headers: streamHeaders(RUN, CONVERSATION) }),
      );
    }
    if (at <= 3) return Promise.reject(new TypeError("no route to host"));
    return Promise.resolve(
      streamed([finished(9)], { headers: streamHeaders(RUN, CONVERSATION) }),
    );
  });
  vi.stubGlobal("fetch", fetch);
  const seen = await all(
    await startNewConversation("helper", null, "x", at_once),
  );
  expect(seen.map((each) => each.event.type)).toEqual(["RUN_FINISHED"]);
  expect(fetch).toHaveBeenCalledTimes(4);
});

test("a deployment that is restarting answers 5xx, and that is tried again", async () => {
  let at = 0;
  const fetch = vi.fn<typeof globalThis.fetch>(() => {
    at += 1;
    if (at === 1) {
      return Promise.resolve(
        streamed([], { headers: streamHeaders(RUN, CONVERSATION) }),
      );
    }
    if (at === 2) {
      return Promise.resolve(refusal(503, "Unavailable", "restarting"));
    }
    return Promise.resolve(
      streamed([finished(9)], { headers: streamHeaders(RUN, CONVERSATION) }),
    );
  });
  vi.stubGlobal("fetch", fetch);
  const seen = await all(
    await startNewConversation("helper", null, "x", at_once),
  );
  expect(seen).toHaveLength(1);
  expect(fetch).toHaveBeenCalledTimes(3);
});

test("requests that never arrive are bounded like streams that drop", async () => {
  const fetch = vi.fn<typeof globalThis.fetch>((input) =>
    String(input) === "/api/turns"
      ? Promise.resolve(
          streamed([], { headers: streamHeaders(RUN, CONVERSATION) }),
        )
      : Promise.reject(new TypeError("no route to host")),
  );
  vi.stubGlobal("fetch", fetch);
  const attached = await startNewConversation("helper", null, "x", at_once);
  const lost = await all(attached).catch((failure: unknown) => failure);
  expect((lost as ApiError).error).toBe("stream_lost");
  // The first stream and one `GET` per try, and not a fifth.
  expect(fetch).toHaveBeenCalledTimes(RETRIES + 1);
});

test("a block replayed at a position already seen is not read again", async () => {
  // Nothing in front of the deployment may make a delta arrive twice, and a
  // replay must not be mistaken for delivery: counting it would reset the
  // budget, and this watcher would reconnect for ever.
  const replay = () =>
    streamed(
      [event("TEXT_MESSAGE_CONTENT", { messageId: "m", delta: "once" }, 3)],
      { headers: streamHeaders(RUN, CONVERSATION) },
    );
  const fetch = answering(Array.from({ length: 12 }, () => replay));
  const attached = await startNewConversation("helper", null, "x", at_once);
  const seen: Numbered[] = [];
  const lost = await (async () => {
    try {
      for await (const each of attached.events) seen.push(each);
    } catch (failure: unknown) {
      return failure;
    }
    return null;
  })();
  // Said once, however many times it was sent.
  expect(seen).toHaveLength(1);
  expect((lost as ApiError).error).toBe("stream_lost");
  expect(fetch).toHaveBeenCalledTimes(RETRIES + 1);
});

test("an id nothing here could have sent is not a position, and not a loss", () => {
  // This build writes decimal digits and nothing else, so anything else came
  // from something in front of the deployment. The event is read; the id is
  // not, and a re-attach asks from the last real position. `Number` is
  // generous where this must not be -- it makes `""` zero, `0x10` sixteen
  // and ` 4 ` four -- so the shape is read first.
  const cases = [
    ["", "an empty id"],
    ["0x10", "a hexadecimal one"],
    ["later", "one that is not a number at all"],
    [" 4 ", "one with space around it"],
    ["4.0", "one with a point in it"],
    ["-1", "a negative one"],
  ] as const;
  for (const [id, what] of cases) {
    void what;
    expect(/^[0-9]+$/.test(id)).toBe(false);
  }
});

test("a block with an id of that kind is read, and moves nothing", async () => {
  for (const id of ["", "0x10", "later"]) {
    const fetch = answering([
      () =>
        streamed(
          [
            event("TEXT_MESSAGE_CONTENT", { messageId: "m", delta: "a" }, 4),
            `id: ${id}\nevent: TEXT_MESSAGE_CONTENT\n` +
              'data: {"type":"TEXT_MESSAGE_CONTENT","messageId":"m","delta":"b"}\n\n',
          ],
          { headers: streamHeaders(RUN, CONVERSATION) },
        ),
      () =>
        streamed([alreadyOver()], {
          headers: streamHeaders(RUN, CONVERSATION),
        }),
    ]);
    const seen = await all(
      await startNewConversation("helper", null, "x", at_once),
    );
    expect(seen.map((each) => each.event)).toEqual([
      { type: "TEXT_MESSAGE_CONTENT", messageId: "m", delta: "a" },
      { type: "TEXT_MESSAGE_CONTENT", messageId: "m", delta: "b" },
      { type: "RUN_FINISHED", runId: RUN, cancelled: false },
    ]);
    expect(seen.map((each) => each.position)).toEqual([4, null, null]);
    // And the position carried on from is the last real one.
    expect(
      (fetch.mock.calls[1]?.[1]?.headers as Record<string, string>)[
        "last-event-id"
      ],
    ).toBe("4");
  }
});

test("an ending is read whatever its id says", async () => {
  // A watcher that dropped the one event saying the run is over would wait
  // for an answer that has already been given.
  answering([
    () =>
      streamed(
        [
          event("TEXT_MESSAGE_CONTENT", { messageId: "m", delta: "a" }, 5),
          finished(2),
        ],
        { headers: streamHeaders(RUN, CONVERSATION) },
      ),
  ]);
  const seen = await all(
    await startNewConversation("helper", null, "x", at_once),
  );
  expect(seen.map((each) => each.event.type)).toEqual([
    "TEXT_MESSAGE_CONTENT",
    "RUN_FINISHED",
  ]);
  // Not a position to carry on after: it did not move the run forward.
  expect(seen[1]?.position).toBeNull();
});

test("the budget is spent on connections that do not deliver, not on time", async () => {
  // A run answering for an hour over a flaky link reconnects more than
  // `RETRIES` times; what is bounded is tries since the last event that arrived.
  let at = 0;
  const fetch = vi.fn<typeof globalThis.fetch>(() => {
    at += 1;
    // Every other stream delivers one numbered event and then drops.
    return Promise.resolve(
      at % 2 === 1
        ? streamed(
            [
              event(
                "TEXT_MESSAGE_CONTENT",
                { messageId: "m", delta: `${at} ` },
                at,
              ),
            ],
            { headers: streamHeaders(RUN, CONVERSATION) },
          )
        : streamed([], { headers: streamHeaders(RUN, CONVERSATION) }),
    );
  });
  vi.stubGlobal("fetch", fetch);
  const attached = await startNewConversation("helper", null, "x", at_once);
  const seen = [];
  for await (const each of attached.events) {
    seen.push(each);
    if (seen.length === RETRIES + 2) break;
  }
  expect(seen).toHaveLength(RETRIES + 2);
  expect(fetch.mock.calls.length).toBeGreaterThan(RETRIES + 1);
});

test("attaching asks from where the conversation said to, and says so once", async () => {
  const fetch = answering([
    () =>
      streamed([alreadyOver()], { headers: streamHeaders(RUN, CONVERSATION) }),
  ]);
  const attached = await attach(CONVERSATION, RUN, 12);
  expect(attached.runId).toBe(RUN);
  const [url, init] = fetch.mock.calls[0] ?? [];
  expect(url).toBe(`/api/conversations/${CONVERSATION}/runs/${RUN}/events`);
  const headers = init?.headers as Record<string, string>;
  expect(headers["last-event-id"]).toBe("12");
  // `after` and `Last-Event-ID` are two ways of saying one thing, and the
  // backend refuses two that disagree. One of them is sent.
  expect(String(url)).not.toContain("after=");
  await all(attached);
});

test("attaching from the beginning says nothing about a position", async () => {
  const fetch = answering([
    () =>
      streamed([finished(9)], { headers: streamHeaders(RUN, CONVERSATION) }),
  ]);
  await all(await attach(CONVERSATION, RUN, 0));
  const headers = fetch.mock.calls[0]?.[1]?.headers as Record<string, string>;
  expect(headers["last-event-id"]).toBeUndefined();
});

test("a stream that drops after an attach carries on from where it was", async () => {
  const fetch = answering([
    () => streamed([], { headers: streamHeaders(RUN, CONVERSATION) }),
    () =>
      streamed([alreadyOver()], { headers: streamHeaders(RUN, CONVERSATION) }),
  ]);
  await all(await attach(CONVERSATION, RUN, 12, at_once));
  const headers = fetch.mock.calls[1]?.[1]?.headers as Record<string, string>;
  expect(headers["last-event-id"]).toBe("12");
});

test("a watcher that goes away stops reading, and says it was its own doing", async () => {
  const { response, write } = writable({
    headers: streamHeaders(RUN, CONVERSATION),
  });
  answering([() => response]);
  const stopping = new AbortController();
  const attached = await startNewConversation("helper", null, "x", {
    signal: stopping.signal,
  });
  const seen: string[] = [];
  const reading = (async () => {
    for await (const each of attached.events) seen.push(each.event.type);
  })();
  write(event("TEXT_MESSAGE_START", { messageId: "m", role: "assistant" }, 1));
  await new Promise((done) => setTimeout(done, 0));
  stopping.abort(new Error("the tab went away"));
  await expect(reading).rejects.toThrow("the tab went away");
  expect(seen).toEqual(["TEXT_MESSAGE_START"]);
});

test("a request that never arrived is an ApiError, not a TypeError", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn<typeof globalThis.fetch>().mockRejectedValue(new TypeError("no net")),
  );
  const failed = await startNewConversation("helper", null, "x").catch(
    (failure: unknown) => failure,
  );
  expect(failed).toBeInstanceOf(ApiError);
  expect((failed as ApiError).error).toBe("network_error");
});

test("the wait doubles from half a second to thirty, a quarter either way", () => {
  expect(backoff(0, () => 0.5)).toBe(500);
  expect(backoff(1, () => 0.5)).toBe(1000);
  expect(backoff(5, () => 0.5)).toBe(16_000);
  expect(backoff(6, () => 0.5)).toBe(30_000);
  expect(backoff(40, () => 0.5)).toBe(30_000);
  expect(backoff(0, () => 0)).toBe(375);
  expect(backoff(0, () => 1)).toBe(625);
});

test("a stream that says nothing for too long is dropped and picked up again", async () => {
  // A proxy that lost the connection without closing it: no byte, no end.
  const { response } = writable({ headers: streamHeaders(RUN, CONVERSATION) });
  const fetch = answering([
    () => response,
    () =>
      streamed([finished(9)], { headers: streamHeaders(RUN, CONVERSATION) }),
  ]);
  const told: boolean[] = [];
  const attached = await startNewConversation("helper", null, "x", {
    ...at_once,
    idleMs: 20,
    onReconnecting: (now) => told.push(now),
  });
  const seen = await all(attached);
  expect(seen.map((each) => each.event.type)).toEqual(["RUN_FINISHED"]);
  expect(fetch).toHaveBeenCalledTimes(2);
  // Said while it was picked up again, and taken back once it was.
  expect(told).toEqual([true, false]);
});

test("a keep-alive is a stream that is alive", async () => {
  const { response, write, close } = writable({
    headers: streamHeaders(RUN, CONVERSATION),
  });
  const fetch = answering([() => response]);
  const attached = await startNewConversation("helper", null, "x", {
    ...at_once,
    idleMs: 250,
  });
  const reading = all(attached);
  // Longer, all told, than the stream may be quiet; never quiet that long.
  for (let n = 0; n < 8; n += 1) {
    await new Promise((done) => setTimeout(done, 50));
    write(": keep-alive\n\n");
  }
  write(finished(9));
  close();
  expect((await reading).map((each) => each.event.type)).toEqual([
    "RUN_FINISHED",
  ]);
  expect(fetch).toHaveBeenCalledTimes(1);
});

test("reconnecting now cuts the wait short", async () => {
  // With the clock stopped, the wait between two tries never ends by itself:
  // only the nudge can end it.
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  try {
    const fetch = answering([
      () => streamed([], { headers: streamHeaders(RUN, CONVERSATION) }),
      () =>
        streamed([finished(9)], {
          headers: streamHeaders(RUN, CONVERSATION),
        }),
    ]);
    let waiting = false;
    const attached = await startNewConversation("helper", null, "x", {
      onReconnecting: (now) => {
        waiting = now;
      },
    });
    const reading = all(attached);
    for (let n = 0; n < 1000 && !waiting; n += 1) await Promise.resolve();
    expect(waiting).toBe(true);
    expect(fetch).toHaveBeenCalledTimes(1);
    reconnectNow();
    expect((await reading).map((each) => each.event.type)).toEqual([
      "RUN_FINISHED",
    ]);
    expect(fetch).toHaveBeenCalledTimes(2);
  } finally {
    vi.useRealTimers();
  }
});

test("a stream a stopping server ends with a hint is re-attached as it says", async () => {
  const hint = [
    "retry: 1000",
    "event: CUSTOM",
    `data: ${JSON.stringify({
      type: "CUSTOM",
      name: "robinauts.reconnect",
      value: { after_ms: 1000 },
    })}`,
    "",
    "",
  ].join("\n");
  const ending = () => {
    const { response, write, close } = writable({
      headers: streamHeaders(RUN, CONVERSATION),
    });
    write(
      event("TEXT_MESSAGE_START", { messageId: "m", role: "assistant" }, 2),
    );
    write(hint);
    close();
    return response;
  };
  // More hints than tries: none is spent on a stream the server ended.
  const fetch = answering([
    ...Array.from({ length: RETRIES + 2 }, () => ending),
    () =>
      streamed([finished(9)], { headers: streamHeaders(RUN, CONVERSATION) }),
  ]);
  const waits: number[] = [];
  const told: boolean[] = [];
  const attached = await startNewConversation("helper", null, "x", {
    wait: (ms) => {
      waits.push(ms);
      return Promise.resolve();
    },
    onReconnecting: (now) => told.push(now),
  });
  const seen = await all(attached);
  expect(seen.at(-1)?.event.type).toBe("RUN_FINISHED");
  expect(fetch).toHaveBeenCalledTimes(RETRIES + 3);
  expect(waits.every((ms) => ms === 1000)).toBe(true);
  // Nothing to say about a connection the server handed on.
  expect(told).toEqual([]);
});

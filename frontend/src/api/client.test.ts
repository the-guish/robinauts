// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import { afterEach, expect, test, vi } from "vitest";

import { ApiError, onUnauthorized, request } from "./client";

/** Answers the next `fetch` with this, and remembers what it was asked. */
function answering(
  status: number,
  body: string | null,
  contentType = "application/json",
) {
  const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
    new Response(body, {
      status,
      headers: { "content-type": contentType },
    }),
  );
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

/** What `fetch` was called with, as the shapes this module actually passes. */
function asked(fetch: ReturnType<typeof answering>) {
  const [url, init] = fetch.mock.calls[0] ?? [];
  return {
    url,
    headers: (init?.headers ?? {}) as Record<string, string>,
    init,
  };
}

/** The error a call threw, or a failure if it did not throw one. */
async function refused(call: Promise<unknown>): Promise<ApiError> {
  const failure = await call.then(
    () => new Error("the call was expected to fail and did not"),
    (error: unknown) => error,
  );
  expect(failure).toBeInstanceOf(ApiError);
  return failure as ApiError;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

test("a refusal becomes an ApiError carrying the code and the detail", async () => {
  answering(
    404,
    JSON.stringify({ error: "not_found", detail: "no such conversation" }),
  );

  const refusal = await refused(
    request("get", "/api/conversations/{conversation_id}", {
      path: { conversation_id: "c-1" },
    }),
  );

  expect(refusal.status).toBe(404);
  expect(refusal.error).toBe("not_found");
  expect(refusal.detail).toBe("no such conversation");
});

test("a refusal that is not one of ours still becomes an ApiError", async () => {
  answering(502, "<html>the proxy is sorry</html>", "text/html");

  const refusal = await refused(request("get", "/api/agents"));

  expect(refusal.status).toBe(502);
  expect(refusal.error).toBe("http_502");
});

test("a request that never arrives becomes an ApiError", async () => {
  const fetch = vi
    .fn<typeof globalThis.fetch>()
    .mockRejectedValue(new TypeError("Failed to fetch"));
  vi.stubGlobal("fetch", fetch);

  const refusal = await refused(request("get", "/api/agents"));

  expect(refusal.status).toBe(0);
  expect(refusal.error).toBe("network_error");
});

test("a 200 that is not JSON is an ApiError, not data", async () => {
  answering(200, "<html>signed out</html>", "text/html");

  const refusal = await refused(request("get", "/api/agents"));

  expect(refusal.status).toBe(200);
  expect(refusal.error).toBe("unreadable_response");
});

test("a request is same-origin, and its query is in the URL", async () => {
  const fetch = answering(
    200,
    JSON.stringify({ items: [], next_cursor: null }),
  );

  await request("get", "/api/conversations", { query: { limit: 20 } });

  const { url, headers, init } = asked(fetch);
  expect(url).toBe("/api/conversations?limit=20");
  expect(init?.credentials).toBe("same-origin");
  // A read proves nothing about its type, and claiming JSON it is not sending
  // would be a lie the backend has no reason to believe.
  expect(headers["content-type"]).toBeUndefined();
});

test("a 204 gives back nothing, and the write says it is JSON", async () => {
  // The backend refuses a write that is not application/json whether or not
  // it carries a body, so a bodyless DELETE has to say so too.
  const fetch = answering(204, null);

  const nothing = await request(
    "delete",
    "/api/conversations/{conversation_id}",
    {
      path: { conversation_id: "c-1" },
    },
  );

  const { url, headers, init } = asked(fetch);
  expect(nothing).toBeUndefined();
  expect(url).toBe("/api/conversations/c-1");
  expect(init?.method).toBe("DELETE");
  expect(headers["content-type"]).toBe("application/json");
  expect(init?.body).toBeUndefined();
});

test("a bodyless POST says it is JSON as well", async () => {
  const fetch = answering(200, JSON.stringify({ cancelled: true }));

  await request(
    "post",
    "/api/conversations/{conversation_id}/runs/{run_id}/cancel",
    {
      path: { conversation_id: "c-1", run_id: "r-1" },
    },
  );

  const { url, headers, init } = asked(fetch);
  expect(url).toBe("/api/conversations/c-1/runs/r-1/cancel");
  expect(init?.method).toBe("POST");
  expect(headers["content-type"]).toBe("application/json");
  expect(init?.body).toBeUndefined();
});

test("a 202 is accepted and carries nothing to read", async () => {
  // A cancel whose run, on whichever server holds it, is still ending.
  answering(202, null);

  const nothing = await request(
    "post",
    "/api/conversations/{conversation_id}/runs/{run_id}/cancel",
    { path: { conversation_id: "c-1", run_id: "r-1" } },
  );

  expect(nothing).toBeUndefined();
});

test("a body is sent as JSON", async () => {
  const fetch = answering(200, JSON.stringify({ id: "c-1", title: "Renamed" }));

  await request("patch", "/api/conversations/{conversation_id}", {
    path: { conversation_id: "c-1" },
    body: { title: "Renamed" },
  });

  const { headers, init } = asked(fetch);
  expect(headers["content-type"]).toBe("application/json");
  expect(init?.body).toBe('{"title":"Renamed"}');
});

test("an abort before the answer is the caller's, not ours", async () => {
  const controller = new AbortController();
  controller.abort();
  const fetch = vi
    .fn<typeof globalThis.fetch>()
    .mockRejectedValue(controller.signal.reason as Error);
  vi.stubGlobal("fetch", fetch);

  const failure = await request("get", "/api/agents", {
    signal: controller.signal,
  }).catch((error: unknown) => error);

  expect(failure).toBe(controller.signal.reason);
  expect(failure).not.toBeInstanceOf(ApiError);
});

test("an abort while the body is read is the caller's too", async () => {
  // The headers arrive, then the connection goes: the body is where this
  // one lands, and it must not look like a server that answered rubbish.
  const controller = new AbortController();
  controller.abort();
  const answer = {
    ok: true,
    status: 200,
    json: () => Promise.reject(controller.signal.reason as Error),
  } as unknown as Response;
  vi.stubGlobal(
    "fetch",
    vi.fn<typeof globalThis.fetch>().mockResolvedValue(answer),
  );

  const failure = await request("get", "/api/agents", {
    signal: controller.signal,
  }).catch((error: unknown) => error);

  expect(failure).toBe(controller.signal.reason);
  expect(failure).not.toBeInstanceOf(ApiError);
});

test("a 401 tells whoever is listening, before the refusal is thrown", async () => {
  answering(401, JSON.stringify({ error: "not_signed_in", detail: "gone" }));
  const told: string[] = [];
  const stop = onUnauthorized(() => told.push("told"));

  const failure = await refused(request("get", "/api/agents"));

  expect(told).toEqual(["told"]);
  expect(failure.status).toBe(401);
  stop();
});

test("a listener that has unsubscribed is not told", async () => {
  answering(401, JSON.stringify({ error: "not_signed_in", detail: "gone" }));
  const told: string[] = [];
  onUnauthorized(() => told.push("told"))();

  await refused(request("get", "/api/agents"));

  expect(told).toEqual([]);
});

test("nothing but a 401 tells them", async () => {
  answering(403, JSON.stringify({ error: "not_yours", detail: "no" }));
  const told: string[] = [];
  const stop = onUnauthorized(() => told.push("told"));

  await refused(request("get", "/api/agents"));

  expect(told).toEqual([]);
  stop();
});

test("a listener that throws does not swallow the refusal", async () => {
  answering(401, JSON.stringify({ error: "not_signed_in", detail: "gone" }));
  const told: string[] = [];
  const stops = [
    onUnauthorized(() => {
      throw new Error("a listener's own bug");
    }),
    // Registered after the one that throws: it is still told, and the
    // caller still gets its ApiError.
    onUnauthorized(() => told.push("second")),
  ];

  const failure = await refused(request("get", "/api/agents"));

  expect(told).toEqual(["second"]);
  expect(failure.error).toBe("not_signed_in");
  for (const stop of stops) stop();
});

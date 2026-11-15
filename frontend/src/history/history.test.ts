// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import { act, renderHook, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import { callsTo, json, refusal, stubFetch, type Call } from "../test/api";
import { conversation, id, page } from "../test/conversations";
import { PAGE, useHistory } from "./history";

/** Let the event loop run once, so every settled promise has been seen. */
const settled = () => new Promise((done) => setTimeout(done, 0));

/** Answer `GET /robinauts/api/conversations` with these pages, in order. */
function pages(...answers: Response[]) {
  const left = [...answers];
  return stubFetch(() => left.shift() ?? json(page([])));
}

/** The hook, once its first page has arrived. */
async function history(fetch: ReturnType<typeof stubFetch>) {
  const { result } = renderHook(() => useHistory());
  await waitFor(() => {
    expect(result.current.status).not.toBe("loading");
  });
  return { result, fetch };
}

test("the first page is asked for with the count and no cursor", async () => {
  const fetch = pages(json(page([conversation(1)])));
  const { result } = await history(fetch);

  expect(callsTo(fetch, "/robinauts/api/conversations")).toEqual([
    { url: `/robinauts/api/conversations?limit=${PAGE}`, body: undefined },
  ]);
  expect(result.current.status).toBe("ready");
  expect(result.current.items).toHaveLength(1);
  expect(result.current.more).toBe(false);
});

test("a page with a cursor offers another, asked for with that cursor", async () => {
  const fetch = pages(
    json(page([conversation(1)], "cursor-1")),
    json(page([conversation(2)], "cursor-2")),
    json(page([conversation(3)])),
  );
  const { result } = await history(fetch);
  expect(result.current.more).toBe(true);

  await act(async () => {
    result.current.loadMore();
  });
  await waitFor(() => {
    expect(result.current.items).toHaveLength(2);
  });
  // Appended, not replaced, and in the order the pages arrived.
  expect(result.current.items.map((one) => one.id)).toEqual([id(1), id(2)]);
  expect(callsTo(fetch, "/robinauts/api/conversations")[1]?.url).toBe(
    `/robinauts/api/conversations?limit=${PAGE}&cursor=cursor-1`,
  );

  await act(async () => {
    result.current.loadMore();
  });
  await waitFor(() => {
    expect(result.current.items).toHaveLength(3);
  });
  // The last page hands back no cursor, so there is nothing more to ask for.
  expect(result.current.more).toBe(false);
});

test("a conversation seen on two pages is one conversation", async () => {
  // Paging walks a list that is changing: one written to while somebody is
  // paging moves in the order and can be handed out twice
  // (docs/specs/conversations.md, "Listing").
  const fetch = pages(
    json(page([conversation(1), conversation(2)], "cursor-1")),
    json(page([conversation(2), conversation(3)])),
  );
  const { result } = await history(fetch);
  await act(async () => {
    result.current.loadMore();
  });
  await waitFor(() => {
    expect(result.current.items).toHaveLength(3);
  });
  expect(result.current.items.map((one) => one.id)).toEqual([
    id(1),
    id(2),
    id(3),
  ]);
});

test("renaming sends the title and asks for the list again", async () => {
  const asked: Call[] = [];
  const fetch = stubFetch((call) => {
    asked.push(call);
    if (call.method === "PATCH") return json(conversation(1, "Named"));
    return json(page([conversation(1, "Named")]));
  });
  const { result } = await history(fetch);

  await act(async () => {
    await result.current.rename(id(1), "Named");
  });

  expect(
    asked.filter((call) => call.method === "PATCH").map((call) => call.body),
  ).toEqual([{ title: "Named" }]);
  expect(asked[1]?.url).toBe(`/robinauts/api/conversations/${id(1)}`);
  // Two listings: the first one, and the one after the write.
  expect(callsTo(fetch, "/robinauts/api/conversations")).toHaveLength(2);
});

test("deleting sends a DELETE and asks for the list again", async () => {
  const fetch = stubFetch((call) =>
    call.method === "DELETE" ? json(null, 204) : json(page([])),
  );
  const { result } = await history(fetch);

  await act(async () => {
    await result.current.remove(id(1));
  });

  expect(
    fetch.mock.calls.filter(([, init]) => init?.method === "DELETE"),
  ).toHaveLength(1);
  expect(callsTo(fetch, "/robinauts/api/conversations")).toHaveLength(2);
});

test("a write that is refused is thrown to whoever asked, and changes nothing", async () => {
  const fetch = stubFetch((call) =>
    call.method === "DELETE"
      ? refusal(409, "RunAlreadyActiveError", "run 1 is running")
      : json(page([conversation(1)])),
  );
  const { result } = await history(fetch);

  await expect(result.current.remove(id(1))).rejects.toMatchObject({
    status: 409,
  });
  // The row is still there, and the list was not asked for again.
  expect(result.current.items).toHaveLength(1);
  expect(callsTo(fetch, "/robinauts/api/conversations")).toHaveLength(1);
});

test("a first page that is refused leaves the list failed, and it can be asked again", async () => {
  const fetch = pages(
    refusal(500, "InternalError", "the request could not be served"),
    json(page([conversation(1)])),
  );
  const { result } = await history(fetch);

  expect(result.current.status).toBe("failed");
  expect(result.current.error).toBe(
    "The conversations could not be loaded: the request could not be served",
  );

  await act(async () => {
    result.current.refresh();
  });
  await waitFor(() => {
    expect(result.current.status).toBe("ready");
  });
  expect(result.current.error).toBeNull();
  expect(result.current.items).toHaveLength(1);
});

test("a page after the first that is refused keeps the pages that arrived", async () => {
  const fetch = pages(
    json(page([conversation(1)], "cursor-1")),
    refusal(422, "InvalidCursorError", "cursor: not a cursor of ours"),
  );
  const { result } = await history(fetch);

  await act(async () => {
    result.current.loadMore();
  });
  await waitFor(() => {
    expect(result.current.error).not.toBeNull();
  });
  expect(result.current.status).toBe("ready");
  expect(result.current.items).toHaveLength(1);
  expect(result.current.loadingMore).toBe(false);
});

test("a page that arrives after a refresh started is not spliced into it", async () => {
  // The answer to "load more" belongs to a listing that no longer exists.
  let slow: ((answer: Response) => void) | null = null;
  const fetch = vi.fn<typeof globalThis.fetch>((input) => {
    const url = String(input);
    if (url.includes("cursor=cursor-1")) {
      return new Promise<Response>((settle) => {
        slow = settle;
      });
    }
    return Promise.resolve(json(page([conversation(1)], "cursor-1")));
  });
  vi.stubGlobal("fetch", fetch);
  const { result } = await history(fetch as ReturnType<typeof stubFetch>);

  await act(async () => {
    result.current.loadMore();
  });
  await act(async () => {
    result.current.refresh();
  });
  await waitFor(() => {
    expect(result.current.loadingMore).toBe(false);
  });

  await act(async () => {
    slow?.(json(page([conversation(9)])));
    await Promise.resolve();
  });

  expect(result.current.items.map((one) => one.id)).toEqual([id(1)]);
});

test("a refresh wins over a page asked for while it was in flight", async () => {
  // Deleting is followed by a fresh first page. A "load more" started during
  // that round trip would append to the list the delete has just left
  // behind, and the deleted row would come back.
  let slowRefresh: ((answer: Response) => void) | null = null;
  let listings = 0;
  const fetch = vi.fn<typeof globalThis.fetch>((input, init) => {
    if (init?.method === "DELETE") return Promise.resolve(json(null, 204));
    if (String(input).includes("cursor=")) {
      return Promise.resolve(json(page([conversation(3)])));
    }
    listings += 1;
    if (listings === 1) {
      return Promise.resolve(
        json(page([conversation(1), conversation(2)], "cursor-1")),
      );
    }
    // The listing that follows the delete: held until this test lets it go.
    return new Promise<Response>((settle) => {
      slowRefresh = settle;
    });
  });
  vi.stubGlobal("fetch", fetch);
  const { result } = await history(fetch as ReturnType<typeof stubFetch>);
  expect(result.current.more).toBe(true);

  let deleting: Promise<void> | null = null;
  await act(async () => {
    deleting = result.current.remove(id(1));
    await settled();
  });
  expect(result.current.refreshing).toBe(true);

  // Asked for in the middle of it, and refused: the cursor belongs to a
  // listing that is being thrown away.
  await act(async () => {
    result.current.loadMore();
    await settled();
  });
  expect(
    callsTo(fetch, "/robinauts/api/conversations").some((call) =>
      call.url.includes("cursor="),
    ),
  ).toBe(false);

  await act(async () => {
    slowRefresh?.(json(page([conversation(2)])));
    await deleting;
  });

  expect(result.current.items.map((one) => one.id)).toEqual([id(2)]);
  expect(result.current.refreshing).toBe(false);
});

// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import { act, renderHook } from "@testing-library/react";
import { expect, test } from "vitest";

import {
  DEVICES,
  FLOWS,
  formatRoute,
  NEW_RUN,
  replaceRoute,
  type Route,
  isConversationId,
  LINK,
  navigate,
  NEW_CHAT,
  parseRoute,
  useRoute,
} from "./router";

const ID = "3f1b8e2a-4c5d-4e6f-8a9b-0c1d2e3f4a5b";

/** Let the event loop run once, which is what a queued event waits for. */
const settled = () => new Promise((done) => setTimeout(done, 0));

test("the two routes are read out of the hash", () => {
  expect(parseRoute("#/")).toEqual({ kind: "new" });
  expect(parseRoute(`#/c/${ID}`)).toEqual({ kind: "conversation", id: ID });
});

test("and written back the same way", () => {
  expect(formatRoute(NEW_CHAT)).toBe("#/");
  expect(formatRoute({ kind: "conversation", id: ID })).toBe(`#/c/${ID}`);
  // Round trips, which is what makes a link's href and the hash it produces
  // one thing rather than two spellings that have to be kept in step.
  expect(parseRoute(formatRoute({ kind: "conversation", id: ID }))).toEqual({
    kind: "conversation",
    id: ID,
  });
});

test("the devices page and the link page are read out of the hash", () => {
  expect(parseRoute("#/devices")).toEqual({ kind: "devices" });
  expect(parseRoute("#/link")).toEqual({ kind: "link" });
});

test("and written back the same way, round trip included", () => {
  expect(formatRoute(DEVICES)).toBe("#/devices");
  expect(formatRoute(LINK)).toBe("#/link");
  expect(parseRoute(formatRoute(DEVICES))).toEqual(DEVICES);
  expect(parseRoute(formatRoute(LINK))).toEqual(LINK);
});

test("the orchestrator's pages are read out of the hash", () => {
  expect(parseRoute("#/flows")).toEqual({
    kind: "flows",
    view: { at: "runs" },
  });
  expect(parseRoute("#/flows/new")).toEqual(NEW_RUN);
  expect(parseRoute("#/flows/new/code_review")).toEqual({
    kind: "flows",
    view: { at: "start", flow: "code_review" },
  });
  expect(parseRoute(`#/flows/runs/${ID}`)).toEqual({
    kind: "flows",
    view: { at: "run", run: ID },
  });
  expect(parseRoute("#/flows/flow/guess_number")).toEqual({
    kind: "flows",
    view: { at: "flow", flow: "guess_number" },
  });
});

test("the orchestrator's pages are written back the same way, round trip included", () => {
  const routes: Route[] = [
    FLOWS,
    NEW_RUN,
    { kind: "flows", view: { at: "run", run: ID } },
    { kind: "flows", view: { at: "flow", flow: "guess_number" } },
    { kind: "flows", view: { at: "start", flow: "guess_number" } },
  ];
  expect(routes.map(formatRoute)).toEqual([
    "#/flows",
    "#/flows/new",
    `#/flows/runs/${ID}`,
    "#/flows/flow/guess_number",
    "#/flows/new/guess_number",
  ]);
  for (const route of routes) {
    expect(parseRoute(formatRoute(route))).toEqual(route);
  }
  // A run id or a flow name that is not one of ours is the runs, not a path.
  expect(
    formatRoute({ kind: "flows", view: { at: "run", run: "../elsewhere" } }),
  ).toBe("#/flows");
  expect(
    formatRoute({ kind: "flows", view: { at: "start", flow: "a/b" } }),
  ).toBe("#/flows/new");
  expect(
    formatRoute({ kind: "flows", view: { at: "flow", flow: "a/b" } }),
  ).toBe("#/flows");
});

test("replacing the route moves it in place and is heard", async () => {
  const { result } = renderHook(() => useRoute());
  const before = history.length;
  await act(async () => {
    replaceRoute(NEW_RUN);
    await settled();
  });
  expect(location.hash).toBe("#/flows/new");
  expect(result.current).toEqual(NEW_RUN);
  expect(history.length).toBe(before);
});

test("a pairing code's query is cut off, and the route is the link page", () => {
  // The URL inside a pairing QR: the code is the app's, never this page's.
  expect(parseRoute("#/link?v=1&c=abc123")).toEqual({ kind: "link" });
});

test("reading the link page's hash leaves the address bar as it was", () => {
  location.hash = "#/link?v=1&c=abc123";
  renderHook(() => useRoute());
  expect(location.hash).toBe("#/link?v=1&c=abc123");
});

test("every other hash is the empty chat", () => {
  for (const hash of [
    "",
    "#",
    "#/",
    "#/nowhere",
    "#/c",
    "#/c/",
    `#/c/${ID}/extra`,
    `#c/${ID}`,
    "#/devices/",
    "#/devices/x",
    "#/link/x",
    "#/flows/",
    "#/flows/runs",
    "#/flows/runs/",
    `#/flows/runs/${ID}/x`,
    "#/flows/runs/nope",
    `#/flows/run/${ID}`,
    "#/flows/new/1x",
    "#/flows/new/",
    "#/flows/flow/",
    "#/flows/flow/1abc",
    "#/flows/flow/a-b",
    "#/flows/guess_number",
    "#/Devices",
    // The sign-in page's own hash, which it reads for itself: this must not
    // claim it, and reading it writes nothing.
    "#/sign-in?error=not_allowed",
  ]) {
    expect(parseRoute(hash), hash).toEqual({ kind: "new" });
  }
});

test("an id that is not one of ours is not a conversation", () => {
  expect(isConversationId(ID)).toBe(true);
  expect(isConversationId(ID.toUpperCase())).toBe(true);
  for (const bad of [
    "",
    "nope",
    `${ID}x`,
    `${ID} `,
    "3f1b8e2a4c5d4e6f8a9b0c1d2e3f4a5b",
    // The shapes that would forge a route or a request path if they were
    // ever pasted into one.
    `${ID}/../agents`,
    `${ID}#/`,
    "%2e%2e",
  ]) {
    expect(isConversationId(bad), bad).toBe(false);
    expect(parseRoute(`#/c/${bad}`), bad).toEqual({ kind: "new" });
  }
});

test("a route carrying an id that is not one of ours is written as home", () => {
  // It cannot come from `parseRoute` and cannot come from the API; what it
  // would be is a bug, and a link home is better than a hash somebody else
  // composed.
  expect(formatRoute({ kind: "conversation", id: "../elsewhere" })).toBe("#/");
});

test("the hash of a conversation's route is what navigating writes", () => {
  navigate({ kind: "conversation", id: ID });
  expect(location.hash).toBe(`#/c/${ID}`);
  navigate(NEW_CHAT);
  expect(location.hash).toBe("#/");
});

test("useRoute follows the hash as it changes", async () => {
  const { result } = renderHook(() => useRoute());
  expect(result.current).toEqual({ kind: "new" });

  await act(async () => {
    navigate({ kind: "conversation", id: ID });
    // A `hashchange` is queued as a task, not a microtask: jsdom announces
    // it on the next turn of the loop, as a browser does.
    await settled();
  });
  expect(result.current).toEqual({ kind: "conversation", id: ID });

  await act(async () => {
    navigate(NEW_CHAT);
    await settled();
  });
  expect(result.current).toEqual({ kind: "new" });
});

test("reading the same hash twice gives the same object", () => {
  // `useSyncExternalStore` compares snapshots by identity; a fresh object
  // every time would be a render that never settles.
  const { result, rerender } = renderHook(() => useRoute());
  const first = result.current;
  rerender();
  expect(result.current).toBe(first);
});

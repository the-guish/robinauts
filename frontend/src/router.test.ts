// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import { expect, test } from "vitest";

import { formatRoute, isConversationId, NEW_CHAT, parseRoute } from "./router";

const ID = "3f1b8e2a-4c5d-4e6f-8a9b-0c1d2e3f4a5b";

test("the two routes are read out of the shell's route", () => {
  expect(parseRoute("")).toEqual({ kind: "new" });
  expect(parseRoute(`/c/${ID}`)).toEqual({ kind: "conversation", id: ID });
});

test("and written back the same way, round trip included", () => {
  expect(formatRoute(NEW_CHAT)).toBe("");
  expect(formatRoute({ kind: "conversation", id: ID })).toBe(`/c/${ID}`);
  expect(parseRoute(formatRoute({ kind: "conversation", id: ID }))).toEqual({
    kind: "conversation",
    id: ID,
  });
});

test("the search page has a route of its own", () => {
  expect(parseRoute("/search")).toEqual({ kind: "search" });
  expect(formatRoute({ kind: "search" })).toBe("/search");
  expect(parseRoute("/search/x")).toEqual({ kind: "new" });
});

test("every other route is the empty chat", () => {
  for (const route of [
    "",
    "/",
    "?x=1",
    "/nowhere",
    "/c",
    "/c/",
    `/c/${ID}/extra`,
    `c/${ID}`,
    "/devices",
    "/link?v=1&c=abc",
    "/flows",
  ]) {
    expect(parseRoute(route), route).toEqual({ kind: "new" });
  }
});

test("a query is cut off", () => {
  expect(parseRoute(`/c/${ID}?x=1`)).toEqual({ kind: "conversation", id: ID });
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
    expect(parseRoute(`/c/${bad}`), bad).toEqual({ kind: "new" });
  }
});

test("a route carrying an id that is not one of ours is written as home", () => {
  // It cannot come from `parseRoute` and cannot come from the API; what it
  // would be is a bug, and a link home is better than a route somebody else
  // composed.
  expect(formatRoute({ kind: "conversation", id: "../elsewhere" })).toBe("");
});

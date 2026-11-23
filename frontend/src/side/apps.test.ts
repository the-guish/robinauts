// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import { expect, test } from "vitest";

import type { ThreadToolCall } from "../chat";
import {
  type App,
  appOf,
  appOffers,
  canonical,
  lastAsked,
  subTabKey,
  subTabsOf,
  viewUrl,
} from "./apps";

const VIEW = "ui://diagrams/view.html";
const APP: App = {
  id: `my server/${VIEW}`,
  server: "my server",
  view: VIEW,
  title: "Diagrams",
  tools: [
    { name: "show_diagram", title: "Diagram" },
    { name: "list_diagrams", title: "List" },
  ],
};

function call(
  callId: string,
  name: string,
  args: Record<string, unknown> | null,
  more: Partial<ThreadToolCall> = {},
): ThreadToolCall {
  return {
    messageId: "m1",
    callId,
    name,
    arguments: args,
    state: "answered",
    live: false,
    ...more,
  };
}

test("an app is a tab of kind app, keyed by its server and its view", () => {
  const [offer] = appOffers([APP]);
  expect(offer).toEqual({
    key: `app:my server/${VIEW}`,
    title: "Diagrams",
    kind: "app",
  });
  expect(offer === undefined ? undefined : appOf([APP], offer)).toBe(APP);
  expect(
    appOf([APP], { key: `app:other/${VIEW}`, title: "", kind: "app" }),
  ).toBeUndefined();
});

test("the view's address spells each part as a part", () => {
  expect(viewUrl("a&b", "my server", "show todos")).toBe(
    "/robinauts/api/apps/my%20server/show%20todos/view?agent=a%26b",
  );
});

test.each<[unknown, string]>([
  [{ b: 1, a: [true, null, "x"] }, '{"a":[true,null,"x"],"b":1}'],
  [{ a: { d: 2, c: 1 } }, '{"a":{"c":1,"d":2}}'],
  [{}, "{}"],
  ["text", '"text"'],
  [undefined, "null"],
  // Half a character, as a model may write one: escaped, in a value and in a key.
  [{ "\ud800": ["a\udc00"] }, '{"\\ud800":["a\\udc00"]}'],
])("%j is the same text whatever order its keys came in", (value, text) => {
  expect(canonical(value)).toBe(text);
  // An address or an element's id can carry it: nothing in it is half a character.
  expect(() => encodeURIComponent(canonical(value))).not.toThrow();
});

test("a sub-tab is one tool with its arguments, numbered per tool, in the order first called", () => {
  const calls = [
    call("c1", "show_diagram", { diagram_id: "a" }),
    call("c2", "draw_diagram", { nodes: [] }),
    call("c3", "show_diagram", { diagram_id: "b" }),
    call("c4", "list_diagrams", {}),
    // Not whole yet: left out until its arguments are.
    call("c5", "show_diagram", null, { state: "running" }),
    // The first diagram again, by a later answer: its sub-tab, its newest call.
    call("c6", "show_diagram", { diagram_id: "a" }, { messageId: "m2" }),
  ];

  expect(subTabsOf(APP, calls)).toEqual([
    {
      key: subTabKey("show_diagram", { diagram_id: "a" }),
      title: "Diagram 1",
      tool: "show_diagram",
      arguments: { diagram_id: "a" },
      call: { messageId: "m2", callId: "c6", state: "answered" },
      last: 5,
    },
    {
      key: subTabKey("show_diagram", { diagram_id: "b" }),
      title: "Diagram 2",
      tool: "show_diagram",
      arguments: { diagram_id: "b" },
      call: { messageId: "m1", callId: "c3", state: "answered" },
      last: 2,
    },
    {
      key: subTabKey("list_diagrams", {}),
      title: "List 1",
      tool: "list_diagrams",
      arguments: {},
      call: { messageId: "m1", callId: "c4", state: "answered" },
      last: 3,
    },
  ]);
  expect(subTabsOf(APP, [])).toEqual([]);
});

test("a later call with no result leaves its sub-tab the earlier call and its result", () => {
  const args = { diagram_id: "a" };
  const answered = call("c1", "show_diagram", args);
  const failed = call("c2", "show_diagram", args, {
    messageId: "m2",
    state: "unanswered",
  });
  const running = call("c3", "show_diagram", args, {
    messageId: "m3",
    state: "running",
  });
  const shown = (calls: ThreadToolCall[]) =>
    subTabsOf(APP, calls).map((sub) => [sub.call.callId, sub.last]);

  // Its turn ended before the tool answered: there is nothing new to show.
  expect(shown([answered, failed])).toEqual([["c1", 0]]);
  // A call still being made may yet answer, so the sub-tab is its own.
  expect(shown([answered, failed, running])).toEqual([["c3", 2]]);
  // With no earlier result to keep, the call with none is all there is.
  expect(shown([failed])).toEqual([["c2", 0]]);
  expect(shown([failed, answered])).toEqual([["c1", 1]]);
});

test("two tools with one title are numbered together, so no two sub-tabs share a name", () => {
  const app: App = {
    ...APP,
    tools: [
      { name: "show_diagram", title: "Diagram" },
      { name: "show_chart", title: "Diagram" },
    ],
  };
  const calls = [
    call("c1", "show_diagram", { diagram_id: "a" }),
    call("c2", "show_chart", { chart_id: "b" }),
    call("c3", "show_diagram", { diagram_id: "c" }),
  ];
  expect(subTabsOf(app, calls).map((sub) => sub.title)).toEqual([
    "Diagram 1",
    "Diagram 2",
    "Diagram 3",
  ]);
});

test("the agent is asking for the view of its newest call being made now", () => {
  const other: App = {
    ...APP,
    id: "second/ui://x",
    server: "second",
    view: "ui://x",
  };
  const stored = call("c1", "show_diagram", { diagram_id: "a" });
  const live = call("c2", "show_diagram", { diagram_id: "b" }, { live: true });
  const plain = call("c3", "draw_diagram", {}, { live: true });
  const unfinished = call("c4", "show_diagram", null, { live: true });

  // The first server that has a tool of that name is the one.
  expect(lastAsked([APP, other], [stored, live, plain, unfinished])).toEqual({
    tab: `app:my server/${VIEW}`,
    sub: subTabKey("show_diagram", { diagram_id: "b" }),
    callId: "m1/c2",
  });
  // Read from the store, a call asks for nothing: the person chose what is open.
  expect(lastAsked([APP], [stored])).toBeNull();
  expect(lastAsked([APP], [plain])).toBeNull();
  expect(lastAsked([], [live])).toBeNull();
});

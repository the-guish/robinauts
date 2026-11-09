// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import { expect, test } from "vitest";

import type { FlowsView } from "../router";
import {
  apiOf,
  flowsOf,
  framedHash,
  frameSrc,
  lastSequence,
  runsOf,
  sameView,
  viewOfFramed,
} from "./orchestrator";

const RUN = "11111111-2222-4333-8444-555555555555";

const VIEWS: [FlowsView, string][] = [
  [{ at: "runs" }, "#/runs"],
  [{ at: "new" }, "#/flows"],
  [{ at: "run", run: RUN }, `#/runs/${RUN}`],
  [{ at: "flow", flow: "guess_number" }, "#/flows/guess_number"],
];

test.each(VIEWS)("%j is the frame's %s, and back", (view, hash) => {
  expect(framedHash(view)).toBe(hash);
  expect(viewOfFramed(hash)).toEqual(view);
});

test.each([
  ["", { at: "runs" }],
  ["#/", { at: "runs" }],
  ["#/runs?flow=guess_number&status=active", { at: "runs" }],
  [`#/runs/${RUN}?view=graph`, { at: "run", run: RUN }],
  ["#/flows/guess_number?version=0.3.0", { at: "flow", flow: "guess_number" }],
])("the frame's query stays its own: %s", (hash, view) => {
  expect(viewOfFramed(hash)).toEqual(view);
});

test.each([
  "#/sign-in",
  "#/runs/nope",
  "#/flows/a-b",
  `#/runs/${RUN}/x`,
  "#/elsewhere",
])(
  "a page of the frame's with no route of the shell's is left alone: %s",
  (hash) => {
    expect(viewOfFramed(hash)).toBeNull();
  },
);

test("a view that is not ours is framed as the runs", () => {
  expect(framedHash({ at: "run", run: "../x" })).toBe("#/runs");
  expect(framedHash({ at: "flow", flow: "a/b" })).toBe("#/flows");
});

test("the frame opens embedded, at the view", () => {
  expect(frameSrc("/neorc/ui/", { at: "run", run: RUN })).toBe(
    `/neorc/ui/?embedded#/runs/${RUN}`,
  );
});

test("two views are the same when they frame the same page", () => {
  expect(sameView({ at: "runs" }, { at: "runs" })).toBe(true);
  expect(sameView({ at: "runs" }, { at: "new" })).toBe(false);
});

test.each([
  ["/neorc/ui/", "/neorc/"],
  ["/neorc/ui", "/neorc/"],
  ["/a/b/ui/", "/a/b/"],
])("the API of %s is one level above it: %s", (url, api) => {
  expect(apiOf(url)).toBe(api);
});

test("only the fields that fit are believed", () => {
  expect(
    flowsOf({ flows: [{ name: "guess_number" }, { name: "a/b" }, {}, 3] }),
  ).toEqual(["guess_number"]);
  expect(flowsOf(null)).toEqual([]);
  expect(
    runsOf({
      runs: [
        { id: RUN, flow: "guess_number", status: "failed" },
        { id: "x", flow: "guess_number", status: "failed" },
        { id: RUN, flow: "guess_number", status: "odd" },
      ],
    }),
  ).toEqual([{ id: RUN, flow: "guess_number", status: "failed" }]);
  expect(runsOf({ runs: "no" })).toEqual([]);
});

test("the last sequence of the news, or where it was", () => {
  expect(lastSequence({ events: [{ sequence: 4 }, { sequence: 7 }] }, 3)).toBe(
    7,
  );
  expect(lastSequence({ events: [] }, 3)).toBe(3);
  expect(lastSequence({ events: [{ sequence: "9" }] }, 3)).toBe(3);
});

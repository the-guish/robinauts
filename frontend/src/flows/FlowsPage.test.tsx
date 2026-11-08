// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import { expect, test } from "vitest";

import { flowsFrameUrl } from "./FlowsPage";

const RUN = "11111111-2222-4333-8444-555555555555";

test("the frame opens neorc embedded, at its runs or at one run", () => {
  expect(flowsFrameUrl("http://127.0.0.1:8420/ui/", null)).toBe(
    "http://127.0.0.1:8420/ui/?embed=1#/runs",
  );
  expect(flowsFrameUrl("http://127.0.0.1:8420/ui/", RUN)).toBe(
    `http://127.0.0.1:8420/ui/?embed=1#/runs/${RUN}`,
  );
  // Whatever query and hash the address came with, the frame's are these.
  expect(flowsFrameUrl("https://neorc.test/ui/?embed=0#/flows", null)).toBe(
    "https://neorc.test/ui/?embed=1#/runs",
  );
});

test("an address that is not http or https frames nothing", () => {
  for (const url of ["javascript:alert(1)", "data:text/html,x", "/ui/", ""]) {
    expect(flowsFrameUrl(url, null)).toBeNull();
  }
});

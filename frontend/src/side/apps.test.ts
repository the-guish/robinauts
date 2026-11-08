// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import { expect, test } from "vitest";

import { type App, appOf, appOffers, viewUrl } from "./apps";

const APP: App = {
  id: "my server/show todos",
  server: "my server",
  tool: "show todos",
  title: "To-do list",
  description: "",
};

test("an app is a tab of kind app, keyed by its server and its tool", () => {
  const [offer] = appOffers([APP]);
  expect(offer).toEqual({
    key: "app:my server/show todos",
    title: "To-do list",
    kind: "app",
  });
  expect(offer === undefined ? undefined : appOf([APP], offer)).toBe(APP);
  expect(
    appOf([APP], { key: "app:other/show todos", title: "", kind: "app" }),
  ).toBeUndefined();
});

test("the view's address spells each part as a part", () => {
  expect(viewUrl("a&b", APP)).toBe(
    "/api/apps/my%20server/show%20todos/view?agent=a%26b",
  );
});

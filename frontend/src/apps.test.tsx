// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import { resetSession, type AppProps } from "@oneberth/ui";
import { act, render, screen, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import { App } from "./App";
import { json, stubFetch } from "./test/api";
import { page } from "./test/conversations";

// A build's own `src/extra/`, with one application (`./extra/index.ts`).
vi.mock("./extra", () => ({
  EXTRA_APPS: [
    {
      name: "reviews",
      title: "Reviews",
      icon: null,
      load: () =>
        Promise.resolve({
          default: ({ name }: AppProps) => <main>{`the page of ${name}`}</main>,
        }),
    },
  ],
}));

const SESSION = {
  mode: "local",
  providers: [],
  principal: { kind: "user", id: "u-1", name: "Ada", email: null },
  roles: ["robinauts/user", "neorc/user"],
  pairing: null,
};

afterEach(resetSession);

test("an extra application is on the rail below neorc, and opens in the area", async () => {
  location.hash = "#/reviews";
  stubFetch((call) => {
    if (call.url === "/auth/session") return json(SESSION);
    return json(page([]));
  });
  render(<App />);
  await act(async () => {
    await Promise.resolve();
  });
  const rail = await screen.findByRole("navigation", { name: "Applications" });
  const links = within(rail).getAllByRole("link");
  expect(links.map((link) => link.textContent)).toEqual([
    "Robinauts",
    "neorc",
    "Reviews",
  ]);
  expect(links[2]).toHaveAttribute("aria-current", "page");
  expect(await screen.findByText("the page of reviews")).toBeVisible();
});

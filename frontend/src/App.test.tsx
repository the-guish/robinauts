// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import { resetSession } from "@oneberth/ui";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";

import { App } from "./App";
import { json, stubFetch } from "./test/api";
import { page } from "./test/conversations";

const ADA = {
  kind: "user",
  id: "u-1",
  name: "Ada",
  email: "ada@x.test",
  provider: "google",
};

/** oneberth's session, as `GET /auth/session` answers it. */
function session(principal: unknown) {
  return {
    mode: "providers",
    providers: [{ id: "google", title: "Google" }],
    principal,
    roles: principal === null ? [] : ["robinauts/user", "neorc/user"],
    pairing: null,
  };
}

// The session store is oneberth's module state: each test's render asks again.
afterEach(resetSession);

/** Answers the session with this, and Robinauts' own reads with nothing. */
async function app(principal: unknown) {
  const fetch = stubFetch((call) => {
    if (call.url === "/auth/session") return json(session(principal));
    if (call.url.startsWith("/robinauts/api/agents")) {
      return json({ items: [] });
    }
    if (call.url.startsWith("/robinauts/api/models")) {
      return json({ items: [] });
    }
    if (call.url.startsWith("/robinauts/api/apps")) return json({ apps: [] });
    return json(page([]));
  });
  render(<App />);
  await act(async () => {
    await Promise.resolve();
  });
  return fetch;
}

test("nobody signed in: the shell's sign-in page in place of every page", async () => {
  await app(null);
  expect(
    screen.getByRole("link", { name: /Sign in with Google/ }),
  ).toBeVisible();
  expect(screen.queryByRole("complementary", { name: "Panel" })).toBeNull();
});

test("a pairing code opened in a browser: the link page, signed out", async () => {
  location.hash = "#/link?v=1&c=abc123";
  await app(null);
  expect(
    screen.getByText(
      "This code is for the Robinauts app on a phone. Open the app and scan it there.",
    ),
  ).toBeVisible();
  expect(screen.queryByRole("link", { name: /Sign in with/ })).toBeNull();
  // It reads no parameter, and leaves the hash as it found it.
  expect(location.hash).toBe("#/link?v=1&c=abc123");
});

test("the link page is the same for somebody signed in", async () => {
  location.hash = "#/link";
  await app(ADA);
  expect(screen.getByText(/This code is for the Robinauts app/)).toBeVisible();
  expect(screen.queryByRole("navigation", { name: "Applications" })).toBeNull();
});

test("signed in: the rail of two applications, and Robinauts first", async () => {
  await app(ADA);
  const rail = await screen.findByRole("navigation", { name: "Applications" });
  const links = within(rail).getAllByRole("link");
  expect(links.map((link) => link.textContent)).toEqual(["Robinauts", "neorc"]);
  expect(within(rail).getByRole("button", { name: "Account" })).toBeVisible();
  // Robinauts is the application opened first, on its empty chat.
  await waitFor(() => {
    expect(location.hash).toBe("#/robinauts");
  });
  expect(
    await screen.findByRole("complementary", { name: "Panel" }),
  ).toBeVisible();
  expect(
    await screen.findByRole("heading", { level: 1, name: "New chat" }),
  ).toBeVisible();
});

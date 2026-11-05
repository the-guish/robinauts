// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import { act, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import { App } from "./App";

/** Answers `/auth/session` with this, and `/api/agents` with one agent. */
function answering(session: unknown, status = 200) {
  const fetch = vi
    .fn<typeof globalThis.fetch>()
    .mockImplementation((input: RequestInfo | URL) => {
      const path = String(input);
      const body =
        path === "/auth/session"
          ? JSON.stringify(session)
          : JSON.stringify({
              items: [{ id: "helper", title: "Helper", engine: "langgraph" }],
            });
      return Promise.resolve(
        new Response(body, {
          status: path === "/auth/session" ? status : 200,
          headers: { "content-type": "application/json" },
        }),
      );
    });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

/** Render, and let both calls arrive. */
async function app() {
  render(<App />);
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

test("nobody signed in: the sign-in page in place of every page", async () => {
  answering({
    sign_in: true,
    local_development: false,
    providers: [{ id: "google", title: "Google" }],
    user: null,
  });
  await app();
  expect(
    screen.getByRole("heading", { name: "Sign in to Robinauts" }),
  ).toBeVisible();
  expect(screen.queryByRole("complementary", { name: "Panel" })).toBeNull();
});

test("a pairing code opened in a browser: the link page, signed out", async () => {
  location.hash = "#/link?v=1&c=abc123";
  answering({
    sign_in: true,
    local_development: false,
    providers: [{ id: "google", title: "Google" }],
    user: null,
  });
  await app();
  expect(
    screen.getByText(
      "This code is for the Robinauts app on a phone. Open the app and scan it there.",
    ),
  ).toBeVisible();
  expect(
    screen.queryByRole("heading", { name: "Sign in to Robinauts" }),
  ).toBeNull();
  // It reads no parameter, and leaves the hash as it found it.
  expect(location.hash).toBe("#/link?v=1&c=abc123");
});

test("the link page is the same for somebody signed in", async () => {
  location.hash = "#/link";
  answering({
    sign_in: true,
    local_development: false,
    providers: [{ id: "google", title: "Google" }],
    user: { id: "u-1", provider: "google", name: "Ada", email: "ada@x.test" },
  });
  await app();
  expect(screen.getByText(/This code is for the Robinauts app/)).toBeVisible();
  expect(screen.queryByRole("complementary", { name: "Panel" })).toBeNull();
});

test("signed in: the shell, with the panel and the empty chat", async () => {
  answering({
    sign_in: true,
    local_development: false,
    providers: [{ id: "google", title: "Google" }],
    user: { id: "u-1", provider: "google", name: "Ada", email: "ada@x.test" },
  });
  await app();
  expect(screen.getByRole("complementary", { name: "Panel" })).toBeVisible();
  expect(screen.getByRole("heading", { name: "New chat" })).toBeVisible();
  expect(screen.queryByRole("note")).toBeNull();
});

test("the local development mode: the shell, and the banner", async () => {
  answering({
    sign_in: false,
    local_development: true,
    providers: [],
    user: { id: "u-0", provider: "!local", name: "Local developer" },
  });
  await app();
  expect(screen.getByRole("complementary", { name: "Panel" })).toBeVisible();
  expect(screen.getByRole("note")).toHaveTextContent(/Local development mode/);
});

test("a session that cannot be loaded offers a way to ask again", async () => {
  answering({ error: "broken", detail: "the database is away" }, 500);
  await app();
  expect(screen.getByRole("alert")).toHaveTextContent("the database is away");
  expect(screen.getByRole("button", { name: "Try again" })).toBeVisible();
});

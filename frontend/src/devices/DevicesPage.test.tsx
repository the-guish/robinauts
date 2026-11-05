// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { expect, test, vi } from "vitest";

import type { Session } from "../session/session";
import { callsTo, json, refusal, stubFetch, type Call } from "../test/api";
import { DevicesPage, NO_PHONES, NO_TOKENS, SHOWN_ONCE } from "./DevicesPage";

const SESSION: Session = {
  sign_in: true,
  local_development: false,
  providers: [{ id: "google", title: "Google" }],
  user: { id: "u-1", provider: "google", name: "Ada", email: "ada@x.test" },
};

const PHONE = {
  id: "d0000000-0000-4000-8000-000000000001",
  name: "Pixel 8",
  platform: "android",
  created_at: "2026-10-01T09:00:00Z",
  last_used_at: "2026-11-03T18:30:00Z",
  expires_at: "2026-10-31T09:00:00Z",
};

const TOKEN = {
  id: "e0000000-0000-4000-8000-000000000001",
  name: "nightly export",
  created_at: "2026-09-10T08:00:00Z",
  expires_at: "2026-12-09T08:00:00Z",
};

const SECRET = "rbt_0123456789abcdef";

/**
 * One phone and one token, until a write takes them away; a test answers
 * what it is about itself and falls through to these for the rest.
 */
function answering(
  answer: (call: Call) => Response | undefined = () => undefined,
) {
  let devices = [PHONE];
  let tokens = [TOKEN];
  return stubFetch((call) => {
    const given = answer(call);
    if (given !== undefined) return given;
    if (call.url === "/auth/devices") return json({ items: devices });
    if (call.url === "/auth/tokens" && call.method === "GET") {
      return json({ items: tokens });
    }
    if (call.url === `/auth/devices/${PHONE.id}`) {
      devices = [];
      return json(null, 204);
    }
    if (call.url === `/auth/tokens/${TOKEN.id}`) {
      tokens = [];
      return json(null, 204);
    }
    return refusal(404, "NotFound", "no such route here");
  });
}

async function page(
  answer?: (call: Call) => Response | undefined,
): Promise<ReturnType<typeof answering>> {
  const fetch = answering(answer);
  render(<DevicesPage session={SESSION} />);
  await waitFor(() => {
    expect(screen.queryByText(/^Loading/)).toBeNull();
  });
  return fetch;
}

const phones = () => screen.getByRole("list", { name: "Phones" });
const tokenList = () => screen.getByRole("list", { name: "API tokens" });

test("the page is named, and lists the phones with their dates", async () => {
  await page();
  expect(
    screen.getByRole("heading", { level: 1, name: "Linked devices" }),
  ).toBeVisible();
  const row = within(phones()).getByRole("listitem");
  expect(row).toHaveTextContent("Pixel 8");
  expect(row).toHaveTextContent("Android");
  for (const label of ["Linked on", "Last used", "Expires"]) {
    expect(within(row).getByText(label)).toBeVisible();
  }
  // In the browser's locale, with the instant itself on the element.
  const linked = row.querySelector(`time[datetime="${PHONE.created_at}"]`);
  expect(linked).toHaveAttribute("title", PHONE.created_at);
  expect(linked?.textContent).not.toBe(PHONE.created_at);
});

test("and the API tokens, below them", async () => {
  await page();
  const row = within(tokenList()).getByRole("listitem");
  expect(row).toHaveTextContent("nightly export");
  expect(within(row).getByText("Created")).toBeVisible();
  expect(
    row.querySelector(`time[datetime="${TOKEN.expires_at}"]`),
  ).not.toBeNull();
});

test("nothing linked and no tokens are said in words", async () => {
  await page((call) =>
    call.method === "GET" ? json({ items: [] }) : undefined,
  );
  expect(screen.getByText(NO_PHONES)).toBeVisible();
  expect(screen.getByText(NO_TOKENS)).toBeVisible();
});

test("a list that cannot be had says why", async () => {
  await page((call) =>
    call.url === "/auth/devices"
      ? refusal(500, "InternalError", "the database is away")
      : undefined,
  );
  expect(screen.getByRole("alert")).toHaveTextContent("the database is away");
});

test("Remove ends the phone, and the list is asked for again", async () => {
  const fetch = await page();
  fireEvent.click(screen.getByRole("button", { name: "Remove Pixel 8" }));
  await waitFor(() => {
    expect(screen.getByText(NO_PHONES)).toBeVisible();
  });
  expect(callsTo(fetch, `/auth/devices/${PHONE.id}`, "DELETE")).toHaveLength(1);
  expect(callsTo(fetch, "/auth/devices")).toHaveLength(2);
});

test("a refused Remove says why, beside the button", async () => {
  await page((call) =>
    call.method === "DELETE"
      ? refusal(404, "NotFound", "you have no device of that id")
      : undefined,
  );
  fireEvent.click(screen.getByRole("button", { name: "Remove Pixel 8" }));
  const row = within(phones()).getByRole("listitem");
  expect(await within(row).findByRole("alert")).toHaveTextContent(
    "you have no device of that id",
  );
});

test("Revoke ends the token, and the list is asked for again", async () => {
  const fetch = await page();
  fireEvent.click(
    screen.getByRole("button", { name: "Revoke nightly export" }),
  );
  await waitFor(() => {
    expect(screen.getByText(NO_TOKENS)).toBeVisible();
  });
  expect(callsTo(fetch, `/auth/tokens/${TOKEN.id}`, "DELETE")).toHaveLength(1);
});

/** New token, a name, Create. */
function makeToken(name: string) {
  fireEvent.click(screen.getByRole("button", { name: "New token" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Name of the token" }), {
    target: { value: name },
  });
  fireEvent.click(screen.getByRole("button", { name: "Create" }));
}

const minting = (call: Call) =>
  call.url === "/auth/tokens" && call.method === "POST"
    ? json({ ...TOKEN, id: "e-2", name: "ci", secret: SECRET }, 201)
    : undefined;

test("New token sends the name it was given", async () => {
  const fetch = await page(minting);
  makeToken("  ci ");
  await screen.findByText(SECRET);
  expect(callsTo(fetch, "/auth/tokens", "POST")).toEqual([
    { url: "/auth/tokens", body: { name: "ci" } },
  ]);
});

test("the secret is shown once, with the sentence that says so", async () => {
  await page(minting);
  makeToken("ci");
  expect(await screen.findByText(SECRET)).toBeVisible();
  expect(screen.getByText(SHOWN_ONCE)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Done" }));
  expect(screen.queryByText(SECRET)).toBeNull();
});

test("New token keeps the secret on the screen until Done", async () => {
  await page(minting);
  makeToken("ci");
  await screen.findByText(SECRET);
  fireEvent.click(screen.getByRole("button", { name: "New token" }));
  expect(screen.getByText(SECRET)).toBeVisible();
  expect(
    screen.queryByRole("textbox", { name: "Name of the token" }),
  ).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Done" }));
  expect(screen.queryByText(SECRET)).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "New token" }));
  expect(
    screen.getByRole("textbox", { name: "Name of the token" }),
  ).toBeVisible();
});

test("Copy puts the secret on the clipboard", async () => {
  const writeText = vi.fn(() => Promise.resolve());
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText },
  });
  try {
    await page(minting);
    makeToken("ci");
    fireEvent.click(await screen.findByRole("button", { name: "Copy" }));
    expect(writeText).toHaveBeenCalledWith(SECRET);
    expect(await screen.findByRole("button", { name: "Copied" })).toBeVisible();
  } finally {
    Reflect.deleteProperty(navigator, "clipboard");
  }
});

test("the list is asked for again once a token is made", async () => {
  const fetch = await page(minting);
  makeToken("ci");
  await screen.findByText(SECRET);
  expect(callsTo(fetch, "/auth/tokens")).toHaveLength(2);
});

test("a token with no name is not asked for", async () => {
  const fetch = await page(minting);
  makeToken("   ");
  expect(screen.getByRole("alert")).toHaveTextContent("A token needs a name.");
  expect(callsTo(fetch, "/auth/tokens", "POST")).toHaveLength(0);
});

test("a refused token says why, beside the form", async () => {
  await page((call) =>
    call.url === "/auth/tokens" && call.method === "POST"
      ? refusal(422, "InvalidValueError", "body.name: too long")
      : undefined,
  );
  makeToken("ci");
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "body.name: too long",
  );
});

test("a session too old to make a token offers to sign in again", async () => {
  await page((call) =>
    call.url === "/auth/tokens" && call.method === "POST"
      ? refusal(403, "FreshSignInRequired", "sign in again")
      : undefined,
  );
  makeToken("ci");
  expect(
    await screen.findByRole("heading", {
      name: "Sign in again to make a token",
    }),
  ).toBeVisible();
  expect(
    screen.getByRole("link", { name: "Sign in again with Google" }),
  ).toHaveAttribute("href", "/auth/login/google?return_to=%2F%23%2Fdevices");
});

test("Link a phone opens the dialog", async () => {
  const fetch = await page((call) =>
    call.url === "/auth/pairings"
      ? json(
          {
            id: "a0000000-0000-4000-8000-000000000001",
            expires_at: "2099-01-01T00:00:00Z",
            qr_svg: "<svg/>",
          },
          201,
        )
      : undefined,
  );
  fireEvent.click(screen.getByRole("button", { name: "Link a phone" }));
  expect(screen.getByRole("dialog", { name: "Link a phone" })).toBeVisible();
  await waitFor(() => {
    expect(callsTo(fetch, "/auth/pairings", "POST")).toHaveLength(1);
  });
});

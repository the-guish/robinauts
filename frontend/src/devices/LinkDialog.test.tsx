// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import { act, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { Session } from "../session/session";
import { loginHref } from "../session/SignInPage";
import { callsTo, json, refusal, stubFetch, type Call } from "../test/api";
import {
  ALT,
  AUTO_RENEWALS,
  BUSY_TEXT,
  CLAIMED,
  EXPIRED,
  LinkDialog,
  LINKED,
  NO_PHONES,
  NOT_SIX,
  POLL_MS,
  SCAN,
  WAITING,
} from "./LinkDialog";

const SESSION: Session = {
  sign_in: true,
  local_development: false,
  providers: [
    { id: "google", title: "Google" },
    { id: "okta", title: "Okta" },
  ],
  user: { id: "u-1", provider: "google", name: "Ada", email: "ada@x.test" },
};

const SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"/>';

const pairingId = (n: number) =>
  `a0000000-0000-4000-8000-${String(n).padStart(12, "0")}`;

const PAIRINGS = "/auth/pairings";
const FIRST = `${PAIRINGS}/${pairingId(1)}`;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  vi.setSystemTime(new Date("2026-11-04T12:00:00Z"));
});

afterEach(() => {
  vi.useRealTimers();
  Reflect.deleteProperty(document, "visibilityState");
});

/**
 * A backend for one dialog: each mint answers the next pairing, and every
 * other call is `answer`'s, or a waiting pairing and a 204.
 */
function backend(
  answer: (call: Call) => Response | undefined = () => undefined,
) {
  let minted = 0;
  return stubFetch((call) => {
    const given = answer(call);
    if (given !== undefined) return given;
    if (call.url === PAIRINGS && call.method === "POST") {
      minted += 1;
      return json(
        {
          id: pairingId(minted),
          expires_at: new Date(Date.now() + 120_000).toISOString(),
          qr_svg: SVG,
        },
        201,
      );
    }
    if (call.method === "DELETE") return json(null, 204);
    return json({ state: "waiting", expires_at: "", device: null });
  });
}

/** A pairing claimed by a phone, as the poll answers it. */
const claimed = () =>
  json({
    state: "claimed",
    expires_at: new Date(Date.now() + 120_000).toISOString(),
    device: { name: "Pixel 8", platform: "android", app_version: null },
  });

/** Let the answers that are already due arrive. */
async function settle() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
}

/** Let time pass, and whatever it brings arrive. */
async function wait(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

/** The page around the dialog: a button that opens it, and closing that unmounts it. */
function Harness({
  onLinked = () => undefined,
}: {
  onLinked?: (() => void) | undefined;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        onClick={() => {
          setOpen(true);
        }}
      >
        Link a phone
      </button>
      {open && (
        <LinkDialog
          session={SESSION}
          onClose={() => {
            setOpen(false);
          }}
          onLinked={onLinked}
        />
      )}
    </>
  );
}

async function opened(onLinked?: () => void) {
  render(<Harness onLinked={onLinked} />);
  const opener = screen.getByRole("button", { name: "Link a phone" });
  opener.focus();
  fireEvent.click(opener);
  await settle();
  return opener;
}

/** Up to the number field: a phone has claimed the code. */
async function claimedDialog(
  answer: (call: Call) => Response | undefined = () => undefined,
  onLinked?: () => void,
) {
  const fetch = backend(
    (call) =>
      answer(call) ??
      (call.url === FIRST && call.method === "GET" ? claimed() : undefined),
  );
  await opened(onLinked);
  await wait(POLL_MS);
  return fetch;
}

const field = () =>
  screen.getByRole("textbox", { name: "The number on the phone" });

function typeAndLink(number: string) {
  fireEvent.change(field(), { target: { value: number } });
  fireEvent.click(screen.getByRole("button", { name: "Link" }));
}

test("opening it asks for a code and shows it as a picture, never as markup", async () => {
  const fetch = backend();
  await opened();
  expect(callsTo(fetch, PAIRINGS, "POST")).toHaveLength(1);
  const picture = screen.getByRole("img", { name: ALT });
  expect(picture).toHaveAttribute(
    "src",
    `data:image/svg+xml;base64,${btoa(SVG)}`,
  );
  // The server's SVG is nowhere in the page as an element of its own.
  expect(
    screen.getByRole("dialog").querySelector('svg[viewBox="0 0 1 1"]'),
  ).toBeNull();
  expect(screen.getByText(SCAN)).toBeVisible();
});

test("it is a dialog named by its title, and the focus is inside it", async () => {
  backend();
  await opened();
  const dialog = screen.getByRole("dialog", { name: "Link a phone" });
  expect(dialog).toHaveAttribute("aria-modal", "true");
  expect(dialog.contains(document.activeElement)).toBe(true);
});

test("the state is said in words, in a polite live region", async () => {
  backend();
  await opened();
  const said = screen.getByText(WAITING);
  expect(said).toHaveAttribute("aria-live", "polite");
});

test("it asks how the code is doing every two seconds", async () => {
  const fetch = backend();
  await opened();
  expect(callsTo(fetch, FIRST)).toHaveLength(0);
  await wait(POLL_MS);
  expect(callsTo(fetch, FIRST)).toHaveLength(1);
  await wait(POLL_MS);
  expect(callsTo(fetch, FIRST)).toHaveLength(2);
});

test("a claimed code asks for the number, in a field made for it", async () => {
  await claimedDialog();
  expect(screen.getByText(CLAIMED)).toBeVisible();
  const box = field();
  expect(box).toHaveAttribute("inputmode", "numeric");
  expect(box).toHaveAttribute("autocomplete", "one-time-code");
  expect(box).toHaveFocus();
  expect(screen.getByRole("button", { name: "Link" })).toBeVisible();
  expect(screen.getByRole("button", { name: "No" })).toBeVisible();
  // The name the phone gave itself is not what the person decides by.
  expect(screen.queryByText(/Pixel 8/)).toBeNull();
});

test("Link sends the six digits as the backend wants them, space or none", async () => {
  const fetch = await claimedDialog((call) =>
    call.url.endsWith("/approve") ? json({ id: "d-1" }) : undefined,
  );
  typeAndLink("482 913");
  await settle();
  expect(callsTo(fetch, `${FIRST}/approve`, "POST")).toEqual([
    { url: `${FIRST}/approve`, body: { number: "482913" } },
  ]);
});

test("the right number links the phone, refreshes the list and offers Done", async () => {
  const onLinked = vi.fn();
  await claimedDialog(
    (call) => (call.url.endsWith("/approve") ? json({ id: "d-1" }) : undefined),
    onLinked,
  );
  typeAndLink("482913");
  await settle();
  expect(screen.getByText(LINKED)).toBeVisible();
  expect(onLinked).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("button", { name: "Done" })).toHaveFocus();
});

test("what is not six digits is not sent", async () => {
  const fetch = await claimedDialog();
  typeAndLink("48291");
  await settle();
  expect(callsTo(fetch, `${FIRST}/approve`, "POST")).toHaveLength(0);
  expect(screen.getByRole("alert")).toHaveTextContent(NOT_SIX);
});

test("a wrong number is counted, and the number asked for again", async () => {
  await claimedDialog((call) =>
    call.url.endsWith("/approve")
      ? refusal(403, "WrongNumber", "that is not the number the phone shows")
      : undefined,
  );
  typeAndLink("111111");
  await settle();
  expect(
    screen.getByText("That is not the number. Two tries left."),
  ).toBeVisible();
  expect(field()).toHaveValue("");
  typeAndLink("222222");
  await settle();
  expect(
    screen.getByText("That is not the number. One try left."),
  ).toBeVisible();
});

test("the third wrong number ends the pairing, and offers a new code", async () => {
  let tries = 0;
  await claimedDialog((call) => {
    if (!call.url.endsWith("/approve")) return undefined;
    tries += 1;
    return tries < 3
      ? refusal(403, "WrongNumber", "that is not the number the phone shows")
      : refusal(404, "NotFound", "you have no such pairing waiting");
  });
  for (const number of ["111111", "222222", "333333"]) {
    typeAndLink(number);
    await settle();
  }
  expect(
    screen.getByText("That was the third wrong number, so the code is gone."),
  ).toBeVisible();
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(screen.getByRole("button", { name: "Show a new code" })).toBeVisible();
});

test("an approve of a code no phone holds asks once more what it is", async () => {
  let asked = 0;
  await claimedDialog((call) => {
    if (call.url.endsWith("/approve")) {
      return refusal(409, "Conflict", "no phone is waiting on this code");
    }
    if (call.url === FIRST && call.method === "GET") {
      asked += 1;
      return asked === 1
        ? claimed()
        : json({ state: "waiting", expires_at: "", device: null });
    }
    return undefined;
  });
  typeAndLink("482913");
  await settle();
  expect(asked).toBe(2);
  expect(screen.getByText(WAITING)).toBeVisible();
});

test("No deletes the pairing and closes the dialog", async () => {
  const fetch = await claimedDialog();
  fireEvent.click(screen.getByRole("button", { name: "No" }));
  await settle();
  expect(callsTo(fetch, FIRST, "DELETE")).toHaveLength(1);
  expect(screen.queryByRole("dialog")).toBeNull();
});

test("closing it while the code waits deletes the pairing, once", async () => {
  const fetch = backend();
  await opened();
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  await settle();
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(callsTo(fetch, FIRST, "DELETE")).toHaveLength(1);
});

test("Escape closes it, deletes the pairing and gives the focus back", async () => {
  const fetch = backend();
  const opener = await opened();
  fireEvent.keyDown(document, { key: "Escape" });
  await settle();
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(callsTo(fetch, FIRST, "DELETE")).toHaveLength(1);
  expect(opener).toHaveFocus();
});

test("closing it once linked deletes nothing", async () => {
  const fetch = await claimedDialog((call) =>
    call.url.endsWith("/approve") ? json({ id: "d-1" }) : undefined,
  );
  typeAndLink("482913");
  await settle();
  fireEvent.click(screen.getByRole("button", { name: "Done" }));
  await settle();
  expect(callsTo(fetch, FIRST, "DELETE")).toHaveLength(0);
});

test("closing it stops the polling", async () => {
  const fetch = backend();
  await opened();
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  await wait(POLL_MS * 3);
  expect(callsTo(fetch, FIRST)).toHaveLength(0);
});

test("a code gone while it waits is replaced at once", async () => {
  const fetch = backend((call) =>
    call.url === FIRST && call.method === "GET"
      ? refusal(404, "NotFound", "you have no such pairing waiting")
      : undefined,
  );
  await opened();
  await wait(POLL_MS);
  expect(callsTo(fetch, PAIRINGS, "POST")).toHaveLength(2);
  expect(screen.getByText(WAITING)).toBeVisible();
});

test("a code past its expiry is replaced without asking about it", async () => {
  const fetch = backend((call) =>
    call.url === PAIRINGS && call.method === "POST"
      ? json(
          {
            id: pairingId(1),
            expires_at: new Date(Date.now() + 1_000).toISOString(),
            qr_svg: SVG,
          },
          201,
        )
      : undefined,
  );
  await opened();
  await wait(POLL_MS);
  expect(callsTo(fetch, PAIRINGS, "POST")).toHaveLength(2);
  expect(callsTo(fetch, FIRST)).toHaveLength(0);
});

test("five codes are replaced by themselves; after that, a button", async () => {
  const fetch = backend((call) =>
    call.url.startsWith(`${PAIRINGS}/`) && call.method === "GET"
      ? refusal(404, "NotFound", "you have no such pairing waiting")
      : undefined,
  );
  await opened();
  for (let round = 0; round <= AUTO_RENEWALS; round += 1) {
    await wait(POLL_MS);
  }
  expect(callsTo(fetch, PAIRINGS, "POST")).toHaveLength(1 + AUTO_RENEWALS);
  expect(screen.getByText(EXPIRED)).toBeVisible();
  const again = screen.getByRole("button", { name: "Show a new code" });
  await wait(POLL_MS * 3);
  expect(callsTo(fetch, PAIRINGS, "POST")).toHaveLength(1 + AUTO_RENEWALS);
  fireEvent.click(again);
  await settle();
  expect(callsTo(fetch, PAIRINGS, "POST")).toHaveLength(2 + AUTO_RENEWALS);
});

test("a session too old to mint turns it into a sign-in, back to this page", async () => {
  backend((call) =>
    call.url === PAIRINGS
      ? refusal(403, "FreshSignInRequired", "sign in again")
      : undefined,
  );
  await opened();
  expect(
    screen.getByRole("dialog", { name: "Sign in again to link a phone" }),
  ).toBeVisible();
  expect(
    screen.getByRole("link", { name: "Sign in again with Google" }),
  ).toHaveAttribute("href", loginHref("google", "#/devices"));
  expect(
    screen.getByRole("link", { name: "Sign in again with Okta" }),
  ).toHaveAttribute("href", "/auth/login/okta?return_to=%2F%23%2Fdevices");
});

test("too many codes at once is said in words", async () => {
  backend((call) =>
    call.url === PAIRINGS
      ? refusal(429, "Busy", "3 codes are waiting")
      : undefined,
  );
  await opened();
  expect(screen.getByText(BUSY_TEXT)).toBeVisible();
});

test("a deployment that allows no phones says so", async () => {
  backend((call) =>
    call.url === PAIRINGS
      ? refusal(403, "Forbidden", "this deployment links no devices")
      : undefined,
  );
  await opened();
  expect(screen.getByText(NO_PHONES)).toBeVisible();
});

test("any other refusal of the mint is its own sentence", async () => {
  backend((call) =>
    call.url === PAIRINGS
      ? refusal(500, "InternalError", "the database is away")
      : undefined,
  );
  await opened();
  expect(screen.getByText("the database is away")).toBeVisible();
});

/** Say that the tab is hidden, or visible again, as a browser does. */
function visibility(state: "hidden" | "visible") {
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => state,
  });
  document.dispatchEvent(new Event("visibilitychange"));
}

test("a hidden tab is not polled, and polling resumes once it is visible", async () => {
  const fetch = backend();
  await opened();
  await act(async () => {
    visibility("hidden");
    await Promise.resolve();
  });
  await wait(POLL_MS * 5);
  expect(callsTo(fetch, FIRST)).toHaveLength(0);
  await act(async () => {
    visibility("visible");
    await Promise.resolve();
  });
  await wait(POLL_MS);
  expect(callsTo(fetch, FIRST)).toHaveLength(1);
});

// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The calls behind "Linked devices", and the little state they need
 * (`docs/architecture/device-pairing.md`).
 *
 * Everything here runs on the browser's session cookie. The page is the one
 * place a credential is made or ended, and the backend refuses a bearer
 * every one of these writes, so there is nothing to choose: the web is always
 * the cookie. `GET /auth/tokens` and `GET /auth/devices` would answer a
 * bearer too; this page never is one.
 *
 * The polling of a pairing is `fetch` on a timer through the typed client
 * (`./LinkDialog.tsx`); the QR comes from the server as SVG text and is
 * shown as a picture, never as markup (`qrSource`).
 */
import { useEffect, useState } from "react";

import { ApiError, detailOf, isRefusal, request } from "../api/client";
import type { components } from "../api/schema";

export type Device = components["schemas"]["DeviceSummary"];
export type Token = components["schemas"]["ApiTokenSummary"];
export type NewToken = components["schemas"]["NewApiTokenResponse"];
export type Minted = components["schemas"]["MintedPairingResponse"];
export type PairingView = components["schemas"]["PairingView"];

/** Where this page sends a sign-in that has to be fresher. */
export const DEVICES_HASH = "#/devices";

/** The refusal of a mint whose session is older than ten minutes. */
export const FRESH_SIGN_IN_REQUIRED = "FreshSignInRequired";
/** A number that is not the one the phone shows (403). */
export const WRONG_NUMBER = "WrongNumber";
/** More codes waiting than a person may have at once (429). */
export const BUSY = "Busy";
/** A deployment with `device_days = 0` (403), among other refusals. */
export const FORBIDDEN = "Forbidden";
/** An approve of a pairing no phone has claimed (409). */
export const CONFLICT = "Conflict";

/** Whether the backend wants a younger session before it mints. */
export function needsFreshSignIn(failure: unknown): boolean {
  return isRefusal(failure, FRESH_SIGN_IN_REQUIRED);
}

/** Whether a call was answered 404: what it named is gone, or was never ours. */
export function isGone(failure: unknown): boolean {
  return failure instanceof ApiError && failure.status === 404;
}

export const listDevices = (signal: AbortSignal) =>
  request("get", "/auth/devices", { signal });

export const removeDevice = (id: string) =>
  request("delete", "/auth/devices/{device_id}", { path: { device_id: id } });

export const listTokens = (signal: AbortSignal) =>
  request("get", "/auth/tokens", { signal });

export const mintToken = (name: string) =>
  request("post", "/auth/tokens", { body: { name } });

export const revokeToken = (id: string) =>
  request("delete", "/auth/tokens/{token_id}", { path: { token_id: id } });

export const mintPairing = () => request("post", "/auth/pairings");

export const pairingState = (id: string) =>
  request("get", "/auth/pairings/{pairing_id}", { path: { pairing_id: id } });

export const approvePairing = (id: string, number: string) =>
  request("post", "/auth/pairings/{pairing_id}/approve", {
    path: { pairing_id: id },
    body: { number },
  });

export const deletePairing = (id: string) =>
  request("delete", "/auth/pairings/{pairing_id}", {
    path: { pairing_id: id },
  });

/**
 * The QR as the `src` of an `<img>`.
 *
 * A data URL, which the Content-Security-Policy's `img-src 'self' data:`
 * allows. The SVG is never put into the page as markup: as a picture it
 * cannot run anything or reach anywhere, whatever it holds. The backend
 * writes it in ASCII, which is what `btoa` takes.
 */
export function qrSource(svg: string): string {
  return `data:image/svg+xml;base64,${btoa(svg)}`;
}

/** The backend's own pattern for a number: six digits, a space allowed. */
const NUMBER = /^[0-9]{3} ?[0-9]{3}$/;

/**
 * The six digits as the backend wants them, or `null` if that is not what
 * was typed.
 *
 * The phone shows `482 913`; the person may type it with the space or
 * without. Either is sent as `482913`. What is not six digits is not sent
 * at all: the backend would answer 422 and count nothing, and saying so here
 * is quicker.
 */
export function typedNumber(typed: string): string | null {
  const trimmed = typed.trim();
  return NUMBER.test(trimmed) ? trimmed.replace(" ", "") : null;
}

/** Whether an instant the server wrote has passed. */
export function hasPassed(instant: string, now = Date.now()): boolean {
  const at = Date.parse(instant);
  return Number.isFinite(at) && at <= now;
}

/** How a platform the app reported is written for a reader. */
const PLATFORMS = new Map([
  ["android", "Android"],
  ["ios", "iOS"],
  ["desktop", "Desktop"],
  ["other", "Other"],
]);

export function platformName(platform: string): string {
  return PLATFORMS.get(platform) ?? platform;
}

const DATES = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

/** An instant in the browser's own locale; the text as it came if unreadable. */
export function shownDate(instant: string): string {
  const at = Date.parse(instant);
  return Number.isFinite(at) ? DATES.format(at) : instant;
}

/** A list, as far as the call for it has got. */
export type Listed<T> =
  | { status: "loading" }
  | { status: "ready"; items: T[] }
  | { status: "failed"; detail: string };

/**
 * The list `ask` answers, asked for again whenever `round` changes.
 *
 * The list it had stays on the screen while the next one comes, so that a
 * Remove does not blank the page for a moment. `ask` must be the same
 * function on every render: one of this module's.
 */
export function useListed<T>(
  ask: (signal: AbortSignal) => Promise<{ items: T[] }>,
  round: number,
): Listed<T> {
  const [listed, setListed] = useState<Listed<T>>({ status: "loading" });
  useEffect(() => {
    const dropped = new AbortController();
    ask(dropped.signal).then(
      (answer) => {
        setListed({ status: "ready", items: answer.items });
      },
      (failure: unknown) => {
        if (dropped.signal.aborted) return;
        setListed({ status: "failed", detail: detailOf(failure) });
      },
    );
    return () => {
      dropped.abort();
    };
  }, [ask, round]);
  return listed;
}

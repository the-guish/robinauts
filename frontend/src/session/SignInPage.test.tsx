// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";

import type { Session } from "./session";
import {
  GENERIC_ERROR,
  SIGN_IN_ERRORS,
  SignInPage,
  errorInHash,
  loginHref,
  returnTo,
} from "./SignInPage";

/** Written as an escape: in the source of a test it is otherwise invisible. */
const ZERO_WIDTH = "\u200b";

const CONFIGURED: Session = {
  sign_in: true,
  local_development: false,
  flows: false,
  providers: [
    { id: "google", title: "Google" },
    { id: "okta", title: "Okta" },
  ],
  user: null,
};

test("one link per provider, each carrying where to come back to", () => {
  location.hash = "#/chat/7";
  render(<SignInPage session={CONFIGURED} />);
  const google = screen.getByRole("link", { name: "Sign in with Google" });
  expect(google).toHaveAttribute(
    "href",
    "/auth/login/google?return_to=%2F%23%2Fchat%2F7",
  );
  expect(screen.getByRole("link", { name: "Sign in with Okta" })).toBeVisible();
});

test("the code the redirect came back with becomes one fixed sentence", () => {
  location.hash = "#/sign-in?error=not_allowed";
  render(<SignInPage session={CONFIGURED} />);
  expect(screen.getByRole("alert")).toHaveTextContent(
    SIGN_IN_ERRORS.get("not_allowed") ?? "",
  );
});

test("every code the backend can send has a sentence", () => {
  // domain.SignInErrorCode, all of it.
  for (const code of [
    "expired",
    "state_mismatch",
    "not_allowed",
    "unknown_provider",
    "busy",
    "provider_unavailable",
    "provider_refused",
    "invalid_id_token",
  ]) {
    expect(SIGN_IN_ERRORS.get(code), code).toBeTypeOf("string");
  }
  expect(SIGN_IN_ERRORS.size).toBe(8);
});

test("a code that names something every object has is still just a code", () => {
  // The code is whatever is in the address bar. Looked up in an object,
  // `__proto__` and `toString` answer with an object and a function, which
  // React throws on rendering -- and this page is the only one somebody who
  // is not signed in can reach.
  for (const code of [
    "__proto__",
    "toString",
    "constructor",
    "hasOwnProperty",
  ]) {
    location.hash = `#/sign-in?error=${code}`;
    const { unmount } = render(<SignInPage session={CONFIGURED} />);
    expect(screen.getByRole("alert"), code).toHaveTextContent(GENERIC_ERROR);
    unmount();
  }
});

test("a code this interface does not know is still a failed sign-in", () => {
  location.hash = "#/sign-in?error=something_new";
  render(<SignInPage session={CONFIGURED} />);
  expect(screen.getByRole("alert")).toHaveTextContent(GENERIC_ERROR);
});

test("no code, no complaint", () => {
  location.hash = "#/";
  render(<SignInPage session={CONFIGURED} />);
  expect(screen.queryByRole("alert")).toBeNull();
});

test("a deployment with no sign-in says so and offers nothing", () => {
  render(
    <SignInPage
      session={{
        sign_in: false,
        local_development: false,
        flows: false,
        providers: [],
      }}
    />,
  );
  expect(screen.queryAllByRole("link")).toHaveLength(0);
  expect(screen.getByText(/no sign-in configured/)).toBeVisible();
});

test("the error code is read from the hash, where the backend puts it", () => {
  expect(errorInHash("#/sign-in?error=busy")).toBe("busy");
  expect(errorInHash("#/sign-in")).toBeNull();
  expect(errorInHash("")).toBeNull();
  expect(errorInHash("#/sign-in?other=1")).toBeNull();
});

test("only a hash route of this origin is somewhere to return to", () => {
  expect(returnTo("#/chat/7")).toBe("/#/chat/7");
  // Nowhere to return to, or somewhere that is not ours.
  expect(returnTo("")).toBe("/");
  expect(returnTo("#")).toBe("/");
  expect(returnTo("#https://evil.test/")).toBe("/");
  // `//host` and `/\host` are another origin with the scheme left out.
  expect(returnTo("#//evil.test")).toBe("/");
  expect(returnTo("#/\\evil.test")).toBe("/");
  expect(returnTo("#/a\\b")).toBe("/");
  // Printable ASCII only: a newline is a header of its own to whatever
  // writes one, and a zero-width space makes two targets that look like one.
  expect(returnTo("#/a\nb")).toBe("/");
  expect(returnTo("#/a b")).toBe("/");
  expect(returnTo(`#/a${ZERO_WIDTH}b`)).toBe("/");
  // Longer than core.MAX_RETURN_TO.
  expect(returnTo(`#/${"a".repeat(600)}`)).toBe("/");
  // The sign-in page is not somewhere to be returned to.
  expect(returnTo("#/sign-in?error=busy")).toBe("/");
});

test("the login link escapes both the provider and the target", () => {
  expect(loginHref("okta", "#/chat/a b")).toBe(
    "/auth/login/okta?return_to=%2F",
  );
  expect(loginHref("a/b", "#/")).toBe("/auth/login/a%2Fb?return_to=%2F%23%2F");
});

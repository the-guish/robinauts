// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import { act, render, screen } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";

import { App } from "./App";
import { ErrorBoundary } from "./ErrorBoundary";

// oneberth's shell, made to throw: it holds the sign-in page, the page
// somebody who is not signed in reaches, and a blank one there is a
// deployment nobody can get into.
vi.mock("@oneberth/ui", () => ({
  Shell: () => {
    throw new Error("the sign-in page fell over");
  },
}));

beforeEach(() => {
  // React writes a caught render error to the console, and so does the
  // boundary. Neither is a failure of the test.
  vi.spyOn(console, "error").mockImplementation(() => undefined);
});

function Throwing(): never {
  throw new Error("a render that threw");
}

test("a child that throws becomes a message and a way back", () => {
  render(
    <ErrorBoundary>
      <Throwing />
    </ErrorBoundary>,
  );
  expect(
    screen.getByRole("heading", { name: "Something went wrong" }),
  ).toBeVisible();
  expect(screen.getByRole("alert")).toHaveTextContent("a render that threw");
  expect(screen.getByRole("button", { name: "Reload" })).toBeVisible();
});

test("the boundary is outside oneberth's shell, the sign-in page included", async () => {
  render(<App />);
  await act(async () => {
    await Promise.resolve();
  });
  expect(
    screen.getByRole("heading", { name: "Something went wrong" }),
  ).toBeVisible();
  expect(screen.getByRole("alert")).toHaveTextContent(
    "the sign-in page fell over",
  );
});

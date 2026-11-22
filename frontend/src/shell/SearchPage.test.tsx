// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test } from "vitest";

import { callsTo, json, refusal, stubFetch } from "../test/api";
import { conversation, DEFAULT_PROJECT_ID, page } from "../test/conversations";
import { SearchPage } from "./SearchPage";

const PROJECTS = [
  { id: DEFAULT_PROJECT_ID, name: "Work", created_at: "2026-01-01T00:00:00Z" },
];

function drawn() {
  render(<SearchPage projects={PROJECTS} agentName={(id) => `Agent ${id}`} />);
  return screen.getByRole("searchbox", { name: "Search conversations" });
}

test("nothing is asked or listed until something is typed", async () => {
  const fetch = stubFetch(() => json(page([])));
  drawn();
  expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Search");
  expect(screen.queryByRole("table")).toBeNull();
  expect(callsTo(fetch, "/robinauts/api/conversations")).toHaveLength(0);
});

test("what is typed is asked of the backend, and the rows say project, name and agent", async () => {
  const fetch = stubFetch((call) =>
    call.url.includes("q=budget")
      ? json(page([conversation(1, "Budget plan")]))
      : json(page([])),
  );
  const box = drawn();
  fireEvent.change(box, { target: { value: "budget" } });
  expect(
    await screen.findByRole("link", { name: "Budget plan" }),
  ).toBeVisible();
  expect(screen.getByText("Work")).toBeVisible();
  expect(screen.getByText("Agent helper")).toBeVisible();
  expect(callsTo(fetch, "/robinauts/api/conversations").length).toBe(1);

  fireEvent.change(box, { target: { value: "zzz" } });
  expect(await screen.findByText("No conversations found.")).toBeVisible();
});

test("results are paged by the backend's cursor", async () => {
  stubFetch((call) =>
    call.url.includes("cursor=next")
      ? json(page([conversation(2, "Second")]))
      : json(page([conversation(1, "First")], "next")),
  );
  const box = drawn();
  fireEvent.change(box, { target: { value: "x" } });
  expect(await screen.findByRole("link", { name: "First" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Next" }));
  expect(await screen.findByRole("link", { name: "Second" })).toBeVisible();
  expect(screen.getByText("Page 2")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Previous" }));
  expect(await screen.findByRole("link", { name: "First" })).toBeVisible();
});

test("a refusal is said, and is not asked again", async () => {
  const fetch = stubFetch(() => refusal(500, "internal", "boom"));
  const box = drawn();
  fireEvent.change(box, { target: { value: "x" } });
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "The search failed",
  );
  await waitFor(() => {
    expect(callsTo(fetch, "/robinauts/api/conversations")).toHaveLength(1);
  });
});

test("the rows stay while the next answer is on its way", async () => {
  let release: (response: Response) => void = () => undefined;
  stubFetch((call) =>
    call.url.includes("q=more")
      ? (new Promise<Response>((resolve) => {
          release = resolve;
        }) as unknown as Response)
      : json(page([conversation(1, "First")])),
  );
  const box = drawn();
  fireEvent.change(box, { target: { value: "first" } });
  expect(await screen.findByRole("link", { name: "First" })).toBeVisible();
  fireEvent.change(box, { target: { value: "more" } });
  expect(await screen.findByText("Searching…")).toBeVisible();
  expect(screen.getByRole("link", { name: "First" })).toBeVisible();
  release(json(page([conversation(2, "Second")])));
  expect(await screen.findByRole("link", { name: "Second" })).toBeVisible();
  expect(screen.queryByRole("link", { name: "First" })).toBeNull();
});

test("a blank box asks nothing and lists nothing", async () => {
  const fetch = stubFetch(() => json(page([conversation(1, "First")])));
  const box = drawn();
  fireEvent.change(box, { target: { value: "first" } });
  await screen.findByRole("link", { name: "First" });
  fireEvent.change(box, { target: { value: "   " } });
  await waitFor(() => {
    expect(screen.queryByRole("table")).toBeNull();
  });
  expect(callsTo(fetch, "/robinauts/api/conversations")).toHaveLength(1);
});

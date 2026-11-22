// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import type { History } from "../history/history";
import { RESULTS_PER_PAGE, SearchPage } from "./SearchPage";

const PROJECT = "11111111-1111-4111-8111-111111111111";

function history(titles: string[]): History {
  return {
    status: "ready",
    items: titles.map((title, i) => ({
      id: `3f1b8e2a-4c5d-4e6f-8a9b-${String(i).padStart(12, "0")}`,
      title,
      agent: "helper",
      model: "m",
      project_id: PROJECT,
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
      running: false,
    })),
    more: false,
    loadingMore: false,
    refreshing: false,
    error: null,
    refresh: vi.fn(),
    loadMore: vi.fn(),
    rename: vi.fn(),
    archive: vi.fn(),
    remove: vi.fn(),
  };
}

function page(titles: string[]) {
  render(
    <SearchPage
      history={history(titles)}
      projects={[
        { id: PROJECT, name: "Work", created_at: "2026-01-01T00:00:00Z" },
      ]}
      agentName={(id) => `Agent ${id}`}
    />,
  );
}

test("nothing is listed until something is typed, then rows match and update", () => {
  page(["Budget plan", "Holiday trip"]);
  expect(screen.queryByRole("table")).toBeNull();
  const box = screen.getByRole("searchbox", { name: "Search conversations" });
  fireEvent.change(box, { target: { value: "budget" } });
  expect(screen.getByRole("link", { name: "Budget plan" })).toBeTruthy();
  expect(screen.queryByRole("link", { name: "Holiday trip" })).toBeNull();
  expect(screen.getByText("Work")).toBeTruthy();
  expect(screen.getByText("Agent helper")).toBeTruthy();
  fireEvent.change(box, { target: { value: "zzz" } });
  expect(screen.getByText("No conversations found.")).toBeTruthy();
});

test("results are paged", () => {
  page(Array.from({ length: RESULTS_PER_PAGE + 3 }, (_, i) => `Chat ${i}`));
  fireEvent.change(screen.getByRole("searchbox"), {
    target: { value: "chat" },
  });
  expect(screen.getAllByRole("link")).toHaveLength(RESULTS_PER_PAGE);
  fireEvent.click(screen.getByRole("button", { name: "Next" }));
  expect(screen.getAllByRole("link")).toHaveLength(3);
  expect(screen.getByText("Page 2 of 2")).toBeTruthy();
});

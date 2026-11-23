// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test } from "vitest";

import { callsTo, json, refusal, stubFetch } from "../test/api";
import {
  conversation,
  DEFAULT_PROJECT_ID,
  id,
  page,
} from "../test/conversations";
import { SearchPage } from "./SearchPage";

const PROJECTS = [
  { id: DEFAULT_PROJECT_ID, name: "Work", created_at: "2026-01-01T00:00:00Z" },
];

function drawn() {
  render(<SearchPage projects={PROJECTS} agentName={(id) => `Agent ${id}`} />);
  return screen.getByRole("searchbox", { name: "Search conversations" });
}

test("on opening, the conversations that are not archived are listed", async () => {
  const fetch = stubFetch(() => json(page([conversation(1, "First")])));
  drawn();
  expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Search");
  expect(await screen.findByRole("link", { name: "First" })).toBeVisible();
  const [call] = callsTo(fetch, "/robinauts/api/conversations");
  expect(call?.url).not.toContain("q=");
  expect(call?.url).toContain("archived=false");
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
  expect(screen.getByRole("cell", { name: "Work" })).toBeVisible();
  expect(screen.getByText("Agent helper")).toBeVisible();
  // The listing on opening, then the search.
  expect(callsTo(fetch, "/robinauts/api/conversations").length).toBe(2);

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

test("a blank box lists everything again", async () => {
  const fetch = stubFetch((call) =>
    call.url.includes("q=first")
      ? json(page([conversation(1, "First")]))
      : json(page([conversation(1, "First"), conversation(2, "Second")])),
  );
  const box = drawn();
  expect(await screen.findByRole("link", { name: "Second" })).toBeVisible();
  fireEvent.change(box, { target: { value: "first" } });
  await waitFor(() => {
    expect(screen.queryByRole("link", { name: "Second" })).toBeNull();
  });
  fireEvent.change(box, { target: { value: "   " } });
  expect(await screen.findByRole("link", { name: "Second" })).toBeVisible();
  expect(callsTo(fetch, "/robinauts/api/conversations")).toHaveLength(3);
});

test("the filters narrow to a project and add the archived ones, marked", async () => {
  const work = { id: id(77), name: "Side", created_at: "2026-01-01T00:00:00Z" };
  const fetch = stubFetch((call) => {
    if (call.url.includes("archived=true"))
      return json({
        items: [{ ...conversation(2, "Old"), archived: true }],
        next_cursor: null,
      });
    if (call.url.includes(`project_id=${work.id}`))
      return json(page([conversation(1, "In side")]));
    return json(page([conversation(3, "Anything")]));
  });
  render(<SearchPage projects={[...PROJECTS, work]} agentName={(a) => a} />);
  expect(await screen.findByRole("link", { name: "Anything" })).toBeVisible();
  fireEvent.change(screen.getByRole("combobox", { name: "Project" }), {
    target: { value: work.id },
  });
  expect(await screen.findByRole("link", { name: "In side" })).toBeVisible();
  fireEvent.change(screen.getByRole("combobox", { name: "Include archived" }), {
    target: { value: "yes" },
  });
  expect(await screen.findByRole("link", { name: "Old" })).toBeVisible();
  expect(screen.getByText("Archived")).toBeVisible();
  const last = callsTo(fetch, "/robinauts/api/conversations").at(-1);
  expect(last?.url).toContain(`project_id=${work.id}`);
});

test("ticked rows are archived by their ids, and the listing is asked again", async () => {
  let archived = false;
  const fetch = stubFetch((call) => {
    if (call.url.includes("/bulk")) {
      archived = true;
      return json({ count: 1 });
    }
    return json(
      page(
        archived
          ? [conversation(2, "Second")]
          : [conversation(1, "First"), conversation(2, "Second")],
      ),
    );
  });
  drawn();
  const archive = await screen.findByRole("button", { name: "Archive" });
  expect(archive).toBeDisabled();
  fireEvent.click(
    await screen.findByRole("checkbox", { name: "Select First" }),
  );
  expect(screen.getByText("1 conversation selected")).toBeVisible();
  fireEvent.click(archive);
  expect(await screen.findByText("Archived 1 conversation.")).toBeVisible();
  expect(callsTo(fetch, "/robinauts/api/conversations/bulk", "POST")).toEqual([
    expect.objectContaining({ body: { action: "archive", ids: [id(1)] } }),
  ]);
  await waitFor(() => {
    expect(screen.queryByRole("link", { name: "First" })).toBeNull();
  });
  expect(
    screen.getByRole("checkbox", { name: "Select Second" }),
  ).not.toBeChecked();
});

test("select all reaches every match on every page but the rows unticked, and delete asks first", async () => {
  const fetch = stubFetch((call) =>
    call.url.includes("/bulk")
      ? json({ count: 14 })
      : json(
          page([conversation(1, "First"), conversation(2, "Second")], "next"),
        ),
  );
  const box = drawn();
  fireEvent.change(box, { target: { value: "plan" } });
  await waitFor(() => {
    expect(
      callsTo(fetch, "/robinauts/api/conversations").some((c) =>
        c.url.includes("q=plan"),
      ),
    ).toBe(true);
  });
  const all = screen.getByRole("checkbox", {
    name: "Select all matching conversations",
  });
  fireEvent.click(all);
  expect(all).toBeChecked();
  expect(screen.getByText("All matching conversations selected")).toBeVisible();
  const second = await screen.findByRole("checkbox", { name: "Select Second" });
  expect(second).toBeChecked();
  fireEvent.click(second);
  expect((all as HTMLInputElement).indeterminate).toBe(true);
  expect(
    screen.getByText("All matching conversations selected but 1"),
  ).toBeVisible();

  fireEvent.click(screen.getByRole("button", { name: "Delete" }));
  expect(
    callsTo(fetch, "/robinauts/api/conversations/bulk", "POST"),
  ).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", { name: "Yes, delete" }));
  expect(await screen.findByText("Deleted 14 conversations.")).toBeVisible();
  expect(callsTo(fetch, "/robinauts/api/conversations/bulk", "POST")).toEqual([
    expect.objectContaining({
      body: {
        action: "delete",
        matching: { q: "plan", project_id: null, archived: false },
        except_ids: [id(2)],
      },
    }),
  ]);
  expect(all).not.toBeChecked();
});

test("a refused bulk action is said, and the selection stays", async () => {
  stubFetch((call) =>
    call.url.includes("/bulk")
      ? refusal(500, "internal", "boom")
      : json(page([conversation(1, "First")])),
  );
  drawn();
  const row = await screen.findByRole("checkbox", { name: "Select First" });
  fireEvent.click(row);
  fireEvent.click(screen.getByRole("button", { name: "Archive" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "The archive failed",
  );
  expect(row).toBeChecked();
});

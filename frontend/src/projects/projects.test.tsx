// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The history a project at a time, as the panel draws it: each project
 * folds away under its heading, "+" starts a new chat in it, and "New
 * project" makes another. Wired as the shell wires it.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useCallback, useRef, useState } from "react";
import { expect, test, vi } from "vitest";

import { useHistory } from "../history/history";
import { Panel } from "../shell/Panel";
import { callsTo, json, refusal, stubFetch } from "../test/api";
import { conversation, DEFAULT_PROJECT_ID, page } from "../test/conversations";
import {
  createProject,
  FOLDED_KEY,
  type Project,
  useFolded,
  useProjects,
} from "./projects";

const DEFAULT: Project = {
  id: DEFAULT_PROJECT_ID,
  name: "Default",
  created_at: "2026-09-01T09:00:00Z",
};
const WORK: Project = {
  id: "00000000-0000-4000-8000-888888888888",
  name: "Work",
  created_at: "2026-09-01T09:30:00Z",
};

function WithProjects({
  onNewChatIn = () => undefined,
}: Readonly<{ onNewChatIn?: (project: string) => void }>) {
  const history = useHistory();
  const [round, setRound] = useState(0);
  const projects = useProjects(round);
  const folded = useFolded();
  const closer = useRef<HTMLButtonElement>(null);
  const newProject = useCallback(async (name: string) => {
    await createProject(name);
    setRound((was) => was + 1);
  }, []);
  return (
    <Panel
      user={null}
      history={history}
      current={null}
      projects={projects.status === "ready" ? projects.items : null}
      folded={folded}
      onNewChatIn={onNewChatIn}
      onNewProject={newProject}
      collapsed={false}
      onCollapse={() => undefined}
      drawer={false}
      onCloseDrawer={() => undefined}
      onNewChat={() => undefined}
      signOut={() => Promise.resolve()}
      signInConfigured
      closeRef={closer}
    />
  );
}

/** The API, with these projects to start with; a project made is added. */
function serve(projects: Project[]) {
  const known = [...projects];
  const atWork = { ...conversation(2, "At work"), project_id: WORK.id };
  return stubFetch((call) => {
    if (call.url.startsWith("/api/projects")) {
      if (call.method === "POST") {
        const { name } = call.body as { name: string };
        if (name === "Refused") {
          return refusal(422, "InvalidValueError", "not that name");
        }
        const made = {
          id: "00000000-0000-4000-8000-777777777777",
          name,
          created_at: "2026-09-03T09:00:00Z",
        };
        known.push(made);
        return json(made, 201);
      }
      return json({ items: known });
    }
    return json(page([conversation(1, "Anywhere"), atWork]));
  });
}

const group = (name: string) => screen.getByRole("list", { name });

test("each conversation is under its project, and a project with none says so", async () => {
  localStorage.clear();
  serve([DEFAULT, WORK]);
  render(<WithProjects />);
  await waitFor(() => {
    expect(screen.getByRole("list", { name: "Work" })).toBeVisible();
  });
  expect(group("Default")).toHaveTextContent("Anywhere");
  expect(group("Default")).not.toHaveTextContent("At work");
  expect(group("Work")).toHaveTextContent("At work");
  localStorage.clear();
});

test("a project folds away under its heading, and stays folded", async () => {
  localStorage.clear();
  serve([DEFAULT, WORK]);
  const { unmount } = render(<WithProjects />);
  const heading = await screen.findByRole("button", { name: /^Work/ });
  expect(heading).toHaveAttribute("aria-expanded", "true");
  fireEvent.click(heading);
  expect(heading).toHaveAttribute("aria-expanded", "false");
  expect(screen.queryByRole("link", { name: "At work" })).toBeNull();
  expect(screen.getByRole("link", { name: "Anywhere" })).toBeVisible();
  expect(
    JSON.parse(localStorage.getItem(`robinauts.${FOLDED_KEY}`) ?? "[]"),
  ).toEqual([WORK.id]);
  unmount();

  // Drawn again, it is still folded; opened, its conversations are back.
  render(<WithProjects />);
  const again = await screen.findByRole("button", { name: /^Work/ });
  await waitFor(() => {
    expect(again).toHaveAttribute("aria-expanded", "false");
  });
  fireEvent.click(again);
  expect(await screen.findByRole("link", { name: "At work" })).toBeVisible();
  localStorage.clear();
});

test("+ on a project starts a new chat in it", async () => {
  localStorage.clear();
  serve([DEFAULT, WORK]);
  const onNewChatIn = vi.fn();
  render(<WithProjects onNewChatIn={onNewChatIn} />);
  fireEvent.click(
    await screen.findByRole("button", { name: "Start a conversation in Work" }),
  );
  expect(onNewChatIn).toHaveBeenCalledWith(WORK.id);
});

test("a new project is made from the panel and drawn, and a refusal is said", async () => {
  localStorage.clear();
  const fetch = serve([DEFAULT]);
  render(<WithProjects />);
  await screen.findByRole("list", { name: "Default" });

  fireEvent.click(screen.getByRole("button", { name: "New project" }));
  const box = screen.getByRole("textbox", { name: "Project name" });
  expect(box).toHaveFocus();
  fireEvent.change(box, { target: { value: "   " } });
  fireEvent.click(screen.getByRole("button", { name: "Create the project" }));
  expect(screen.getByRole("alert")).toHaveTextContent(
    "A project needs a name.",
  );

  fireEvent.change(box, { target: { value: "Refused" } });
  fireEvent.click(screen.getByRole("button", { name: "Create the project" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("not that name");

  fireEvent.change(box, { target: { value: "  Holidays " } });
  fireEvent.click(screen.getByRole("button", { name: "Create the project" }));
  expect(
    await screen.findByRole("list", { name: "Holidays" }),
  ).toHaveTextContent("No conversations yet.");
  expect(callsTo(fetch, "/api/projects", "POST").at(-1)?.body).toEqual({
    name: "Holidays",
  });
  expect(screen.getByRole("button", { name: "New project" })).toHaveFocus();
});

// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";

import { callsTo, json, refusal, stubFetch, type Call } from "../test/api";
import { LOADING, NewRunPage, NO_FLOWS } from "./NewRunPage";

const RUN = "11111111-1111-4111-8111-111111111111";

const CODE = {
  name: "code_review",
  title: "Code and review",
  has_form: true,
  inputs: [
    {
      name: "repo_branch",
      label: "Repository",
      type: "string",
      lov: "repo_branch",
      options: [
        { value: "https://git.example/app@main", label: "app · main" },
        { value: "https://git.example/app@next", label: "app · next" },
      ],
    },
    {
      name: "agent",
      label: "Agent",
      type: "string",
      lov: "agent",
      options: [{ value: "coder", label: "Coder · claude" }],
    },
    { name: "task", label: "Task", type: "string", lov: null, options: null },
  ],
};
const GAME = {
  name: "guess_number",
  title: "guess_number",
  has_form: false,
  inputs: [],
};

afterEach(() => {
  location.hash = "";
});

function answering(
  answer: (call: Call) => Response | undefined = () => undefined,
) {
  return stubFetch((call) => {
    const given = answer(call);
    if (given !== undefined) return given;
    if (call.url === "/api/flows") return json({ items: [CODE, GAME] });
    if (call.url === "/api/flows/code_review/runs")
      return json({ id: RUN }, 201);
    return refusal(404, "NotFound", "no such route here");
  });
}

test("New run lists the flows, each a link to its form", async () => {
  answering();
  render(<NewRunPage flow={null} />);
  expect(screen.getByText(LOADING)).toBeInTheDocument();
  const list = await screen.findByRole("list", { name: "Flows to start" });
  const links = within(list).getAllByRole("link");
  expect(links.map((link) => link.getAttribute("href"))).toEqual([
    "#/flows/new/code_review",
    "#/flows/new/guess_number",
  ]);
  expect(links[0]).toHaveTextContent("Code and review");
  expect(links[0]).toHaveTextContent("code_review · 3 inputs");
  expect(links[1]).toHaveTextContent("no inputs");
});

test("a folder with no flow says so", async () => {
  answering((call) =>
    call.url === "/api/flows" ? json({ items: [] }) : undefined,
  );
  render(<NewRunPage flow={null} />);
  expect(await screen.findByText(NO_FLOWS)).toBeInTheDocument();
});

test("a flow's form offers each list's values, and a run starts with the inputs as JSON", async () => {
  const fetch = answering();
  render(<NewRunPage flow="code_review" />);
  const form = await screen.findByRole("form", {
    name: "Start Code and review",
  });
  const repository = within(form).getByLabelText("Repository");
  expect(
    within(repository)
      .getAllByRole("option")
      .map((option) => option.textContent),
  ).toEqual(["Choose…", "app · main", "app · next"]);
  fireEvent.change(repository, {
    target: { value: "https://git.example/app@next" },
  });
  fireEvent.change(within(form).getByLabelText("Agent"), {
    target: { value: "coder" },
  });
  fireEvent.change(within(form).getByLabelText("Task"), {
    target: { value: "Fix the bug" },
  });
  fireEvent.click(within(form).getByRole("button", { name: "Start run" }));
  await screen.findByRole("button", { name: "Starting…" });
  await expect.poll(() => location.hash).toBe(`#/flows/runs/${RUN}`);
  expect(callsTo(fetch, "/api/flows/code_review/runs", "POST")).toEqual([
    {
      url: "/api/flows/code_review/runs",
      body: {
        inputs: {
          repo_branch: "https://git.example/app@next",
          agent: "coder",
          task: "Fix the bug",
        },
      },
    },
  ]);
});

test("a form not filled is not sent, and says what is missing", async () => {
  const fetch = answering();
  render(<NewRunPage flow="code_review" />);
  const form = await screen.findByRole("form", {
    name: "Start Code and review",
  });
  fireEvent.click(within(form).getByRole("button", { name: "Start run" }));
  const said = within(form).getByRole("alert");
  expect(
    within(said)
      .getAllByRole("listitem")
      .map((item) => item.textContent),
  ).toEqual([
    "Choose a value for Repository.",
    "Choose a value for Agent.",
    "Task needs a value.",
  ]);
  expect(callsTo(fetch, "/api/flows/code_review/runs", "POST")).toEqual([]);
});

test("the server's refusal is shown under the button, and nothing moves", async () => {
  answering((call) =>
    call.method === "POST"
      ? refusal(
          422,
          "InvalidInputs",
          "code_review: input 'agent': 'x' is not one",
        )
      : undefined,
  );
  render(<NewRunPage flow="code_review" />);
  const form = await screen.findByRole("form", {
    name: "Start Code and review",
  });
  fireEvent.change(within(form).getByLabelText("Repository"), {
    target: { value: "https://git.example/app@main" },
  });
  fireEvent.change(within(form).getByLabelText("Agent"), {
    target: { value: "coder" },
  });
  fireEvent.change(within(form).getByLabelText("Task"), {
    target: { value: "x" },
  });
  fireEvent.click(within(form).getByRole("button", { name: "Start run" }));
  expect(await within(form).findByRole("alert")).toHaveTextContent(
    "code_review: input 'agent': 'x' is not one",
  );
  expect(
    within(form).getByRole("button", { name: "Start run" }),
  ).toBeInTheDocument();
  expect(location.hash).toBe("");
});

test("a flow the folder does not hold is said so, with the way back", async () => {
  answering();
  render(<NewRunPage flow="nothing" />);
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "There is no flow named nothing.",
  );
  expect(screen.getByRole("link", { name: "See the flows" })).toHaveAttribute(
    "href",
    "#/flows/new",
  );
});

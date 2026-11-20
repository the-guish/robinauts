// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import { formOf } from "@neorc/ui";
import { afterEach, expect, test } from "vitest";

import { callsTo, json, refusal, stubFetch } from "../test/api";
import {
  a2uiForm,
  CATALOG,
  forgetFlows,
  formFor,
  searchOptions,
  type Flow,
  type FlowInput,
} from "./forms";

afterEach(forgetFlows);

const BRANCHES: FlowInput = {
  name: "repo_branch",
  label: "Repository",
  type: "string",
  live: false,
  lov: "repo_branch",
  options: [
    { value: "app@main", label: "app · main" },
    { value: "app@next", label: "app · next" },
  ],
};
const AGENTS: FlowInput = {
  name: "agent",
  label: "Agent",
  type: "string",
  live: false,
  lov: "agent",
  options: [{ value: "helper", label: "Helper", models: ["sonnet"] }],
};
const TASK: FlowInput = {
  name: "task",
  label: "Task",
  type: "string",
  live: false,
};
const REPOSITORY: FlowInput = {
  name: "repo",
  label: "Repository",
  type: "string",
  live: false,
  lov: "repository",
  options: [{ value: "app", label: "app" }],
};
const BRANCH: FlowInput = {
  name: "branch",
  label: "Branch",
  type: "string",
  live: true,
  lov: "branch",
  of: "repo",
};
const PROJECT: FlowInput = {
  name: "project",
  label: "Project",
  type: "string",
  live: true,
  lov: "project",
};

function flow(inputs: FlowInput[], has_form = true): Flow {
  return { name: "code_task", title: "Code a task", has_form, inputs };
}

/** The components of a form, by id. */
function components(messages: readonly unknown[] | null) {
  expect(messages).not.toBeNull();
  const [surface, update] = messages as [
    { version: string; createSurface: unknown },
    {
      version: string;
      updateComponents: { surfaceId: string; components: { id: string }[] };
    },
  ];
  expect(surface).toEqual({
    version: "v0.9",
    createSurface: { surfaceId: "code_task", catalogId: CATALOG },
  });
  expect(update.updateComponents.surfaceId).toBe("code_task");
  return new Map(update.updateComponents.components.map((c) => [c.id, c]));
}

test("a form is a column of its title and one control per input, in order", () => {
  const drawn = components(a2uiForm(flow([BRANCHES, AGENTS, TASK])));
  expect(drawn.get("root")).toEqual({
    id: "root",
    component: "Column",
    children: ["title", "input-repo_branch", "input-agent", "input-task"],
  });
  expect(drawn.get("title")).toEqual({
    id: "title",
    component: "Text",
    variant: "h3",
    text: "Code a task",
  });
});

test("every kind of input makes a form neorc's subset draws", () => {
  const count: FlowInput = {
    name: "count",
    label: "Count",
    type: "number",
    live: false,
  };
  const draft: FlowInput = {
    name: "draft",
    label: "Draft",
    type: "boolean",
    live: false,
  };
  const form = a2uiForm(flow([BRANCHES, AGENTS, TASK, count, draft]));
  expect(formOf({ form })).not.toBeNull();
});

test("a list of values is a choice of its values, filled, and required", () => {
  const drawn = components(a2uiForm(flow([BRANCHES, AGENTS])));
  expect(drawn.get("input-repo_branch")).toEqual({
    id: "input-repo_branch",
    component: "ChoicePicker",
    label: "Repository",
    variant: "mutuallyExclusive",
    options: [
      { value: "app@main", label: "app · main" },
      { value: "app@next", label: "app · next" },
    ],
    value: { path: "/repo_branch" },
    checks: [
      {
        condition: {
          call: "required",
          args: { value: { path: "/repo_branch" } },
        },
        message: "Choose a value for Repository.",
      },
    ],
  });
  // An agent's models are the backend's to check; the choice is the value.
  expect(drawn.get("input-agent")).toMatchObject({
    options: [{ value: "helper", label: "Helper" }],
  });
});

test.each([
  [
    TASK,
    {
      component: "TextField",
      variant: "longText",
      checks: [
        {
          condition: { call: "required", args: { value: { path: "/task" } } },
          message: "Task needs a value.",
        },
      ],
    },
  ],
  [
    { name: "tries", label: "Tries", type: "number", live: false },
    { component: "TextField", variant: "number" },
  ],
  [
    { name: "draft", label: "Draft", type: "boolean", live: false },
    { component: "CheckBox" },
  ],
])("an input of its type: %o", (input, expected) => {
  const drawn = components(a2uiForm(flow([input])));
  expect(drawn.get(`input-${input.name}`)).toMatchObject({
    label: input.label,
    value: { path: `/${input.name}` },
    ...expected,
  });
});

test.each([
  [{ ...BRANCHES, options: [] }],
  [{ name: "when", label: "When", type: "datetime", live: false }],
])("a form the subset cannot say is none: %o", (input) => {
  expect(a2uiForm(flow([TASK, input]))).toBeNull();
});

test("the source answers a listed flow's form, and none for the others", async () => {
  const fetch = stubFetch(() =>
    json({
      items: [
        flow([TASK]),
        { ...flow([TASK], false), name: "plain", title: "plain" },
      ],
    }),
  );
  const asked = (name: string) => formFor({ name, version: "1", content: {} });
  components(await asked("code_task"));
  expect(await asked("plain")).toBeNull();
  expect(await asked("elsewhere")).toBeNull();
  // The flows are read once.
  expect(callsTo(fetch, "/robinauts/api/flows")).toHaveLength(1);
});

test("a read that failed is asked again", async () => {
  let answered = 0;
  stubFetch(() =>
    answered++ === 0
      ? json({ error: "Unavailable", detail: "not yet" }, 503)
      : json({ items: [flow([TASK])] }),
  );
  const asked = () => formFor({ name: "code_task", version: "1", content: {} });
  await expect(asked()).rejects.toThrow("not yet");
  components(await asked());
});

test("a list that is searched is a choice that names it, after the input it is of", () => {
  const form = a2uiForm(flow([REPOSITORY, BRANCH, PROJECT]));
  const drawn = components(form);
  expect(drawn.get("input-branch")).toEqual({
    id: "input-branch",
    component: "ChoicePicker",
    label: "Branch",
    variant: "mutuallyExclusive",
    optionsFrom: { source: "branch", after: { path: "/repo" } },
    value: { path: "/branch" },
    checks: [
      {
        condition: { call: "required", args: { value: { path: "/branch" } } },
        message: "Choose a value for Branch.",
      },
    ],
  });
  expect(drawn.get("input-project")).toMatchObject({
    optionsFrom: { source: "project" },
  });
  // neorc draws it with a source of options, and only then.
  expect(formOf({ form }, undefined, { search: true })).not.toBeNull();
  expect(formOf({ form })).toBeNull();
});

test("a search asks the backend for the list the choice names", async () => {
  const fetch = stubFetch(() =>
    json({ items: [{ value: "main", label: "main" }], more: true }),
  );
  const found = await searchOptions({
    flow: "code_task",
    source: "branch",
    after: "app",
    query: "ma in",
    limit: 50,
    signal: new AbortController().signal,
  });
  await searchOptions({
    flow: "code_task",
    source: "project",
    after: null,
    query: "",
    limit: 10,
    signal: new AbortController().signal,
  });
  expect(found).toEqual({
    items: [{ value: "main", label: "main" }],
    more: true,
  });
  const [branch, project] = fetch.mock.calls.map(
    ([input]) => new URL(String(input), location.href),
  );
  expect(branch?.pathname).toBe("/robinauts/api/lovs/branch/options");
  expect(Object.fromEntries(branch?.searchParams ?? [])).toEqual({
    q: "ma in",
    limit: "50",
    after: "app",
  });
  expect(project?.pathname).toBe("/robinauts/api/lovs/project/options");
  expect(project?.searchParams.has("after")).toBe(false);
});

test("a search the backend refuses is refused in the backend's words", async () => {
  stubFetch(() =>
    refusal(
      502,
      "BadGateway",
      "the branches of app could not be read: its server did not answer",
    ),
  );
  await expect(
    searchOptions({
      flow: "code_task",
      source: "branch",
      after: "app",
      query: "",
      limit: 50,
      signal: new AbortController().signal,
    }),
  ).rejects.toThrow(
    /^the branches of app could not be read: its server did not answer$/,
  );
});

// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import { formOf } from "@neorc/ui";
import { afterEach, expect, test } from "vitest";

import { callsTo, json, stubFetch } from "../test/api";
import {
  a2uiForm,
  CATALOG,
  forgetFlows,
  formFor,
  type Flow,
  type FlowInput,
} from "./forms";

afterEach(forgetFlows);

const BRANCHES: FlowInput = {
  name: "repo_branch",
  label: "Repository",
  type: "string",
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
  lov: "agent",
  options: [{ value: "helper", label: "Helper", models: ["sonnet"] }],
};
const TASK: FlowInput = { name: "task", label: "Task", type: "string" };

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
  const count: FlowInput = { name: "count", label: "Count", type: "number" };
  const draft: FlowInput = { name: "draft", label: "Draft", type: "boolean" };
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
    { name: "tries", label: "Tries", type: "number" },
    { component: "TextField", variant: "number" },
  ],
  [
    { name: "draft", label: "Draft", type: "boolean" },
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
  [{ name: "when", label: "When", type: "datetime" }],
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

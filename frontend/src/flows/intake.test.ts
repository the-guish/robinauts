// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import { expect, test } from "vitest";

import {
  controlOf,
  emptyFields,
  inputsOf,
  shownInputs,
  withField,
  type Flow,
} from "./intake";

const FLOW: Flow = {
  name: "code_review",
  title: "Code and review",
  has_form: true,
  inputs: [
    {
      name: "repo_branch",
      label: "Repository",
      type: "string",
      lov: "repo_branch",
      options: [{ value: "app@main", label: "app · main" }],
    },
    { name: "task", label: "Task", type: "string", lov: null, options: null },
    { name: "tries", label: "Tries", type: "number", lov: null, options: null },
    {
      name: "draft",
      label: "Draft",
      type: "boolean",
      lov: null,
      options: null,
    },
    {
      name: "after",
      label: "After",
      type: "datetime",
      lov: null,
      options: null,
    },
  ],
};

test.each([
  ["repo_branch", "select"],
  ["task", "text"],
  ["tries", "number"],
  ["draft", "checkbox"],
  ["after", "datetime"],
])("the input %s is drawn as a %s", (name, control) => {
  const input = FLOW.inputs.find((each) => each.name === name);
  expect(input && controlOf(input)).toBe(control);
});

test("an untouched form is empty, its boxes unticked", () => {
  expect(emptyFields(FLOW)).toEqual({
    repo_branch: "",
    task: "",
    tries: "",
    draft: false,
    after: "",
  });
});

test("filled fields are the inputs, each in the JSON form the orchestrator takes", () => {
  const read = inputsOf(FLOW, {
    repo_branch: "app@main",
    task: "Fix the bug",
    tries: "3",
    draft: true,
    after: "2026-11-10T09:30",
  });
  expect(read).toEqual({
    inputs: {
      repo_branch: "app@main",
      task: "Fix the bug",
      tries: 3,
      draft: true,
      after: { $datetime: new Date("2026-11-10T09:30").toISOString() },
    },
  });
});

test("every input is required, and each field that is not one says why", () => {
  expect(
    inputsOf(FLOW, {
      repo_branch: "",
      task: "  ",
      tries: "three",
      draft: false,
      after: "not a time",
    }),
  ).toEqual({
    problems: [
      "Choose a value for Repository.",
      "Task needs a value.",
      "Tries is not a number.",
      "After is not a date and time.",
    ],
  });
});

const PAIRED: Flow = {
  name: "pair",
  title: "Pair",
  has_form: true,
  inputs: [
    {
      name: "agent",
      label: "Agent",
      type: "string",
      lov: "agent",
      options: [
        { value: "coder", label: "Coder", models: ["big", "small"] },
        { value: "chat", label: "Chat", models: ["small", "fast", "big"] },
      ],
    },
    {
      name: "model",
      label: "Model",
      type: "string",
      lov: "model",
      options: ["fast", "small", "big"].map((id) => ({ value: id, label: id })),
    },
  ],
};

test.each([
  ["", ["fast", "small", "big"]],
  ["coder", ["big", "small"]],
  ["chat", ["small", "fast", "big"]],
])("with agent %j chosen, the model list is %j", (agent, models) => {
  const shown = shownInputs(PAIRED, { agent, model: "" });
  expect(shown[1]?.options?.map((option) => option.value)).toEqual(models);
});

test.each([
  // [model before, agent chosen, model after]
  ["", "coder", "big"],
  ["small", "coder", "small"],
  ["fast", "coder", "big"],
  ["fast", "chat", "fast"],
  ["fast", "", "fast"],
])(
  "a model %j stays or gives way when agent %j is chosen: %j",
  (model, agent, after) => {
    const fields = withField(PAIRED, { agent: "", model }, "agent", agent);
    expect(fields).toEqual({ agent, model: after });
  },
);

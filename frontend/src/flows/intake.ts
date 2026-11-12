// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The calls behind the flows' forms, and the little logic between a form's
 * fields and the inputs a run starts with
 * (`docs/architecture/orchestration.md`, "Flows as forms").
 *
 * The server lists every flow of its flows folder with its inputs, and for
 * an input with a list of values the values it may take: the repositories'
 * branches, the models, the agents. A run is started with the inputs as
 * JSON, which the server checks against the form and hands to the
 * orchestrator as they came. Nothing here knows any one flow.
 */
import { request } from "../api/client";
import type { components } from "../api/schema";

export type Flow = components["schemas"]["FlowSummary"];
export type FlowInput = components["schemas"]["FlowInputSummary"];

/** What a field holds while a person fills it: a checkbox's state, or text. */
export type FieldValue = string | boolean;
export type Fields = Readonly<Record<string, FieldValue>>;

export const listFlows = (signal: AbortSignal) =>
  request("get", "/api/flows", { signal });

export const startRun = (flow: string, inputs: Record<string, unknown>) =>
  request("post", "/api/flows/{flow}/runs", {
    path: { flow },
    body: { inputs },
  });

/** The fields of a form nobody has touched: empty, and boxes unticked. */
export function emptyFields(flow: Flow): Fields {
  return Object.fromEntries(
    flow.inputs.map((input) => [
      input.name,
      input.type === "boolean" ? false : "",
    ]),
  );
}

/** The models of the agent chosen in the form's one agent field, if any. */
function agentModels(flow: Flow, fields: Fields): readonly string[] | null {
  const agents = flow.inputs.filter((input) => input.lov === "agent");
  const agent = agents.length === 1 ? agents[0] : undefined;
  if (agent === undefined) return null;
  const chosen = agent.options?.find((o) => o.value === fields[agent.name]);
  return chosen?.models ?? null;
}

/**
 * The form's inputs as its fields leave them: a model's list holds only the
 * models of the agent chosen beside it, in that agent's order. The server
 * refuses any other pair.
 */
export function shownInputs(flow: Flow, fields: Fields): FlowInput[] {
  const allowed = agentModels(flow, fields);
  return flow.inputs.map((input) => {
    const options = input.options;
    if (input.lov !== "model" || allowed === null || !options) return input;
    const narrowed = allowed.flatMap((id) =>
      options.filter((option) => option.value === id),
    );
    return { ...input, options: narrowed };
  });
}

/**
 * The fields with one changed. A model the chosen agent does not run on gives
 * way to that agent's default, the first of its models.
 */
export function withField(
  flow: Flow,
  fields: Fields,
  name: string,
  value: FieldValue,
): Fields {
  const next: Record<string, FieldValue> = { ...fields, [name]: value };
  const allowed = agentModels(flow, next);
  if (allowed === null) return next;
  for (const input of flow.inputs) {
    const model = next[input.name];
    if (input.lov !== "model" || typeof model !== "string") continue;
    if (!allowed.includes(model)) next[input.name] = allowed[0] ?? "";
  }
  return next;
}

/** What a field is drawn as. */
export type Control = "select" | "text" | "number" | "checkbox" | "datetime";

export function controlOf(input: FlowInput): Control {
  if (input.options !== null && input.options !== undefined) return "select";
  switch (input.type) {
    case "number":
      return "number";
    case "boolean":
      return "checkbox";
    case "datetime":
      return "datetime";
    default:
      return "text";
  }
}

/** The tag the orchestrator writes a datetime as, on the wire. */
const DATETIME_TAG = "$datetime";

/**
 * The inputs a run starts with, or what keeps the fields from being them,
 * one sentence per field. Every input a flow declares is required, as the
 * orchestrator has it. A number is a JSON number; a datetime, read in the
 * browser's time zone, is the orchestrator's tagged form in UTC.
 */
export function inputsOf(
  flow: Flow,
  fields: Fields,
):
  | { readonly inputs: Record<string, unknown> }
  | { readonly problems: readonly string[] } {
  const inputs: Record<string, unknown> = {};
  const problems: string[] = [];
  for (const input of flow.inputs) {
    const value = fields[input.name];
    const control = controlOf(input);
    if (control === "checkbox") {
      inputs[input.name] = value === true;
      continue;
    }
    const text = typeof value === "string" ? value : "";
    if (text.trim() === "") {
      problems.push(
        control === "select"
          ? `Choose a value for ${input.label}.`
          : `${input.label} needs a value.`,
      );
      continue;
    }
    if (control === "number") {
      const number = Number(text);
      if (!Number.isFinite(number)) {
        problems.push(`${input.label} is not a number.`);
        continue;
      }
      inputs[input.name] = number;
    } else if (control === "datetime") {
      const when = new Date(text);
      if (Number.isNaN(when.getTime())) {
        problems.push(`${input.label} is not a date and time.`);
        continue;
      }
      inputs[input.name] = { [DATETIME_TAG]: when.toISOString() };
    } else {
      inputs[input.name] = text;
    }
  }
  return problems.length > 0 ? { problems } : { inputs };
}

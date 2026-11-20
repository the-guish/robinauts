// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * Robinauts' forms to start a run, for neorc's root component: its source of
 * forms (oneberth's `neorc-in-robinauts.md`, "The form that starts a run").
 *
 * A flow of the flows folder may have a form file beside it,
 * `<flow>.form.toml`, with a title, a label per input and, for an input that
 * takes its value from a list, which list: the repositories' branches, the
 * agents or the models (`docs/architecture/orchestration.md`, "Flows as
 * forms"). The backend reads the file and fills the lists
 * (`GET /robinauts/api/flows`); this turns one flow's form into the A2UI v0.9
 * messages neorc draws, within the subset neorc accepts: a column of a
 * heading and one control per input.
 *
 * A list too long or too changing to list, a repository's branches or the
 * person's projects, is a choice whose options neorc asks for as the person
 * types (`optionsFrom`): `searchOptions` answers from
 * `GET /robinauts/api/lovs/<lov>/options`. A run of a flow with a form starts
 * here too (`startRun`), where the backend checks those choices anew.
 *
 * neorc draws its own form for a flow this answers `null` for: a flow with no
 * form file, one the backend does not list, and one this cannot say in the
 * subset -- an input with a list that is empty, or a date and time.
 */
import type {
  FormMessages,
  FormRequest,
  OptionPage,
  OptionRequest,
} from "@neorc/ui";

import { ApiError, request } from "../api/client";
import type { components } from "../api/schema";

export type { FormRequest, OptionRequest } from "@neorc/ui";

export type Flow = components["schemas"]["FlowSummary"];
export type FlowInput = components["schemas"]["FlowInputSummary"];

/** The catalogue neorc's forms are written against. */
export const CATALOG = "https://github.com/open-shipyard/neorc/forms/v1";

type Component = Record<string, unknown>;

/** A check that a field holds something, with what to say when it does not. */
function required(name: string, message: string): Component {
  return {
    condition: { call: "required", args: { value: { path: `/${name}` } } },
    message,
  };
}

/** The control of one input; `null` for one the subset cannot draw. */
function control(input: FlowInput): Component | null {
  const id = `input-${input.name}`;
  const value = { path: `/${input.name}` };
  if (input.live === true && input.lov !== null && input.lov !== undefined) {
    const after =
      input.of === null || input.of === undefined
        ? {}
        : { after: { path: `/${input.of}` } };
    return {
      id,
      component: "ChoicePicker",
      label: input.label,
      variant: "mutuallyExclusive",
      optionsFrom: { source: input.lov, ...after },
      value,
      checks: [required(input.name, `Choose a value for ${input.label}.`)],
    };
  }
  if (input.options !== null && input.options !== undefined) {
    // The subset draws a choice from one option up; an empty list is none.
    if (input.options.length === 0) return null;
    return {
      id,
      component: "ChoicePicker",
      label: input.label,
      variant: "mutuallyExclusive",
      options: input.options.map(({ value, label }) => ({ value, label })),
      value,
      checks: [required(input.name, `Choose a value for ${input.label}.`)],
    };
  }
  switch (input.type) {
    case "boolean":
      return { id, component: "CheckBox", label: input.label, value };
    case "number":
      return {
        id,
        component: "TextField",
        label: input.label,
        variant: "number",
        value,
      };
    case "datetime":
      return null;
    default:
      return {
        id,
        component: "TextField",
        label: input.label,
        variant: "longText",
        value,
        checks: [required(input.name, `${input.label} needs a value.`)],
      };
  }
}

/**
 * A flow's form as A2UI v0.9 messages: a surface, and its components, every
 * input a field of the data model under its own name, so that the form sends
 * the flow's inputs. `null` when an input cannot be drawn in neorc's subset.
 */
export function a2uiForm(flow: Flow): FormMessages | null {
  const controls: Component[] = [];
  for (const input of flow.inputs) {
    const drawn = control(input);
    if (drawn === null) return null;
    controls.push(drawn);
  }
  const heading: Component = {
    id: "title",
    component: "Text",
    variant: "h3",
    text: flow.title,
  };
  const root: Component = {
    id: "root",
    component: "Column",
    children: [heading.id, ...controls.map((each) => each.id)],
  };
  return [
    {
      version: "v0.9",
      createSurface: { surfaceId: flow.name, catalogId: CATALOG },
    },
    {
      version: "v0.9",
      updateComponents: {
        surfaceId: flow.name,
        components: [root, heading, ...controls],
      },
    },
  ];
}

/**
 * The form neorc asks for: the flow's, when the backend lists it with a form
 * file. The flows are fixed for the server's life, so one read serves every
 * flow; a read that failed is asked again next time.
 */
let flows: Promise<readonly Flow[]> | null = null;

export async function formFor(
  asked: FormRequest,
): Promise<FormMessages | null> {
  flows ??= request("get", "/robinauts/api/flows").then(
    (answer) => answer.items,
    (failure: unknown) => {
      flows = null;
      throw failure;
    },
  );
  const flow = (await flows).find((each) => each.name === asked.name);
  return flow?.has_form === true ? a2uiForm(flow) : null;
}

/** Back to a page that has read nothing. Nothing in the application calls this. */
export function forgetFlows(): void {
  flows = null;
}

/** A refusal as neorc shows one: the backend's own words. */
function said(failure: unknown): unknown {
  return failure instanceof ApiError ? new Error(failure.detail) : failure;
}

/**
 * The options neorc asks for, for a choice of a form that names its list
 * (`optionsFrom.source`, the input's `lov`): the values that match what the
 * person typed, a branch of the repository chosen or one of their projects.
 */
export async function searchOptions(asked: OptionRequest): Promise<OptionPage> {
  try {
    return await request("get", "/robinauts/api/lovs/{kind}/options", {
      path: { kind: asked.source },
      query: {
        q: asked.query,
        limit: asked.limit,
        ...(asked.after === null ? {} : { after: asked.after }),
      },
      signal: asked.signal,
    });
  } catch (failure) {
    throw said(failure);
  }
}

/**
 * A run of a flow Robinauts gave the form for, started through Robinauts:
 * the backend checks its inputs against the form, the searched lists as they
 * are now, then starts it at neorc as the person's.
 */
export async function startRun(
  flow: string,
  inputs: Record<string, unknown>,
): Promise<{ id: string }> {
  try {
    return await request("post", "/robinauts/api/flows/{flow}/runs", {
      path: { flow },
      body: { inputs },
    });
  } catch (failure) {
    throw said(failure);
  }
}

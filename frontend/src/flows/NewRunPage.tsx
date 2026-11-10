// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * "New run", at `#/flows/new` and `#/flows/new/<flow>`: this interface's own
 * pages for starting a flow, where the chat would be. The orchestrator's
 * framed page is for the runs (`./FlowsPage.tsx`); starting one is here, on
 * the server's forms (`./intake.ts`).
 *
 * The first lists the flows; the second is one flow's form: a choice from
 * its list for an input that has one -- a repository's branch, a model, an
 * agent and its worker -- and a field of its type for any other. Every
 * input is required. Once the run starts, the shell goes to it, in the
 * frame; a refusal is the server's own sentence, under the button.
 */
import { useEffect, useId, useState, type FormEvent } from "react";

import { detailOf } from "../api/client";
import { formatRoute, navigate } from "../router";
import {
  controlOf,
  emptyFields,
  inputsOf,
  listFlows,
  startRun,
  type FieldValue,
  type Fields,
  type Flow,
  type FlowInput,
} from "./intake";
import "./flows.css";

/** What the page says while the flows have not come yet. */
export const LOADING = "Loading the flows…";
/** What it says when the flows folder holds none. */
export const NO_FLOWS = "The flows folder holds no flow.";

/** Under a list of values the configuration leaves empty. */
export const NOTHING_TO_CHOOSE =
  "Nothing to choose from: the configuration offers no value for this list.";

export function NewRunPage({ flow }: Readonly<{ flow: string | null }>) {
  const [flows, setFlows] = useState<readonly Flow[] | null>(null);
  const [refused, setRefused] = useState<string | null>(null);
  useEffect(() => {
    const asking = new AbortController();
    listFlows(asking.signal)
      .then((answer) => {
        setFlows(answer.items);
      })
      .catch((failure: unknown) => {
        if (!asking.signal.aborted) setRefused(detailOf(failure));
      });
    return () => {
      asking.abort();
    };
  }, []);
  const chosen =
    flow === null || flows === null
      ? undefined
      : flows.find((each) => each.name === flow);
  return (
    <div className="flows-scroll">
      <div className="flows-page">
        {refused !== null ? (
          <>
            <h1 className="flows-title">New run</h1>
            <p role="alert" className="flows-refused">
              {refused}
            </p>
          </>
        ) : flows === null ? (
          <>
            <h1 className="flows-title">New run</h1>
            <p className="flows-note">{LOADING}</p>
          </>
        ) : flow === null ? (
          <FlowList flows={flows} />
        ) : chosen === undefined ? (
          <>
            <h1 className="flows-title">New run</h1>
            <p role="alert" className="flows-refused">
              There is no flow named {flow}.
            </p>
            <a href={formatRoute({ kind: "flows", view: { at: "new" } })}>
              See the flows
            </a>
          </>
        ) : (
          <RunForm key={chosen.name} flow={chosen} />
        )}
      </div>
    </div>
  );
}

function FlowList({ flows }: Readonly<{ flows: readonly Flow[] }>) {
  return (
    <>
      <h1 className="flows-title">New run</h1>
      {flows.length === 0 ? (
        <p className="flows-note">{NO_FLOWS}</p>
      ) : (
        <ul aria-label="Flows to start" className="flows-cards">
          {flows.map((flow) => (
            <li key={flow.name}>
              <a
                href={formatRoute({
                  kind: "flows",
                  view: { at: "start", flow: flow.name },
                })}
                className="flows-card"
              >
                <span className="flows-card-title">{flow.title}</span>
                <span className="flows-card-detail">
                  {flow.title === flow.name ? "" : `${flow.name} · `}
                  {inputCount(flow.inputs.length)}
                </span>
              </a>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

function inputCount(count: number): string {
  if (count === 0) return "no inputs";
  return count === 1 ? "1 input" : `${count} inputs`;
}

function RunForm({ flow }: Readonly<{ flow: Flow }>) {
  const [fields, setFields] = useState<Fields>(() => emptyFields(flow));
  const [busy, setBusy] = useState(false);
  const [problems, setProblems] = useState<readonly string[]>([]);
  const problemsId = useId();
  const set = (name: string, value: FieldValue) => {
    setFields((was) => ({ ...was, [name]: value }));
  };
  const start = async (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    const read = inputsOf(flow, fields);
    if ("problems" in read) {
      setProblems(read.problems);
      return;
    }
    setBusy(true);
    setProblems([]);
    try {
      const run = await startRun(flow.name, read.inputs);
      navigate({ kind: "flows", view: { at: "run", run: run.id } });
    } catch (failure) {
      setProblems([detailOf(failure)]);
      setBusy(false);
    }
  };
  return (
    <>
      <div className="flows-head">
        <h1 className="flows-title">{flow.title}</h1>
        <a
          href={formatRoute({
            kind: "flows",
            view: { at: "flow", flow: flow.name },
          })}
          className="flows-aside"
        >
          See {flow.name}
        </a>
      </div>
      <form
        aria-label={`Start ${flow.title}`}
        aria-describedby={problems.length > 0 ? problemsId : undefined}
        noValidate
        onSubmit={(event) => void start(event)}
        className="flows-form"
      >
        {flow.inputs.length === 0 && (
          <p className="flows-note">This flow takes no inputs.</p>
        )}
        {flow.inputs.map((input) => (
          <Field
            key={input.name}
            input={input}
            value={fields[input.name] ?? ""}
            busy={busy}
            onChange={(value) => {
              set(input.name, value);
            }}
          />
        ))}
        {problems.length > 0 && (
          <ul id={problemsId} role="alert" className="flows-refused">
            {problems.map((problem) => (
              <li key={problem}>{problem}</li>
            ))}
          </ul>
        )}
        <div className="flows-actions">
          <button
            type="submit"
            aria-disabled={busy}
            className="flows-button flows-primary"
          >
            {busy ? "Starting…" : "Start run"}
          </button>
        </div>
      </form>
    </>
  );
}

function Field({
  input,
  value,
  busy,
  onChange,
}: Readonly<{
  input: FlowInput;
  value: FieldValue;
  busy: boolean;
  onChange: (value: FieldValue) => void;
}>) {
  const id = useId();
  const noteId = useId();
  const control = controlOf(input);
  const empty = control === "select" && (input.options ?? []).length === 0;
  const text = typeof value === "string" ? value : "";
  if (control === "checkbox") {
    return (
      <div className="flows-check">
        <input
          id={id}
          type="checkbox"
          checked={value === true}
          disabled={busy}
          onChange={(event) => {
            onChange(event.target.checked);
          }}
        />
        <label htmlFor={id} className="flows-label">
          {input.label}
        </label>
      </div>
    );
  }
  return (
    <div className="flows-field">
      <label htmlFor={id} className="flows-label">
        {input.label}
      </label>
      {control === "select" ? (
        <>
          <select
            id={id}
            value={text}
            disabled={busy}
            required
            aria-describedby={empty ? noteId : undefined}
            onChange={(event) => {
              onChange(event.target.value);
            }}
            className="flows-input"
          >
            <option value="">Choose…</option>
            {(input.options ?? []).map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
          {empty && (
            <p id={noteId} className="flows-note">
              {NOTHING_TO_CHOOSE}
            </p>
          )}
        </>
      ) : control === "text" ? (
        <textarea
          id={id}
          value={text}
          rows={3}
          readOnly={busy}
          required
          onChange={(event) => {
            onChange(event.target.value);
          }}
          className="flows-input flows-text"
        />
      ) : (
        <input
          id={id}
          type={control === "number" ? "number" : "datetime-local"}
          step={control === "number" ? "any" : undefined}
          value={text}
          readOnly={busy}
          required
          onChange={(event) => {
            onChange(event.target.value);
          }}
          className="flows-input"
        />
      )}
    </div>
  );
}

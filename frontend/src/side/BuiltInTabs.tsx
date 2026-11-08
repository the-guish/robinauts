// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The two tabs every conversation can open: "Context window" and "Usage"
 * (`docs/specs/frontend.md`).
 *
 * Both read only what the interface already has: the thread on the screen
 * (`./transcript.ts`) and the list of models. Neither asks the backend
 * anything, and neither says more than that allows: the tokens are an
 * estimate, and a window the deployment does not state is "unknown".
 */
import type { Models } from "../shell/ModelPicker";
import {
  counted,
  type TranscriptStore,
  usageOf,
  useTranscript,
} from "./transcript";

/** The model a conversation runs on, as the tab names it. */
export interface ShownModel {
  readonly id: string;
  readonly title: string;
}

/** The model's title from the list, or its id while there is none. */
export function shownModel(
  models: Models,
  id: string | null,
): ShownModel | null {
  if (id === null) return null;
  const listed =
    models.status === "ready"
      ? models.items.find((model) => model.id === id)
      : undefined;
  return { id, title: listed?.title ?? id };
}

/**
 * How many tokens the model's context window holds, if the deployment says.
 *
 * `GET /api/models` does not say it today: the operator's
 * `context_window` is read by the engines and stops there. The field is
 * looked for rather than assumed, so the meter appears the day the answer
 * carries a positive whole number under that name, and until then the tab
 * says "unknown".
 */
export function contextWindowOf(
  models: Models,
  id: string | null,
): number | null {
  if (id === null || models.status !== "ready") return null;
  const model = models.items.find((one) => one.id === id);
  if (model === undefined || !("context_window" in model)) return null;
  const window: unknown = model.context_window;
  return typeof window === "number" && Number.isInteger(window) && window > 0
    ? window
    : null;
}

const ESTIMATE_NOTE =
  "An estimate: about four characters to a token, counted over the thread on the screen.";

export function ContextWindowTab({
  transcript,
  model,
  contextWindow,
}: Readonly<{
  transcript: TranscriptStore;
  model: ShownModel | null;
  contextWindow: number | null;
}>) {
  const { estimatedTokens } = usageOf(useTranscript(transcript));
  const share =
    contextWindow === null
      ? null
      : Math.min(100, Math.round((estimatedTokens / contextWindow) * 100));
  return (
    <div className="side-builtin">
      <dl className="side-facts">
        <div className="side-fact">
          <dt>Model</dt>
          <dd>{model === null ? "None chosen" : model.title}</dd>
        </div>
        <div className="side-fact">
          <dt>Window</dt>
          <dd data-window="">
            {contextWindow === null
              ? "unknown"
              : `${counted(contextWindow)} tokens`}
          </dd>
        </div>
        <div className="side-fact">
          <dt>In the thread</dt>
          <dd data-estimate="">about {counted(estimatedTokens)} tokens</dd>
        </div>
      </dl>
      {contextWindow !== null && share !== null && (
        <div
          role="meter"
          aria-label="Context used"
          aria-valuemin={0}
          aria-valuemax={contextWindow}
          aria-valuenow={Math.min(estimatedTokens, contextWindow)}
          aria-valuetext={`about ${counted(estimatedTokens)} of ${counted(contextWindow)} tokens, ${String(share)}%`}
          className="side-meter"
        >
          <div
            className="side-meter-fill"
            // The one value here that is genuinely dynamic.
            style={{ width: `${String(share)}%` }}
          />
        </div>
      )}
      <p className="side-note">{ESTIMATE_NOTE}</p>
      <p className="side-note">
        {contextWindow === null
          ? "The deployment does not say how large this model's window is. "
          : ""}
        What the model is sent also holds the agent&apos;s instructions and its
        tools, which the interface does not see; when the window fills, the
        engine trims or summarises the oldest exchanges.
      </p>
    </div>
  );
}

export function UsageTab({
  transcript,
}: Readonly<{ transcript: TranscriptStore }>) {
  const usage = usageOf(useTranscript(transcript));
  // Term, value, and whether it is a part of the line above it.
  const rows: readonly (readonly [string, string, boolean])[] = [
    ["Messages", counted(usage.questions + usage.answers), false],
    ["Questions", counted(usage.questions), true],
    ["Answers", counted(usage.answers), true],
    ["Tool calls", counted(usage.toolCalls), false],
    ["Characters", counted(usage.characters), false],
    ["of text", counted(usage.text), true],
    ["of reasoning", counted(usage.reasoning), true],
    ["to and from tools", counted(usage.tools), true],
    ["Tokens (estimate)", `about ${counted(usage.estimatedTokens)}`, false],
  ];
  return (
    <div className="side-builtin">
      <dl className="side-facts">
        {rows.map(([term, value, part]) => (
          <div
            key={term}
            className={part ? "side-fact side-fact-part" : "side-fact"}
          >
            <dt>{term}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
      <p className="side-note">{ESTIMATE_NOTE}</p>
      <p className="side-note">
        Recording what each turn really used is planned and not built yet, so
        this is what the conversation&apos;s transcript tells, not what a
        provider billed.
      </p>
    </div>
  );
}

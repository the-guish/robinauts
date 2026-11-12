// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * Which model answers (`docs/specs/agents.md`).
 *
 * Unlike the agent, the model stays a choice once a conversation exists, so
 * the picker is in two places: beside the agent picker on the empty chat,
 * where it says what the first message will run on, and on the line under an
 * open conversation's title, where changing it moves the conversation to
 * another model from its next turn on.
 *
 * There is no "Default" entry. An agent's model is a default, copied into a
 * conversation when it starts, so the list is the models and the agent's
 * default is simply the one selected on a new chat.
 */
import { useRef, useState } from "react";

import { detailOf, isRefusal, request, UNKNOWN_MODEL } from "../api/client";
import type { components } from "../api/schema";
import type { ConversationId } from "../chat";
import { type Conversation, setModel } from "../conversation/conversation";
import { type Offered, useOffered } from "./offered";
import { forget, remember, remembered } from "./storage";
import "./picker.css";

export type Model = components["schemas"]["ModelSummary"];

export const MODEL_KEY = "model";

/** What this deployment offers, as far as the one call has got. */
export type Models = Offered<Model>;

const askModels = (signal: AbortSignal) =>
  request("get", "/api/models", { signal });

/**
 * The models, asked for once.
 *
 * By the shell, like the agents (`./AgentPicker.tsx`): the empty chat is
 * remounted on every "New chat", and the open conversation's line needs the
 * list as well.
 */
export function useModels(round = 0): Models {
  return useOffered(askModels, round);
}

/**
 * The models an agent's turns may run on, in the agent's order: the backend
 * refuses any other. The whole list while the agent is not known.
 */
export function modelsOf(
  models: Models,
  listed: readonly string[] | null,
): Models {
  if (models.status !== "ready" || listed === null) return models;
  const items = listed.flatMap((id) =>
    models.items.filter((model) => model.id === id),
  );
  return { status: "ready", items };
}

/** What a model is called in a list that no longer has it. */
export function unoffered(id: string): string {
  return `${id} (no longer offered)`;
}

/** Whether the deployment still offers that model; unknown until it says. */
export function isOffered(models: Models, id: string): boolean | null {
  if (models.status !== "ready") return null;
  return models.items.some((model) => model.id === id);
}

/**
 * Which model a first message would run on, and how to change it.
 *
 * **What this browser picked, if the deployment still offers it; otherwise
 * the chosen agent's default.** So until somebody picks a model, switching
 * agent on the empty chat switches to that agent's default as well; once a
 * model has been picked it is kept across agents, as the agent is kept across
 * new chats. Nothing is remembered until somebody picks one.
 *
 * **While the models have not arrived, or could not be**, it is the
 * remembered one as it is, unchecked -- somebody who picked a model is not
 * to be answered by another because a list was slow -- and `null` for
 * anyone who never picked, which the backend takes as the agent's default
 * (`docs/specs/wire.md`). A remembered model the deployment no longer
 * offers is then refused by the backend, by name, and the shell forgets it
 * (the third of what this returns), so the next message is not refused for
 * it again. So is one picked from a list that has gone stale.
 */
export function useChosenModel(
  models: Models,
  agentDefault: string | null,
): [string | null, (id: string) => void, (id: string) => void] {
  const [kept, setKept] = useState<string | null>(() => remembered(MODEL_KEY));
  let chosen: string | null = kept;
  if (models.status === "ready") {
    const offered = (id: string | null) =>
      id !== null && isOffered(models, id) === true;
    // The first of the list only where the agent's default is not offered,
    // which the backend refuses at start-up: a picker has to show something.
    chosen = offered(kept)
      ? kept
      : offered(agentDefault)
        ? agentDefault
        : (models.items[0]?.id ?? null);
  }
  return [
    chosen,
    (id: string) => {
      setKept(id);
      remember(MODEL_KEY, id);
    },
    (id: string) => {
      if (id !== kept) return;
      setKept(null);
      forget(MODEL_KEY);
    },
  ];
}

/**
 * The `<select>` itself, over the list the deployment offers.
 *
 * A `chosen` the list does not have -- a conversation whose model the
 * operator has since removed -- is shown as it is, marked, and cannot be
 * picked again: the conversation's next turn is refused until another is
 * (`docs/specs/agents.md`), and a picker that quietly showed some other model
 * would hide exactly that.
 */
export function ModelPicker({
  models,
  chosen,
  onChoose,
  busy = false,
}: Readonly<{
  models: Models;
  chosen: string | null;
  onChoose: (id: string) => void;
  /** A change is being saved. */
  busy?: boolean;
}>) {
  if (models.status === "loading") {
    return <p className="picker-note">Loading the models…</p>;
  }
  if (models.status === "failed") {
    return (
      <p role="alert" className="picker-failed">
        The models could not be loaded: {models.detail}
      </p>
    );
  }
  const stale = chosen !== null && isOffered(models, chosen) === false;
  return (
    <label className="picker">
      <span>Model</span>
      <select
        value={chosen ?? ""}
        // Never disabled while a change is saved: a disabled control drops
        // the focus, and a keyboard stepping through the list is still
        // choosing (`ConversationModel`).
        aria-busy={busy}
        onChange={(event) => {
          onChoose(event.target.value);
        }}
        className="picker-select"
      >
        {stale && (
          <option value={chosen} disabled>
            {unoffered(chosen)}
          </option>
        )}
        {models.items.map((model) => (
          <option key={model.id} value={model.id}>
            {model.title}
          </option>
        ))}
      </select>
    </label>
  );
}

/** What a change the backend would not take is told as. */
export const NOT_OFFERED =
  "that model is not one this deployment offers any more";

/** The sentence for a refused change, in the picker's own words where it has any. */
function refusedWith(failure: unknown): string {
  // The backend's detail for it names the field of the request, for whoever
  // reads the log; the one value in it is the model.
  if (isRefusal(failure, UNKNOWN_MODEL)) return NOT_OFFERED;
  return detailOf(failure);
}

/**
 * The picker on an open conversation's line: a change is a `PUT`.
 *
 * Allowed while a run is going: that run keeps the model it started with and
 * the next turn uses the new one. A refusal is said here, beside the picker,
 * which is where it was asked from -- as a rename's is said on its row -- and
 * the picker goes back to the model the conversation still has.
 *
 * **Where the person lands is what is saved.** A keyboard stepping through a
 * closed `<select>` changes it at every step, so a change made while another
 * is being saved is not dropped: the latest one waits, and is sent when the
 * first has answered -- unless it is what the conversation is then on. The
 * steps in between are never sent.
 *
 * The shell keys this by the conversation, so a change in the air and a
 * refusal do not follow the page to another one.
 */
export function ConversationModel({
  models,
  conversationId,
  model,
  onMoved,
  onNotOffered,
}: Readonly<{
  models: Models;
  conversationId: ConversationId;
  model: string;
  /** The conversation as the change left it. */
  onMoved: (moved: Conversation) => void;
  /** A change refused because the deployment no longer offers the model. */
  onNotOffered?: () => void;
}>) {
  // What the picker shows while a change is being saved: the latest asked for.
  const [moving, setMoving] = useState<string | null>(null);
  const [refused, setRefused] = useState<string | null>(null);
  // Refs and not state: they are read by the loop below across its awaits,
  // where state would be what it was when the loop began.
  const saving = useRef(false);
  const waiting = useRef<string | null>(null);

  const move = async (modelId: string) => {
    setRefused(null);
    if (saving.current) {
      waiting.current = modelId;
      setMoving(modelId);
      return;
    }
    let has = model;
    let next: string | null = modelId;
    saving.current = true;
    try {
      while (next !== null && next !== has) {
        waiting.current = null;
        setMoving(next);
        let moved: Conversation;
        try {
          moved = await setModel(conversationId, next);
        } catch (failure) {
          // What waits was chosen over a model that did not take; the
          // picker goes back to the one the conversation still has.
          setRefused(refusedWith(failure));
          if (isRefusal(failure, UNKNOWN_MODEL)) onNotOffered?.();
          waiting.current = null;
          return;
        }
        has = moved.model;
        onMoved(moved);
        next = waiting.current;
      }
    } finally {
      saving.current = false;
      setMoving(null);
    }
  };

  // Without the list there is nothing to pick from, but the line still says
  // which model the conversation is on: that much the conversation told us.
  if (models.status !== "ready") {
    return (
      <>
        <p className="picker-note">
          Model <span className="picker-value">{model}</span>
        </p>
        {models.status === "failed" && (
          <p role="alert" className="picker-failed">
            The models could not be loaded: {models.detail}
          </p>
        )}
      </>
    );
  }

  return (
    <>
      <ModelPicker
        models={models}
        chosen={moving ?? model}
        busy={moving !== null}
        onChoose={(id) => {
          void move(id);
        }}
      />
      {refused !== null && (
        <p role="alert" className="picker-failed picker-line">
          The model could not be changed: {refused}
        </p>
      )}
    </>
  );
}

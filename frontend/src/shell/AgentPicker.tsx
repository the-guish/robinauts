// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * Which agent the next conversation is with (`docs/specs/agents.md`).
 *
 * It shows on an empty chat and nowhere else: once a conversation exists,
 * the agent is a fact about it rather than a choice.
 *
 * The choice is remembered per browser, and checked against what the
 * deployment still offers: an agent removed from the configuration is not
 * one this picker will quietly keep selecting.
 */
import { useState } from "react";

import { request } from "../api/client";
import type { components } from "../api/schema";
import { type Offered, useOffered } from "./offered";
import { remember, remembered } from "./storage";
import "./picker.css";

export type Agent = components["schemas"]["AgentSummary"];

export const AGENT_KEY = "agent";

/** What this deployment offers, as far as the one call has got. */
export type Agents = Offered<Agent>;

const askAgents = (signal: AbortSignal) =>
  request("get", "/api/agents", { signal });

/**
 * The agents, asked for once.
 *
 * Called by the shell rather than by the picker, because the picker lives
 * inside the empty chat, which is remounted every time "New chat" is
 * pressed. Fetching from there would mean a call per click for a list that
 * only changes when somebody edits the configuration and restarts the
 * server.
 */
export function useAgents(): Agents {
  return useOffered(askAgents);
}

/** The remembered agent if it is still offered, otherwise the first one. */
function chosenFrom(items: Agent[], kept: string | null): string | null {
  const first = items[0];
  if (first === undefined) return null;
  return items.some((agent) => agent.id === kept) && kept !== null
    ? kept
    : first.id;
}

/**
 * Which agent a first message would go to, and how to change it.
 *
 * **Held above the picker**, because it is not only the picker's business:
 * the chat needs it to begin a conversation (`src/chat/index.ts`), and the
 * picker is what the chat shows above its box. It is the shell that has both
 * of them in view, so the shell holds the choice.
 *
 * `null` while the agents have not arrived, and where the deployment has
 * none -- which is a deployment with nobody to talk to, not a choice nobody
 * has made yet.
 */
export function useChosenAgent(
  agents: Agents,
): [string | null, (id: string) => void] {
  const [kept, setKept] = useState<string | null>(() => remembered(AGENT_KEY));
  const chosen =
    agents.status === "ready" ? chosenFrom(agents.items, kept) : null;
  return [
    chosen,
    (id: string) => {
      setKept(id);
      remember(AGENT_KEY, id);
    },
  ];
}

export function AgentPicker({
  agents,
  chosen,
  onChoose,
}: Readonly<{
  agents: Agents;
  chosen: string | null;
  onChoose: (id: string) => void;
}>) {
  if (agents.status === "loading") {
    return <p className="picker-note">Loading the agents…</p>;
  }
  if (agents.status === "failed") {
    return (
      <p role="alert" className="picker-failed">
        The agents could not be loaded: {agents.detail}
      </p>
    );
  }
  if (agents.items.length === 0) {
    return (
      <div className="picker-none">
        <select aria-label="Agent" disabled className="picker-select">
          <option>No agent</option>
        </select>
        <p role="alert" className="picker-note">
          This deployment has no agent configured, so there is nobody to talk
          to. Whoever runs it adds one to the configuration file.
        </p>
      </div>
    );
  }
  return (
    <label className="picker">
      <span>Agent</span>
      <select
        value={chosen ?? ""}
        onChange={(event) => {
          onChoose(event.target.value);
        }}
        className="picker-select"
      >
        {agents.items.map((agent) => (
          <option key={agent.id} value={agent.id}>
            {agent.title}
          </option>
        ))}
      </select>
    </label>
  );
}

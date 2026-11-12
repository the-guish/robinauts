// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import {
  act,
  fireEvent,
  render,
  renderHook,
  screen,
} from "@testing-library/react";
import { expect, test, vi } from "vitest";

import {
  AGENT_KEY,
  AgentPicker,
  useAgents,
  useChosenAgent,
  type Agent,
  type Agents,
} from "./AgentPicker";

const AGENTS: Agent[] = [
  {
    id: "helper",
    title: "Helper",
    engine: "langgraph",
    model: "sonnet",
    models: ["sonnet"],
  },
  {
    id: "writer",
    title: "Writer",
    engine: "pydantic-ai",
    model: "sonnet",
    models: ["sonnet"],
  },
];

const ready = (items: Agent[]): Agents => ({ status: "ready", items });

const kept = () => localStorage.getItem(`robinauts.${AGENT_KEY}`);

/**
 * The picker with the choice above it, which is where the choice lives.
 *
 * The shell holds it (`useChosenAgent`), because the chat needs the same
 * answer to begin a conversation; drawing the pair together is what a test
 * of the picker is really about.
 */
function Picking({ agents }: { agents: Agents }) {
  const [chosen, choose] = useChosenAgent(agents);
  return <AgentPicker agents={agents} chosen={chosen} onChoose={choose} />;
}

test("no agent at all: nothing to pick, and a reason why", () => {
  render(<Picking agents={ready([])} />);
  expect(screen.getByLabelText("Agent")).toBeDisabled();
  expect(screen.getByRole("alert")).toHaveTextContent(/no agent configured/);
});

test("more than one: a picker, and the choice is remembered", () => {
  render(<Picking agents={ready(AGENTS)} />);
  const picker = screen.getByLabelText("Agent");
  expect(picker).toHaveValue("helper");
  fireEvent.change(picker, { target: { value: "writer" } });
  expect(picker).toHaveValue("writer");
  expect(kept()).toBe("writer");
});

test("the agent a first message would go to is the chosen one", () => {
  const { result } = renderHook(() => useChosenAgent(ready(AGENTS)));
  expect(result.current[0]).toBe("helper");
  // Nothing has arrived yet, and nothing has been chosen: there is no agent
  // to begin a conversation with rather than a first one to guess at.
  const loading = renderHook(() => useChosenAgent({ status: "loading" }));
  expect(loading.result.current[0]).toBeNull();
  const none = renderHook(() => useChosenAgent(ready([])));
  expect(none.result.current[0]).toBeNull();
});

test("what this browser chose is what it opens with", () => {
  localStorage.setItem(`robinauts.${AGENT_KEY}`, "writer");
  render(<Picking agents={ready(AGENTS)} />);
  expect(screen.getByLabelText("Agent")).toHaveValue("writer");
});

test("an agent the deployment no longer offers is not kept selected", () => {
  localStorage.setItem(`robinauts.${AGENT_KEY}`, "gone");
  render(<Picking agents={ready(AGENTS)} />);
  expect(screen.getByLabelText("Agent")).toHaveValue("helper");
});

test("while they are being fetched, and when they cannot be", () => {
  const { unmount } = render(<Picking agents={{ status: "loading" }} />);
  expect(screen.getByText("Loading the agents…")).toBeVisible();
  unmount();
  render(<Picking agents={{ status: "failed", detail: "no network" }} />);
  expect(screen.getByRole("alert")).toHaveTextContent("no network");
});

test("the list is asked for once, and a re-render is not a second ask", async () => {
  const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
    new Response(JSON.stringify({ items: AGENTS }), {
      status: 200,
      headers: { "content-type": "application/json" },
    }),
  );
  vi.stubGlobal("fetch", fetch);
  const { result, rerender } = renderHook(() => useAgents());
  await act(async () => {
    await Promise.resolve();
  });
  expect(result.current).toEqual({ status: "ready", items: AGENTS });
  // A re-render is not a second call: the shell holds this, so that "New
  // chat" -- which remounts the picker -- costs nothing.
  rerender();
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(fetch.mock.calls[0]?.[0]).toBe("/api/agents");
});

test("a refusal becomes something to show, not a silence", async () => {
  vi.stubGlobal(
    "fetch",
    vi
      .fn<typeof globalThis.fetch>()
      .mockRejectedValue(new TypeError("no network")),
  );
  const { result } = renderHook(() => useAgents());
  await act(async () => {
    await Promise.resolve();
  });
  expect(result.current.status).toBe("failed");
  expect(
    result.current.status === "failed" ? result.current.detail : "",
  ).toContain("no network");
});

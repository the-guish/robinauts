// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { expect, test, vi } from "vitest";

import type { ThreadToolCall } from "../chat";
import { json, refusal, stubFetch, type Call } from "../test/api";
import { START_TIMEOUT_MS } from "./AppFrame";
import { AppTab, type Asked } from "./AppTab";
import { type App, subTabKey, subTabsOf } from "./apps";
import { PROTOCOL_VERSION } from "./bridge";

const APP: App = {
  id: "draw/ui://diagrams/view.html",
  server: "draw",
  view: "ui://diagrams/view.html",
  title: "Diagrams",
  tools: [{ name: "show_diagram", title: "Diagram" }],
};

const CONVERSATION = "0b7e0000-0000-4000-8000-000000000001";

const DRAWN = {
  content: [{ type: "text", text: "Two boxes." }],
  structuredContent: { nodes: [{ id: "a" }, { id: "b" }], revision: 1 },
  isError: false,
};

function shown(
  callId: string,
  diagram: string,
  more: Partial<ThreadToolCall> = {},
): ThreadToolCall {
  return {
    messageId: "m2",
    callId,
    name: "show_diagram",
    arguments: { diagram_id: diagram },
    state: "answered",
    live: true,
    ...more,
  };
}

function answering(answer: (call: Call) => Response = () => json(DRAWN)) {
  return stubFetch(answer);
}

/** A sub-tab's frame: the sub-tab itself has the same title. */
function frameOf(title: string) {
  const frame = document.querySelector<HTMLIFrameElement>(
    `iframe[title="${title}"]`,
  );
  if (frame === null) throw new Error(`no frame titled ${title}`);
  return frame;
}

function tabOf(calls: ThreadToolCall[], asked: Asked | null = null) {
  return (
    <AppTab
      agent="planner"
      app={APP}
      conversation={CONVERSATION}
      subTabs={subTabsOf(APP, calls)}
      asked={asked}
    />
  );
}

/** The tab with one sub-tab, its frame, and what the frame's window is posted. */
function tab(calls: ThreadToolCall[] = [shown("c1", "a")]) {
  const drawn = render(tabOf(calls));
  const frame = frameOf("Diagram 1");
  const view = frame.contentWindow;
  if (view === null) throw new Error("the frame has no window");
  const posted = vi.spyOn(view, "postMessage");
  return { ...drawn, frame, view, posted };
}

/**
 * A message from `source`, as the browser would hand it to the page: a
 * sandboxed frame's origin is opaque, and spelt `"null"`.
 */
function from(source: Window, data: unknown, origin = "null") {
  act(() => {
    window.dispatchEvent(new MessageEvent("message", { data, source, origin }));
  });
}

const sent = (posted: ReturnType<typeof tab>["posted"]) =>
  posted.mock.calls.map(([message, target]) => {
    // Always "*": an opaque origin has no other name.
    expect(target).toBe("*");
    return message as Record<string, unknown>;
  });

function handshake(view: Window) {
  from(view, {
    jsonrpc: "2.0",
    id: 1,
    method: "ui/initialize",
    params: { protocolVersion: PROTOCOL_VERSION },
  });
  from(view, {
    jsonrpc: "2.0",
    method: "ui/notifications/initialized",
    params: {},
  });
}

async function started(calls?: ThreadToolCall[]) {
  const drawn = tab(calls);
  handshake(drawn.view);
  await waitFor(() => {
    expect(sent(drawn.posted)).toHaveLength(3);
  });
  return drawn;
}

test("the view is framed sandboxed, with scripts alone, and hidden until it is told a result", () => {
  answering();
  const { frame } = tab();
  expect(frame).toHaveAttribute(
    "src",
    "/robinauts/api/apps/draw/show_diagram/view?agent=planner",
  );
  expect(frame).toHaveAttribute("sandbox", "allow-scripts");
  expect(screen.getByRole("status")).toHaveTextContent("Loading Diagram 1…");
});

test("the handshake: the theme, then the agent's arguments and the result of its call", async () => {
  document.documentElement.dataset.theme = "dark";
  const fetch = answering();
  const { posted, rerender } = await started();
  const [initialized, input, result] = sent(posted);
  expect(initialized).toEqual({
    jsonrpc: "2.0",
    id: 1,
    result: {
      protocolVersion: PROTOCOL_VERSION,
      hostInfo: { name: "robinauts", version: "0" },
      hostCapabilities: { serverTools: {} },
      hostContext: {
        theme: "dark",
        displayMode: "inline",
        availableDisplayModes: ["inline"],
      },
    },
  });
  expect(input).toEqual({
    jsonrpc: "2.0",
    method: "ui/notifications/tool-input",
    params: { arguments: { diagram_id: "a" } },
  });
  expect(result).toEqual({
    jsonrpc: "2.0",
    method: "ui/notifications/tool-result",
    params: DRAWN,
  });
  // The result is the agent's own, read from the conversation: no tool is called.
  expect(fetch).toHaveBeenCalledOnce();
  const [url, init] = fetch.mock.calls[0] ?? [];
  expect(url).toBe(
    `/robinauts/api/conversations/${CONVERSATION}/messages/m2/tool-result?call_id=c1`,
  );
  expect(init?.method ?? "GET").toBe("GET");
  // Loaded: the view is shown, and nothing stands in its place.
  expect(screen.queryByRole("status")).toBeNull();

  // The theme changes on the screen, and the view is told.
  document.documentElement.dataset.theme = "light";
  await waitFor(() => {
    expect(sent(posted).at(-1)).toEqual({
      jsonrpc: "2.0",
      method: "ui/notifications/host-context-changed",
      params: { theme: "light" },
    });
  });

  // The agent shows the same diagram again: the same sub-tab, the same view,
  // told the new call's result and not its arguments again.
  rerender(tabOf([shown("c1", "a"), shown("c9", "a", { messageId: "m4" })]));
  await waitFor(() => {
    expect(fetch).toHaveBeenCalledTimes(2);
  });
  expect(fetch.mock.calls[1]?.[0]).toBe(
    `/robinauts/api/conversations/${CONVERSATION}/messages/m4/tool-result?call_id=c9`,
  );
  await waitFor(() => {
    expect(sent(posted).at(-1)).toEqual(result);
  });
  expect(screen.getAllByRole("tab")).toHaveLength(1);
  expect(
    sent(posted).filter(
      (message) => message.method === "ui/notifications/tool-input",
    ),
  ).toHaveLength(1);
});

test("a call is told once its tool has answered, and as cancelled when its turn ended first", async () => {
  const fetch = answering();
  const running = shown("c1", "a", { state: "running" });
  const { view, posted, rerender } = tab([running]);
  handshake(view);
  await waitFor(() => {
    expect(sent(posted)).toHaveLength(2);
  });
  // Still running: the arguments are told, and the view waits.
  expect(fetch).not.toHaveBeenCalled();
  expect(screen.getByRole("status")).toHaveTextContent("Loading Diagram 1…");

  rerender(tabOf([{ ...running, state: "unanswered" }]));
  await waitFor(() => {
    expect(sent(posted).at(-1)).toEqual({
      jsonrpc: "2.0",
      method: "ui/notifications/tool-cancelled",
      params: { reason: "The turn ended before the tool answered." },
    });
  });
  expect(fetch).not.toHaveBeenCalled();
  expect(screen.queryByRole("status")).toBeNull();
});

test("the view's tool calls go through the backend, and nothing else is offered", async () => {
  const fetch = answering((call) =>
    call.url.endsWith("/pin_node")
      ? refusal(502, "BadGateway", "The tool server did not answer.")
      : json(DRAWN),
  );
  const { view, posted } = await started();

  from(view, {
    jsonrpc: "2.0",
    id: 2,
    method: "tools/call",
    params: { name: "show_diagram", arguments: { diagram_id: "a" } },
  });
  await waitFor(() => {
    expect(sent(posted).at(-1)).toEqual({
      jsonrpc: "2.0",
      id: 2,
      result: DRAWN,
    });
  });
  expect(fetch.mock.calls.at(-1)?.[0]).toBe(
    "/robinauts/api/apps/draw/tools/show_diagram",
  );
  expect(JSON.parse(String(fetch.mock.calls.at(-1)?.[1]?.body))).toEqual({
    agent: "planner",
    arguments: { diagram_id: "a" },
  });

  // A call the backend refuses: an error, in fixed words.
  from(view, {
    jsonrpc: "2.0",
    id: 3,
    method: "tools/call",
    params: { name: "pin_node", arguments: { id: "a" } },
  });
  await waitFor(() => {
    expect(sent(posted).at(-1)).toEqual({
      jsonrpc: "2.0",
      id: 3,
      error: { code: -32603, message: "The tool call failed." },
    });
  });

  from(view, { jsonrpc: "2.0", id: 4, method: "ui/open-link", params: {} });
  expect(sent(posted).at(-1)).toEqual({
    jsonrpc: "2.0",
    id: 4,
    error: { code: -32601, message: "Method not found" },
  });
  from(view, { jsonrpc: "2.0", id: 5, method: "ping" });
  expect(sent(posted).at(-1)).toEqual({ jsonrpc: "2.0", id: 5, result: {} });
});

test("a message from anywhere but the frame, or one that is no message, is not read", () => {
  const fetch = answering();
  const { view, posted } = tab();
  from(window, { jsonrpc: "2.0", id: 1, method: "ui/initialize" });
  // From the frame's window, and from an origin with a name: not the view.
  from(
    view,
    { jsonrpc: "2.0", id: 1, method: "ui/initialize" },
    "https://elsewhere.example",
  );
  from(view, "ui/initialize");
  from(view, { id: 1, method: "ui/initialize" });
  from(view, { jsonrpc: "2.0", method: "ui/notifications/initialized" });
  expect(posted).not.toHaveBeenCalled();
  expect(fetch).not.toHaveBeenCalled();
});

test("a result that cannot be read says so in the view's place, and can be tried again", async () => {
  answering(() => refusal(404, "MessageNotFoundError", "No such result."));
  const { frame, view } = tab();
  handshake(view);
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("Diagram 1 could not be opened.");
  expect(alert).toHaveTextContent("No such result.");

  answering();
  fireEvent.click(screen.getByRole("button", { name: "Try again" }));
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.getByRole("status")).toHaveTextContent("Loading Diagram 1…");
  // A new frame, and a new handshake with it.
  expect(frameOf("Diagram 1")).not.toBe(frame);
});

test("a later result that cannot be read leaves the view that is showing", async () => {
  const fetch = answering();
  const { frame, posted, rerender } = await started();
  expect(screen.queryByRole("status")).toBeNull();

  // The agent shows the same diagram again, and this call's result is not read.
  answering(() => refusal(404, "MessageNotFoundError", "No such result."));
  rerender(tabOf([shown("c1", "a"), shown("c9", "a", { messageId: "m4" })]));
  await waitFor(() => {
    expect(vi.mocked(globalThis.fetch)).toHaveBeenCalledOnce();
  });
  await act(async () => {
    await Promise.resolve();
  });
  // The same frame, still shown, with what it was told before and no more.
  expect(screen.queryByRole("alert")).toBeNull();
  expect(frameOf("Diagram 1")).toBe(frame);
  expect(frame.closest(".side-app")).toHaveAttribute("data-state", "ready");
  expect(sent(posted)).toHaveLength(3);
  expect(fetch).toHaveBeenCalledOnce();
});

test("a view that loads and never asks to start is said not to have", () => {
  vi.useFakeTimers();
  try {
    answering();
    const { frame } = tab();
    fireEvent.load(frame);
    act(() => {
      vi.advanceTimersByTime(START_TIMEOUT_MS);
    });
    expect(screen.getByRole("alert")).toHaveTextContent(
      "The app's view did not start.",
    );
  } finally {
    vi.useRealTimers();
  }
});

test("a tab is empty until the agent calls, then holds a sub-tab per call, the asked one on top", async () => {
  answering();
  const { rerender } = render(tabOf([]));
  expect(
    screen.getByText(/Diagrams shows here when the agent uses it/),
  ).toBeInTheDocument();
  expect(screen.queryByRole("tab")).toBeNull();

  // Two diagrams: the newest call is on top, and only its view is loaded.
  const first = shown("c1", "a", { live: false });
  const second = shown("c2", "b", { live: false });
  rerender(tabOf([first, second]));
  const names = () => screen.getAllByRole("tab").map((tab) => tab.textContent);
  const onTop = () =>
    screen
      .getAllByRole("tab")
      .find((tab) => tab.getAttribute("aria-selected") === "true")?.textContent;
  expect(names()).toEqual(["Diagram 1", "Diagram 2"]);
  expect(onTop()).toBe("Diagram 2");
  expect(document.querySelectorAll("iframe")).toHaveLength(1);
  const secondView = frameOf("Diagram 2");

  // The person picks the first: its view is made, and the second's is kept.
  fireEvent.click(screen.getByRole("tab", { name: "Diagram 1" }));
  expect(onTop()).toBe("Diagram 1");
  const firstView = frameOf("Diagram 1");
  expect(frameOf("Diagram 2")).toBe(secondView);
  expect(secondView.closest("[hidden]")).not.toBeNull();

  // The agent asks for the second again: it takes the top back.
  const asked = { sub: subTabKey("show_diagram", { diagram_id: "b" }), at: 1 };
  rerender(tabOf([first, second, shown("c3", "b")], asked));
  expect(onTop()).toBe("Diagram 2");
  expect(names()).toEqual(["Diagram 1", "Diagram 2"]);
  // The asking is taken once: what the person picks after it stays.
  fireEvent.keyDown(screen.getByRole("tab", { name: "Diagram 2" }), {
    key: "ArrowLeft",
  });
  expect(onTop()).toBe("Diagram 1");
  rerender(tabOf([first, second, shown("c3", "b")], asked));
  expect(onTop()).toBe("Diagram 1");

  // A view that has started is told nothing as its frame goes: a message
  // posted in the step that removes a frame never reaches it.
  const view = firstView.contentWindow;
  if (view === null) throw new Error("the frame has no window");
  const posted = vi.spyOn(view, "postMessage");
  handshake(view);
  await waitFor(() => {
    expect(posted).toHaveBeenCalledTimes(3);
  });
  rerender(<p>closed</p>);
  expect(posted).toHaveBeenCalledTimes(3);
});

test("a sub-tab closes on its own, stays closed, and a later call of the agent's opens it again", () => {
  localStorage.clear();
  answering();
  const first = shown("c1", "a", { live: false });
  const second = shown("c2", "b", { live: false });
  const names = () =>
    screen.queryAllByRole("tab").map((tab) => tab.textContent);
  const { unmount } = render(tabOf([first, second]));

  // The one on top is closed: its frame goes, and its neighbour takes the top.
  fireEvent.click(screen.getByRole("button", { name: "Close Diagram 2" }));
  expect(names()).toEqual(["Diagram 1"]);
  expect(document.querySelector('iframe[title="Diagram 2"]')).toBeNull();
  expect(screen.getByRole("tab", { name: "Diagram 1" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  expect(frameOf("Diagram 1")).toBeInTheDocument();

  // Delete closes the one on top too. With none left, the tab says so.
  fireEvent.keyDown(screen.getByRole("tab", { name: "Diagram 1" }), {
    key: "Delete",
  });
  expect(names()).toEqual([]);
  expect(
    screen.getByText(/Diagrams shows here when the agent uses it/),
  ).toBeInTheDocument();

  // The conversation opened again: this browser remembers both as closed.
  unmount();
  const again = render(tabOf([first, second]));
  expect(names()).toEqual([]);

  // The agent shows the second diagram again: its sub-tab is back, under
  // the name it had.
  again.rerender(tabOf([first, second, shown("c3", "b")]));
  expect(names()).toEqual(["Diagram 2"]);
  localStorage.clear();
});

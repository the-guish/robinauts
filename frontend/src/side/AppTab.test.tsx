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

import { json, refusal, stubFetch, type Call } from "../test/api";
import { AppTab, START_TIMEOUT_MS } from "./AppTab";
import { type App, type ToolResults, toolResults } from "./apps";
import { PROTOCOL_VERSION } from "./bridge";

const APP: App = {
  id: "todo/show_todos",
  server: "todo",
  tool: "show_todos",
  title: "To-do list",
  description: "",
};

const LIST = {
  content: [{ type: "text", text: "1 to-do(s), 0 done" }],
  structuredContent: { items: [{ id: "1", text: "milk", done: false }] },
  isError: false,
};

function answering(answer: (call: Call) => Response = () => json(LIST)) {
  return stubFetch(answer);
}

/** The tab, its frame, and what the frame's window is posted. */
function tab(results?: ToolResults) {
  render(<AppTab agent="planner" app={APP} turnsEnded={0} results={results} />);
  const frame = screen.getByTitle<HTMLIFrameElement>("To-do list");
  const view = frame.contentWindow;
  if (view === null) throw new Error("the frame has no window");
  const posted = vi.spyOn(view, "postMessage");
  return { frame, view, posted };
}

/** A message from `source`, as the browser would hand it to the page. */
function from(source: Window, data: unknown) {
  act(() => {
    window.dispatchEvent(new MessageEvent("message", { data, source }));
  });
}

const sent = (posted: ReturnType<typeof tab>["posted"]) =>
  posted.mock.calls.map(([message, target]) => {
    // Always "*": an opaque origin has no other name.
    expect(target).toBe("*");
    return message as Record<string, unknown>;
  });

async function started(results?: ToolResults) {
  const drawn = tab(results);
  from(drawn.view, {
    jsonrpc: "2.0",
    id: 1,
    method: "ui/initialize",
    params: { protocolVersion: PROTOCOL_VERSION },
  });
  from(drawn.view, {
    jsonrpc: "2.0",
    method: "ui/notifications/initialized",
    params: {},
  });
  await waitFor(() => {
    expect(sent(drawn.posted)).toHaveLength(3);
  });
  return drawn;
}

test("the view is framed sandboxed, with scripts alone, and hidden until it answers", () => {
  answering();
  const { frame } = tab();
  expect(frame).toHaveAttribute(
    "src",
    "/api/apps/todo/show_todos/view?agent=planner",
  );
  expect(frame).toHaveAttribute("sandbox", "allow-scripts");
  expect(screen.getByRole("status")).toHaveTextContent("Loading To-do list…");
});

test("the handshake: the theme, then the app's own tool, its input and its result", async () => {
  document.documentElement.dataset.theme = "dark";
  const fetch = answering();
  const { posted } = await started();
  const [initialized, input, result] = sent(posted);
  expect(initialized).toEqual({
    jsonrpc: "2.0",
    id: 1,
    result: {
      protocolVersion: PROTOCOL_VERSION,
      hostInfo: { name: "robinauts" },
      hostCapabilities: { serverTools: {} },
      hostContext: { theme: "dark", displayMode: "inline" },
    },
  });
  expect(input).toEqual({
    jsonrpc: "2.0",
    method: "ui/notifications/tool-input",
    params: { arguments: {} },
  });
  expect(result).toEqual({
    jsonrpc: "2.0",
    method: "ui/notifications/tool-result",
    params: LIST,
  });
  expect(fetch).toHaveBeenCalledOnce();
  const [url, init] = fetch.mock.calls[0] ?? [];
  expect(url).toBe("/api/apps/todo/tools/show_todos");
  expect(init?.method).toBe("POST");
  expect(JSON.parse(String(init?.body))).toEqual({
    agent: "planner",
    arguments: {},
  });
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
});

test("the view's tool calls go through the backend, and nothing else is offered", async () => {
  const fetch = answering((call) =>
    call.url.endsWith("/remove_todo")
      ? refusal(502, "BadGateway", "The tool server did not answer.")
      : json(LIST),
  );
  const { view, posted } = await started();

  from(view, {
    jsonrpc: "2.0",
    id: 2,
    method: "tools/call",
    params: { name: "add_todo", arguments: { text: "milk" } },
  });
  await waitFor(() => {
    expect(sent(posted).at(-1)).toEqual({
      jsonrpc: "2.0",
      id: 2,
      result: LIST,
    });
  });
  expect(fetch.mock.calls.at(-1)?.[0]).toBe("/api/apps/todo/tools/add_todo");
  expect(JSON.parse(String(fetch.mock.calls.at(-1)?.[1]?.body))).toEqual({
    agent: "planner",
    arguments: { text: "milk" },
  });

  // A call the backend refuses: an error, in fixed words.
  from(view, {
    jsonrpc: "2.0",
    id: 3,
    method: "tools/call",
    params: { name: "remove_todo", arguments: { id: "1" } },
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
  from(view, "ui/initialize");
  from(view, { id: 1, method: "ui/initialize" });
  from(view, { jsonrpc: "2.0", method: "ui/notifications/initialized" });
  expect(posted).not.toHaveBeenCalled();
  expect(fetch).not.toHaveBeenCalled();
});

test("a first call that fails says so in the view's place, and can be tried again", async () => {
  answering(() =>
    refusal(502, "BadGateway", "The tool server did not answer."),
  );
  const { frame, view } = tab();
  from(view, { jsonrpc: "2.0", id: 1, method: "ui/initialize" });
  from(view, { jsonrpc: "2.0", method: "ui/notifications/initialized" });
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("To-do list could not be opened.");
  expect(alert).toHaveTextContent("The tool server did not answer.");

  answering();
  fireEvent.click(screen.getByRole("button", { name: "Try again" }));
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.getByRole("status")).toHaveTextContent("Loading To-do list…");
  // A new frame, and a new handshake with it.
  expect(screen.getByTitle("To-do list")).not.toBe(frame);
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

test("a tool of the app's server answering in the chat hands the view the app's answer again", async () => {
  const fetch = answering();
  const results = toolResults();
  const { posted } = await started(results);
  act(() => {
    results.publish("other__show_todos");
    results.publish("todo__add_todo");
  });
  await waitFor(() => {
    expect(sent(posted)).toHaveLength(4);
  });
  expect(sent(posted)[3]).toEqual({
    jsonrpc: "2.0",
    method: "ui/notifications/tool-result",
    params: LIST,
  });
  expect(fetch).toHaveBeenCalledTimes(2);
});

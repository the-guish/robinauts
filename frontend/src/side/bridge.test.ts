// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import { describe, expect, test } from "vitest";

import {
  callToolResult,
  dispatch,
  hostContextChanged,
  hostTheme,
  type Inbound,
  initializeResult,
  INTERNAL_ERROR,
  INVALID_PARAMS,
  METHOD_NOT_FOUND,
  parseInbound,
  type Phase,
  PROTOCOL_VERSION,
  toolCallFailed,
  toolInput,
  toolResult,
} from "./bridge";

describe("parseInbound", () => {
  test.each<[string, unknown, Inbound | null]>([
    [
      "a request with a number id",
      { jsonrpc: "2.0", id: 1, method: "ping" },
      { kind: "request", id: 1, method: "ping", params: undefined },
    ],
    [
      "a request with a string id and params",
      { jsonrpc: "2.0", id: "a", method: "tools/call", params: { name: "x" } },
      { kind: "request", id: "a", method: "tools/call", params: { name: "x" } },
    ],
    [
      "a notification",
      { jsonrpc: "2.0", method: "ui/notifications/initialized", params: {} },
      {
        kind: "notification",
        method: "ui/notifications/initialized",
        params: {},
      },
    ],
    ["not an object", "ping", null],
    ["null", null, null],
    ["an array", [{ jsonrpc: "2.0", id: 1, method: "ping" }], null],
    ["another version", { jsonrpc: "1.0", id: 1, method: "ping" }, null],
    ["no version", { id: 1, method: "ping" }, null],
    ["no method: a response", { jsonrpc: "2.0", id: 1, result: {} }, null],
    ["an empty method", { jsonrpc: "2.0", id: 1, method: "" }, null],
    ["a method that is not text", { jsonrpc: "2.0", id: 1, method: 7 }, null],
    ["a null id", { jsonrpc: "2.0", id: null, method: "ping" }, null],
    ["a fractional id", { jsonrpc: "2.0", id: 1.5, method: "ping" }, null],
    ["an object id", { jsonrpc: "2.0", id: {}, method: "ping" }, null],
    [
      "params that are not structured",
      { jsonrpc: "2.0", id: 1, method: "ping", params: "x" },
      null,
    ],
  ])("%s", (_, data, expected) => {
    expect(parseInbound(data)).toEqual(expected);
  });
});

const request = (method: string, params?: unknown): Inbound => ({
  kind: "request",
  id: 7,
  method,
  params,
});
const note = (method: string): Inbound => ({
  kind: "notification",
  method,
  params: {},
});

describe("dispatch", () => {
  test.each<[string, Inbound, Phase, ReturnType<typeof dispatch>]>([
    [
      "ui/initialize is answered with the theme, and starts the handshake",
      request("ui/initialize", { protocolVersion: PROTOCOL_VERSION }),
      "waiting",
      {
        phase: "initializing",
        step: {
          kind: "reply",
          message: {
            jsonrpc: "2.0",
            id: 7,
            result: {
              protocolVersion: PROTOCOL_VERSION,
              hostInfo: { name: "robinauts" },
              hostCapabilities: { serverTools: {} },
              hostContext: { theme: "dark", displayMode: "inline" },
            },
          },
        },
      },
    ],
    [
      "a view that starts again is answered again",
      request("ui/initialize"),
      "running",
      {
        phase: "initializing",
        step: {
          kind: "reply",
          message: { jsonrpc: "2.0", id: 7, result: initializeResult("dark") },
        },
      },
    ],
    [
      "initialized after initialize starts the app",
      note("ui/notifications/initialized"),
      "initializing",
      { phase: "running", step: { kind: "start" } },
    ],
    [
      "initialized before initialize is ignored",
      note("ui/notifications/initialized"),
      "waiting",
      { phase: "waiting", step: { kind: "ignore" } },
    ],
    [
      "initialized twice starts it once",
      note("ui/notifications/initialized"),
      "running",
      { phase: "running", step: { kind: "ignore" } },
    ],
    [
      "another notification is ignored",
      note("ui/notifications/size-changed"),
      "running",
      { phase: "running", step: { kind: "ignore" } },
    ],
    [
      "ping is answered with nothing",
      request("ping"),
      "running",
      {
        phase: "running",
        step: { kind: "reply", message: { jsonrpc: "2.0", id: 7, result: {} } },
      },
    ],
    [
      "tools/call is relayed",
      request("tools/call", { name: "add_todo", arguments: { text: "milk" } }),
      "running",
      {
        phase: "running",
        step: {
          kind: "call",
          id: 7,
          name: "add_todo",
          arguments: { text: "milk" },
        },
      },
    ],
    [
      "tools/call without arguments has none",
      request("tools/call", { name: "show_todos" }),
      "running",
      {
        phase: "running",
        step: { kind: "call", id: 7, name: "show_todos", arguments: {} },
      },
    ],
    ...(
      [
        ["no params", undefined],
        ["params that are a list", ["add_todo"]],
        ["no name", { arguments: {} }],
        ["an empty name", { name: "" }],
        ["a name that is not text", { name: 3 }],
        ["arguments that are a list", { name: "add_todo", arguments: [1] }],
        ["null arguments", { name: "add_todo", arguments: null }],
      ] as const
    ).map(
      ([what, params]): [
        string,
        Inbound,
        Phase,
        ReturnType<typeof dispatch>,
      ] => [
        `tools/call with ${what} is refused as invalid`,
        request("tools/call", params),
        "running",
        {
          phase: "running",
          step: {
            kind: "reply",
            message: {
              jsonrpc: "2.0",
              id: 7,
              error: { code: INVALID_PARAMS, message: "Invalid params" },
            },
          },
        },
      ],
    ),
    ...["ui/open-link", "ui/message", "resources/read", "tools/list"].map(
      (method): [string, Inbound, Phase, ReturnType<typeof dispatch>] => [
        `${method} is not offered`,
        request(method, {}),
        "running",
        {
          phase: "running",
          step: {
            kind: "reply",
            message: {
              jsonrpc: "2.0",
              id: 7,
              error: { code: METHOD_NOT_FOUND, message: "Method not found" },
            },
          },
        },
      ],
    ),
  ])("%s", (_, message, phase, expected) => {
    expect(dispatch(message, phase, "dark")).toEqual(expected);
  });
});

test("what the host tells the view", () => {
  const answer = {
    content: [{ type: "text", text: "1 to-do" }],
    structuredContent: { items: [] },
    isError: false,
  };
  expect(toolInput()).toEqual({
    jsonrpc: "2.0",
    method: "ui/notifications/tool-input",
    params: { arguments: {} },
  });
  expect(toolResult(answer)).toEqual({
    jsonrpc: "2.0",
    method: "ui/notifications/tool-result",
    params: answer,
  });
  // No structured content is none, not null.
  expect(
    callToolResult({ ...answer, structuredContent: null, isError: true }),
  ).toEqual({ content: answer.content, isError: true });
  expect(hostContextChanged("light")).toEqual({
    jsonrpc: "2.0",
    method: "ui/notifications/host-context-changed",
    params: { theme: "light" },
  });
  // Fixed words, whatever the backend said.
  expect(toolCallFailed("x")).toEqual({
    jsonrpc: "2.0",
    id: "x",
    error: { code: INTERNAL_ERROR, message: "The tool call failed." },
  });
});

test.each<[string | undefined, boolean, "light" | "dark"]>([
  ["light", true, "light"],
  ["dark", false, "dark"],
  [undefined, true, "dark"],
  [undefined, false, "light"],
  ["system", true, "dark"],
])("the theme chosen %s, the system dark %s, is %s", (chosen, dark, theme) => {
  expect(hostTheme(chosen, dark)).toBe(theme);
});

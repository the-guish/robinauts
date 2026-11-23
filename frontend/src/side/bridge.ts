// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The host's side of the MCP Apps protocol (`io.modelcontextprotocol/ui`):
 * what an app's view may say to the panel, and what the panel says back
 * (`docs/architecture/mcp-apps.md`).
 *
 * JSON-RPC 2.0 over `postMessage`. The view asks `ui/initialize` and is told
 * the theme and the size of its box; it says `ui/notifications/initialized`,
 * and the host then tells it the arguments of the agent's call and, once the
 * tool has answered, its result. The view may ask `tools/call`, which the
 * backend relays to its server, and `ping`. Anything else it asks is
 * answered "method not found": a view cannot open a page, write in the chat
 * or reach the model.
 *
 * **Everything here is pure**: it reads a message and says what to do, and
 * builds what is sent. The frame, the listener and the network are
 * `./AppFrame.tsx`'s. What is sent goes to an opaque origin, so it is posted
 * to `"*"`: nothing built here carries more than the view's own server
 * already has -- the theme, the size, the arguments of the agent's call and
 * what the server answered.
 */

/** The version of MCP Apps the host speaks. */
export const PROTOCOL_VERSION = "2026-01-26";

/**
 * Who the host says it is. The standard asks for a name and a version, and
 * its own library refuses an answer to `ui/initialize` without both. The
 * interface has no version of its own to tell, so it tells the one the
 * backend's MCP client tells a server (`web/mcp_client.py`).
 */
export const HOST_INFO = { name: "robinauts", version: "0" } as const;

/** The theme a view is told: the one on the screen, never "system". */
export type HostTheme = "light" | "dark";

export type RequestId = string | number;

/** A message from the view the host reads: a request or a notification. */
export type Inbound =
  | {
      readonly kind: "request";
      readonly id: RequestId;
      readonly method: string;
      readonly params: unknown;
    }
  | {
      readonly kind: "notification";
      readonly method: string;
      readonly params: unknown;
    };

/** A JSON-RPC message the host posts to the view. */
export type Outbound =
  | { jsonrpc: "2.0"; id: RequestId; result: unknown }
  | {
      jsonrpc: "2.0";
      id: RequestId;
      error: { code: number; message: string };
    }
  | { jsonrpc: "2.0"; method: string; params: unknown };

/** JSON-RPC's own error codes, the three the host answers with. */
export const METHOD_NOT_FOUND = -32601;
export const INVALID_PARAMS = -32602;
export const INTERNAL_ERROR = -32603;

/**
 * How far the handshake has got: nothing yet, `ui/initialize` answered, or
 * `ui/notifications/initialized` heard and the view told its call.
 */
export type Phase = "waiting" | "initializing" | "running";

/** What to do about one message from the view. */
export type Step =
  /** Post this answer. */
  | { readonly kind: "reply"; readonly message: Outbound }
  /** Call the tool through the backend, and answer request `id` with it. */
  | {
      readonly kind: "call";
      readonly id: RequestId;
      readonly name: string;
      readonly arguments: Readonly<Record<string, unknown>>;
    }
  /** The handshake is done: tell the view the call it is of. */
  | { readonly kind: "start" }
  | { readonly kind: "ignore" };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isId(value: unknown): value is RequestId {
  return (
    typeof value === "string" ||
    (typeof value === "number" && Number.isInteger(value))
  );
}

/**
 * A message from the view, or `null` for anything that is not a request or
 * a notification of JSON-RPC 2.0.
 *
 * Responses are `null` too: the host asks the view nothing, so there is
 * nothing for one to answer. A view's message is a third party's data, read
 * as such: no field is trusted to be what it claims until it is looked at.
 */
export function parseInbound(data: unknown): Inbound | null {
  if (!isRecord(data) || data.jsonrpc !== "2.0") return null;
  const { method, params } = data;
  if (typeof method !== "string" || method === "") return null;
  if (params !== undefined && typeof params !== "object") return null;
  if (!("id" in data)) return { kind: "notification", method, params };
  return isId(data.id)
    ? { kind: "request", id: data.id, method, params }
    : null;
}

export function response(id: RequestId, result: unknown): Outbound {
  return { jsonrpc: "2.0", id, result };
}

export function errorResponse(
  id: RequestId,
  code: number,
  message: string,
): Outbound {
  return { jsonrpc: "2.0", id, error: { code, message } };
}

export function notification(method: string, params: unknown): Outbound {
  return { jsonrpc: "2.0", method, params };
}

/** The size of the box a view is drawn in, in CSS pixels. */
export interface BoxSize {
  readonly width: number;
  readonly height: number;
}

/**
 * The size as the view is told it: both dimensions fixed, so the view fills
 * its box. A box not on the screen has no size to tell.
 */
function containerDimensions(size: BoxSize | null) {
  return size === null || size.width <= 0 || size.height <= 0
    ? {}
    : { containerDimensions: { width: size.width, height: size.height } };
}

/**
 * What `ui/initialize` is answered with. The side panel is told as `inline`,
 * the one mode offered, with the size of the view's box.
 */
export function initializeResult(theme: HostTheme, size: BoxSize | null) {
  return {
    protocolVersion: PROTOCOL_VERSION,
    hostInfo: HOST_INFO,
    hostCapabilities: { serverTools: {} },
    hostContext: {
      theme,
      displayMode: "inline",
      availableDisplayModes: ["inline"],
      ...containerDimensions(size),
    },
  };
}

/** The arguments of the agent's call, which the view is of. */
export function toolInput(args: Readonly<Record<string, unknown>>): Outbound {
  return notification("ui/notifications/tool-input", { arguments: args });
}

/** The agent's call ended with no result: its turn was stopped or failed. */
export function toolCancelled(): Outbound {
  return notification("ui/notifications/tool-cancelled", {
    reason: "The turn ended before the tool answered.",
  });
}

/** What the backend answered a tool call with, in MCP's shape. */
export interface ToolAnswer {
  readonly content: readonly unknown[];
  readonly structuredContent: Readonly<Record<string, unknown>> | null;
  readonly isError: boolean;
}

/**
 * A `CallToolResult` as MCP writes it: `structuredContent` left out rather
 * than `null` when the server sent none.
 */
export function callToolResult(answer: ToolAnswer): Record<string, unknown> {
  return {
    content: answer.content,
    isError: answer.isError,
    ...(answer.structuredContent === null
      ? {}
      : { structuredContent: answer.structuredContent }),
  };
}

/** The result of the agent's call, handed to the view. */
export function toolResult(answer: ToolAnswer): Outbound {
  return notification("ui/notifications/tool-result", callToolResult(answer));
}

/** What changed of the host's context: the theme, or the size of the box. */
export function hostContextChanged(
  changed: { readonly theme: HostTheme } | { readonly size: BoxSize },
): Outbound {
  return notification(
    "ui/notifications/host-context-changed",
    "theme" in changed
      ? { theme: changed.theme }
      : containerDimensions(changed.size),
  );
}

/**
 * The answer to a tool call the backend did not answer with a result.
 *
 * Fixed words: the backend's refusal is for the person, not for the view.
 */
export function toolCallFailed(id: RequestId): Outbound {
  return errorResponse(id, INTERNAL_ERROR, "The tool call failed.");
}

/** What to do about one message, and how far the handshake has got after. */
export function dispatch(
  message: Inbound,
  phase: Phase,
  theme: HostTheme,
  size: BoxSize | null = null,
): { readonly phase: Phase; readonly step: Step } {
  if (message.kind === "notification") {
    if (
      message.method === "ui/notifications/initialized" &&
      phase === "initializing"
    ) {
      return { phase: "running", step: { kind: "start" } };
    }
    return { phase, step: { kind: "ignore" } };
  }
  const { id, method, params } = message;
  const reply = (outbound: Outbound) => ({
    phase,
    step: { kind: "reply", message: outbound } as const,
  });
  switch (method) {
    case "ui/initialize":
      return {
        // A view that starts again is told again, its call included.
        phase: "initializing",
        step: {
          kind: "reply",
          message: response(id, initializeResult(theme, size)),
        },
      };
    case "ping":
      return reply(response(id, {}));
    case "tools/call": {
      const call = toolCall(params);
      return call === null
        ? reply(errorResponse(id, INVALID_PARAMS, "Invalid params"))
        : { phase, step: { kind: "call", id, ...call } };
    }
    default:
      return reply(errorResponse(id, METHOD_NOT_FOUND, "Method not found"));
  }
}

function toolCall(
  params: unknown,
): { name: string; arguments: Record<string, unknown> } | null {
  if (!isRecord(params)) return null;
  const { name, arguments: given } = params;
  if (typeof name !== "string" || name === "") return null;
  if (given === undefined) return { name, arguments: {} };
  return isRecord(given) ? { name, arguments: given } : null;
}

/**
 * The theme on the screen: the one chosen on `<html>` (`data-theme`), or the
 * operating system's when the choice is "system" (no attribute).
 */
export function hostTheme(
  chosen: string | undefined,
  systemDark: boolean,
): HostTheme {
  if (chosen === "light" || chosen === "dark") return chosen;
  return systemDark ? "dark" : "light";
}

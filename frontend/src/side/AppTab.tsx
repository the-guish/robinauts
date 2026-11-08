// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * An MCP app's tab: its view in a sandboxed frame, and the host's side of
 * the conversation with it (`./bridge.ts`,
 * `docs/adr/0007-mcp-apps-in-the-side-panel.md`).
 *
 * **The frame.** The view is served by the backend under a policy of its own
 * (`viewUrl`) and framed with `sandbox="allow-scripts"` and nothing else:
 * never `allow-same-origin`, so it runs in an opaque origin and can neither
 * read the session, call the API, nor script this page.
 *
 * **The bridge.** Only a message whose `source` is this frame's window is
 * read, and only one `./bridge.ts` makes sense of; anything else is
 * dropped. An opaque origin has no name to post to, so answers go to `"*"`,
 * and they carry only what the view may see: the theme and what its own
 * server answered. Tool calls go to the backend, which relays them to the
 * server with its credential (`POST /api/apps/<server>/tools/<tool>`).
 *
 * **Loading.** The view is hidden until the app's own tool has answered,
 * and a view that never starts, or a first call that fails, is said so in
 * its place, with a way to try again.
 *
 * **After a turn.** A turn may change what the app shows, so each one that
 * ends has the app's own tool called again and its result handed to the view.
 * A call that fails leaves the view as it is. A tool of the app's server
 * that answers in the chat does the same, so what the agent changes shows
 * while the turn goes on; only the newest of these reads reaches the view.
 */
import { useEffect, useRef, useState, useSyncExternalStore } from "react";

import { detailOf, request } from "../api/client";
import { type App, type ToolResults, viewUrl } from "./apps";
import {
  callToolResult,
  dispatch,
  type HostTheme,
  hostContextChanged,
  hostTheme,
  type Outbound,
  parseInbound,
  type Phase,
  response,
  toolCallFailed,
  toolInput,
  toolResult,
} from "./bridge";

/** How long a view that has loaded has to ask `ui/initialize`. */
export const START_TIMEOUT_MS = 10_000;

const DARK = "(prefers-color-scheme: dark)";

function systemDark(): boolean {
  try {
    return window.matchMedia(DARK).matches;
  } catch {
    return false;
  }
}

function subscribeToTheme(changed: () => void): () => void {
  // The choice is an attribute on <html> (`../shell/ThemeToggle.tsx`); the
  // system's is a media query. Either may change the theme on the screen.
  const observer = new MutationObserver(changed);
  observer.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme"],
  });
  let query: MediaQueryList | null = null;
  try {
    query = window.matchMedia(DARK);
    query.addEventListener("change", changed);
  } catch {
    query = null;
  }
  return () => {
    observer.disconnect();
    query?.removeEventListener("change", changed);
  };
}

/** The theme on the screen, light or dark, whatever chose it. */
export function useHostTheme(): HostTheme {
  return useSyncExternalStore(
    subscribeToTheme,
    () => hostTheme(document.documentElement.dataset.theme, systemDark()),
    () => "light",
  );
}

function callTool(
  agent: string,
  server: string,
  tool: string,
  args: Readonly<Record<string, unknown>>,
  signal: AbortSignal,
) {
  return request("post", "/api/apps/{server}/tools/{tool}", {
    path: { server, tool },
    body: { agent, arguments: { ...args } },
    signal,
  });
}

export function AppTab({
  agent,
  app,
  turnsEnded,
  results,
}: Readonly<{
  agent: string;
  app: App;
  turnsEnded: number;
  results?: ToolResults | undefined;
}>) {
  // "Try again" is a new frame, and a new handshake with it.
  const [attempt, setAttempt] = useState(0);
  return (
    <AppFrame
      key={attempt}
      agent={agent}
      app={app}
      turnsEnded={turnsEnded}
      results={results}
      onRetry={() => {
        setAttempt((count) => count + 1);
      }}
    />
  );
}

type FrameState =
  | { status: "loading" }
  | { status: "ready" }
  | { status: "failed"; detail: string };

function AppFrame({
  agent,
  app,
  turnsEnded,
  results,
  onRetry,
}: Readonly<{
  agent: string;
  app: App;
  turnsEnded: number;
  results?: ToolResults | undefined;
  onRetry: () => void;
}>) {
  const theme = useHostTheme();
  const frame = useRef<HTMLIFrameElement>(null);
  const [state, setState] = useState<FrameState>({ status: "loading" });
  const [loaded, setLoaded] = useState(false);
  // Read by the listener, which outlives the render that made it.
  const shownTheme = useRef(theme);
  const phase = useRef<Phase>("waiting");
  // The theme the view was last told, once it has been told one.
  const told = useRef<HostTheme | null>(null);
  // Reads of the app's tool after a change, numbered: a tool's answer and a
  // turn's end may both ask, and only the newest answer is handed over.
  const asked = useRef(0);

  useEffect(() => {
    shownTheme.current = theme;
    if (told.current === null || told.current === theme) return;
    told.current = theme;
    post(frame.current, hostContextChanged(theme));
  }, [theme]);

  useEffect(() => {
    const target = frame.current;
    const dropped = new AbortController();
    const call = (tool: string, args: Readonly<Record<string, unknown>>) =>
      callTool(agent, app.server, tool, args, dropped.signal);

    const onMessage = (event: MessageEvent) => {
      if (
        target === null ||
        event.source === null ||
        event.source !== target.contentWindow
      ) {
        return;
      }
      const message = parseInbound(event.data);
      if (message === null) return;
      const next = dispatch(message, phase.current, shownTheme.current);
      phase.current = next.phase;
      const { step } = next;
      switch (step.kind) {
        case "reply":
          if (message.kind === "request" && message.method === "ui/initialize")
            told.current = shownTheme.current;
          post(target, step.message);
          return;
        case "start":
          post(target, toolInput());
          call(app.tool, {}).then(
            (answer) => {
              post(target, toolResult(answer));
              setState({ status: "ready" });
            },
            (failure: unknown) => {
              if (dropped.signal.aborted) return;
              setState({ status: "failed", detail: detailOf(failure) });
            },
          );
          return;
        case "call":
          call(step.name, step.arguments).then(
            (answer) => {
              post(target, response(step.id, callToolResult(answer)));
            },
            () => {
              if (!dropped.signal.aborted)
                post(target, toolCallFailed(step.id));
            },
          );
          return;
        case "ignore":
          return;
      }
    };
    window.addEventListener("message", onMessage);
    const deaf = results?.subscribe((name) => {
      if (phase.current !== "running" || !name.startsWith(`${app.server}__`))
        return;
      const mine = ++asked.current;
      call(app.tool, {}).then(
        (answer) => {
          if (mine === asked.current) post(target, toolResult(answer));
        },
        () => {
          // The view keeps what it shows; the next change asks again.
        },
      );
    });
    return () => {
      window.removeEventListener("message", onMessage);
      deaf?.();
      dropped.abort();
    };
  }, [agent, app.server, app.tool, results]);

  // The next turn's read cancels this one, so the newest result is the last
  // one the view is handed. A view still starting reads the list then.
  const seenTurns = useRef(turnsEnded);
  useEffect(() => {
    if (seenTurns.current === turnsEnded) return undefined;
    seenTurns.current = turnsEnded;
    if (phase.current !== "running") return undefined;
    const dropped = new AbortController();
    const mine = ++asked.current;
    callTool(agent, app.server, app.tool, {}, dropped.signal).then(
      (answer) => {
        if (!dropped.signal.aborted && mine === asked.current)
          post(frame.current, toolResult(answer));
      },
      () => undefined,
    );
    return () => {
      dropped.abort();
    };
  }, [turnsEnded, agent, app.server, app.tool]);

  // A view that loaded and never asked to start: an error page, say, which
  // the frame holds and nobody should see.
  useEffect(() => {
    if (!loaded) return;
    const timer = setTimeout(() => {
      if (phase.current === "waiting") {
        setState({
          status: "failed",
          detail: "The app's view did not start.",
        });
      }
    }, START_TIMEOUT_MS);
    return () => {
      clearTimeout(timer);
    };
  }, [loaded]);

  return (
    <div className="side-app" data-state={state.status}>
      {state.status === "loading" && (
        <p role="status" className="side-app-status">
          Loading {app.title}…
        </p>
      )}
      {state.status === "failed" && (
        <div role="alert" className="side-app-status">
          <p>{app.title} could not be opened.</p>
          <p className="side-app-detail">{state.detail}</p>
          <button type="button" onClick={onRetry} className="side-app-retry">
            Try again
          </button>
        </div>
      )}
      <iframe
        ref={frame}
        src={viewUrl(agent, app)}
        title={app.title}
        // Scripts, and nothing else: no allow-same-origin, ever.
        sandbox="allow-scripts"
        onLoad={() => {
          setLoaded(true);
        }}
        className="side-app-frame"
      />
    </div>
  );
}

function post(target: HTMLIFrameElement | null, message: Outbound): void {
  // An opaque origin has no name, so "*": see the module's comment for why
  // nothing posted here is more than the view may see.
  target?.contentWindow?.postMessage(message, "*");
}

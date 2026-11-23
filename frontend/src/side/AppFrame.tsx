// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * One sub-tab's view in a sandboxed frame, and the host's side of the
 * conversation with it (`./bridge.ts`, `docs/architecture/mcp-apps.md`).
 *
 * **The frame.** The view is served by the backend under a policy of its own
 * (`viewUrl`) and framed with `sandbox="allow-scripts"` and nothing else:
 * never `allow-same-origin`, so it runs in an opaque origin and can neither
 * read the session, call the API, nor script this page.
 *
 * **The bridge.** Only a message whose `source` is this frame's window, from
 * an opaque origin, is read, and only one `./bridge.ts` makes sense of;
 * anything else is dropped. An opaque origin has no name to post to, so
 * answers go to `"*"`,
 * and they carry only what the view's own server already has. The view's
 * tool calls go to the backend, which relays them to the server with its
 * credential (`POST /robinauts/api/apps/<server>/tools/<tool>`).
 *
 * **The call.** The view is of one call of the agent. Once it has started it
 * is told the call's arguments, and the call's result once the tool has
 * answered: read whole from the backend, since the chat holds its text
 * alone. A later call with the same tool and arguments is this sub-tab's
 * too, and its result is told to the view already here.
 *
 * **Loading.** The view is hidden until it has been told a result. A view
 * that never starts, or a first result that cannot be read, is said so in
 * its place, with a way to try again. A later call's result that cannot be
 * read leaves the view as it is, showing what it was told before.
 */
import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";

import { detailOf, request } from "../api/client";
import { type SubTab, viewUrl } from "./apps";
import {
  type BoxSize,
  callToolResult,
  dispatch,
  type HostTheme,
  hostContextChanged,
  hostTheme,
  type Outbound,
  parseInbound,
  type Phase,
  resourceTeardown,
  response,
  toolCallFailed,
  toolCancelled,
  toolInput,
  toolResult,
} from "./bridge";

/** How long a view that has loaded has to ask `ui/initialize`. */
export const START_TIMEOUT_MS = 10_000;

/**
 * The origin a message from a sandboxed frame is from: the frame has no
 * `allow-same-origin`, so its origin is opaque, which a message spells
 * `"null"`.
 */
export const OPAQUE_ORIGIN = "null";

const DARK = "(prefers-color-scheme: dark)";

function systemDark(): boolean {
  try {
    return window.matchMedia(DARK).matches;
  } catch {
    return false;
  }
}

function subscribeToTheme(changed: () => void): () => void {
  // The choice is an attribute on <html> (oneberth's theme toggle); the
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

/** The box the frame is drawn in; `null` for a frame not on the screen. */
function sizeOf(frame: HTMLIFrameElement | null): BoxSize | null {
  if (frame === null) return null;
  const size = { width: frame.clientWidth, height: frame.clientHeight };
  return size.width > 0 && size.height > 0 ? size : null;
}

type FrameState =
  | { status: "loading" }
  | { status: "ready" }
  | { status: "failed"; detail: string };

export function AppFrame({
  agent,
  server,
  conversation,
  sub,
  onRetry,
}: Readonly<{
  agent: string;
  server: string;
  /** The conversation the call is of, which its result is read from. */
  conversation: string;
  sub: SubTab;
  onRetry: () => void;
}>) {
  const theme = useHostTheme();
  const frame = useRef<HTMLIFrameElement>(null);
  const [state, setState] = useState<FrameState>({ status: "loading" });
  const [loaded, setLoaded] = useState(false);
  // How many times the view has started: each start is told the call again.
  const [starts, setStarts] = useState(0);
  // Read by the listener, which outlives the render that made it.
  const shownTheme = useRef(theme);
  const phase = useRef<Phase>("waiting");
  // The theme and the size the view was last told, once it has been told.
  const told = useRef<HostTheme | null>(null);
  const toldSize = useRef<BoxSize | null>(null);
  const args = useRef(sub.arguments);

  useEffect(() => {
    shownTheme.current = theme;
    if (told.current === null || told.current === theme) return;
    told.current = theme;
    post(frame.current, hostContextChanged({ theme }));
  }, [theme]);

  const tool = sub.tool;
  useEffect(() => {
    const target = frame.current;
    const dropped = new AbortController();

    const onMessage = (event: MessageEvent) => {
      if (
        target === null ||
        event.origin !== OPAQUE_ORIGIN ||
        event.source === null ||
        event.source !== target.contentWindow
      ) {
        return;
      }
      const message = parseInbound(event.data);
      if (message === null) return;
      const size = sizeOf(target);
      const next = dispatch(message, phase.current, shownTheme.current, size);
      phase.current = next.phase;
      const { step } = next;
      switch (step.kind) {
        case "reply":
          if (
            message.kind === "request" &&
            message.method === "ui/initialize"
          ) {
            told.current = shownTheme.current;
            toldSize.current = size;
          }
          post(target, step.message);
          return;
        case "start":
          post(target, toolInput(args.current));
          setStarts((count) => count + 1);
          return;
        case "call":
          request("post", "/robinauts/api/apps/{server}/tools/{tool}", {
            path: { server, tool: step.name },
            body: { agent, arguments: { ...step.arguments } },
            signal: dropped.signal,
          }).then(
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
    return () => {
      window.removeEventListener("message", onMessage);
      dropped.abort();
    };
  }, [agent, server]);

  // The call's result, told once the view has started and the tool has
  // answered; a call with none to come is told as cancelled. A later call of
  // this sub-tab is told the same way, and so is a view that starts again.
  const { messageId, callId, state: called } = sub.call;
  useEffect(() => {
    if (starts === 0 || called === "running") return undefined;
    if (called === "unanswered") {
      post(frame.current, toolCancelled());
      return undefined;
    }
    const dropped = new AbortController();
    request(
      "get",
      "/robinauts/api/conversations/{conversation_id}/messages/{message_id}/tool-result",
      {
        path: { conversation_id: conversation, message_id: messageId },
        query: { call_id: callId },
        signal: dropped.signal,
      },
    ).then(
      (answer) => {
        if (dropped.signal.aborted) return;
        post(frame.current, toolResult(answer));
        setState({ status: "ready" });
      },
      (failure: unknown) => {
        if (dropped.signal.aborted) return;
        // A view that is showing a result is left as it is: only a view with
        // nothing to show yet has this said in its place.
        setState((was) =>
          was.status === "ready"
            ? was
            : { status: "failed", detail: detailOf(failure) },
        );
      },
    );
    return () => {
      dropped.abort();
    };
  }, [starts, conversation, messageId, callId, called]);

  // The size of the box, told when it changes: the panel was resized, or
  // the frame came on the screen.
  useEffect(() => {
    const target = frame.current;
    if (target === null || typeof ResizeObserver === "undefined") {
      return undefined;
    }
    const observer = new ResizeObserver(() => {
      const size = sizeOf(target);
      const was = toldSize.current;
      if (phase.current !== "running" || size === null) return;
      if (was?.width === size.width && was.height === size.height) return;
      toldSize.current = size;
      post(target, hostContextChanged({ size }));
    });
    observer.observe(target);
    return () => {
      observer.disconnect();
    };
  }, []);

  // Told as the frame goes, while it is still in the page: a layout
  // effect's end runs before the frame is taken out.
  useLayoutEffect(() => {
    const target = frame.current;
    return () => {
      if (phase.current !== "waiting") post(target, resourceTeardown());
    };
  }, []);

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

  // A view told its call was cancelled has nothing more to wait for.
  const cancelled = starts > 0 && called === "unanswered";
  const status =
    state.status === "loading" && cancelled ? "ready" : state.status;

  return (
    <div className="side-app" data-state={status}>
      {status === "loading" && (
        <p role="status" className="side-app-status">
          Loading {sub.title}…
        </p>
      )}
      {state.status === "failed" && (
        <div role="alert" className="side-app-status">
          <p>{sub.title} could not be opened.</p>
          <p className="side-app-detail">{state.detail}</p>
          <button type="button" onClick={onRetry} className="side-app-retry">
            Try again
          </button>
        </div>
      )}
      <iframe
        ref={frame}
        src={viewUrl(agent, server, tool)}
        title={sub.title}
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

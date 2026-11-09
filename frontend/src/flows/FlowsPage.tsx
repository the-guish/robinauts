// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * "Flows", at `#/flows…`: the orchestrator's own page, framed where the chat
 * would be (`docs/architecture/orchestration.md`).
 *
 * The server says where that page is, in the session's `flows_url`, and only
 * when it serves one. It is opened embedded, with no navigation of its own:
 * the panel's "Flows" section is the navigation (`./orchestrator.ts`). The
 * page is the server's, on its origin, so the frame needs no sandbox
 * attribute to reach its own API, and the server's headers let this page
 * alone frame it.
 *
 * **The frame is loaded once** and then moved by its hash, both ways:
 *
 * - a route of the shell's -- a link in the panel, Back, a link pasted --
 *   moves the frame, by replacing its hash, which reloads nothing and takes
 *   no step of its own in the history;
 * - a move inside the frame -- a run opened from the list, a run started --
 *   moves the shell's route in place, so the address bar and the panel say
 *   where the frame is. The frame's move already took its step in the
 *   history, so Back undoes both at once.
 */
import { useEffect, useRef, useState } from "react";

import { replaceRoute, type FlowsView } from "../router";
import { frameSrc, framedHash, sameView, viewOfFramed } from "./orchestrator";
import "./flows.css";

/** The frame's window, once it holds the orchestrator's page. */
function loaded(frame: HTMLIFrameElement | null, url: string): Window | null {
  const framed = frame?.contentWindow ?? null;
  try {
    return framed !== null &&
      new URL(url, location.href).pathname === framed.location.pathname
      ? framed
      : null;
  } catch {
    // Not this origin's page any more: nothing to move.
    return null;
  }
}

export function FlowsPage({
  url,
  view,
}: Readonly<{ url: string; view: FlowsView }>) {
  const frame = useRef<HTMLIFrameElement>(null);
  // The address it is first opened at; after that it moves by its hash.
  const [src] = useState(() => frameSrc(url, view));
  const shown = useRef(view);

  // The shell's route, to the frame.
  useEffect(() => {
    shown.current = view;
    const framed = loaded(frame.current, url);
    if (framed === null) return;
    const at = viewOfFramed(framed.location.hash);
    if (at !== null && sameView(at, view)) return;
    const { pathname, search } = framed.location;
    framed.location.replace(`${pathname}${search}${framedHash(view)}`);
  }, [url, view]);

  // The frame's moves, to the shell's route.
  const [framed, setFramed] = useState<Window | null>(null);
  useEffect(() => {
    if (framed === null) return;
    const moved = () => {
      const at = viewOfFramed(framed.location.hash);
      if (at === null || sameView(at, shown.current)) return;
      shown.current = at;
      replaceRoute({ kind: "flows", view: at });
    };
    framed.addEventListener("hashchange", moved);
    return () => {
      framed.removeEventListener("hashchange", moved);
    };
  }, [framed]);

  return (
    <div className="flows">
      <iframe
        ref={frame}
        title="Flows"
        src={src}
        onLoad={() => {
          setFramed(loaded(frame.current, url));
        }}
        className="flows-frame"
      />
    </div>
  );
}

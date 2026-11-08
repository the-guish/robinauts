// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The right side panel: tabs about the conversation on the screen
 * (`docs/specs/frontend.md`).
 *
 * **Ours**, like the left panel (`../shell/Panel.tsx`), and shaped like it:
 *
 * - **the rail.** On a wide screen it collapses to a thin rail holding its
 *   toggle, and this browser remembers which (`SIDE_KEY`). It starts as the
 *   rail, so a browser that never opened it keeps the chat as wide as it was.
 * - **the width.** Its left edge is a separator that a pointer drags and the
 *   arrow keys move, between `MIN_WIDTH` and `MAX_SHARE` of the window; the
 *   width is remembered too (`SIDE_WIDTH_KEY`).
 * - **the drawer.** Below the breakpoint of `../shell/breakpoint.ts` it is an
 *   overlay drawer from the right edge, opened from a button the shell
 *   draws; the shell holds whether it is open, as it does for the left one.
 *
 * What is in it is `./SideTabs.tsx`, remounted per conversation: each
 * conversation has its own open tabs.
 */
import {
  type CSSProperties,
  type KeyboardEvent,
  type PointerEvent,
  type RefObject,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";

import { PanelRightIcon, XIcon } from "../icons";
import { useWide } from "../shell/breakpoint";
import { useLockedScroll, useTrappedFocus } from "../shell/drawer";
import { remember, remembered } from "../shell/storage";
import { SideTabs } from "./SideTabs";
import type { TabOffer, TabRenderers } from "./tabs";
import "./side.css";

/** What the button that opens the drawer says it controls. */
export const SIDE_ID = "robinauts-side";

/** Collapsed to the rail (`"rail"`) or open (`"open"`), per browser. */
export const SIDE_KEY = "side";

/** The width, in CSS pixels, per browser. */
export const SIDE_WIDTH_KEY = "side.width";

export const MIN_WIDTH = 260;
export const DEFAULT_WIDTH = 320;
/** The most of the window it may take. */
export const MAX_SHARE = 0.6;
/** How far one arrow key moves the edge. */
export const STEP = 16;

export interface SidePanelProps {
  /** Whose tabs to show: a conversation id, or `NEW_CHAT_TABS`. */
  conversation: string;
  /** What the [+] menu can open, in its order. */
  offers: readonly TabOffer[];
  renderers: TabRenderers;
  /** Open as an overlay drawer. Only on a small screen. */
  drawer: boolean;
  onCloseDrawer: () => void;
  /** What is focused when the drawer opens. */
  closeRef: RefObject<HTMLButtonElement | null>;
}

function clamp(width: number, max: number): number {
  return Math.round(Math.min(Math.max(width, MIN_WIDTH), max));
}

function useCollapsed(): [boolean, (collapsed: boolean) => void] {
  const [collapsed, setCollapsed] = useState(
    () => remembered(SIDE_KEY) !== "open",
  );
  return [
    collapsed,
    (next: boolean) => {
      setCollapsed(next);
      remember(SIDE_KEY, next ? "rail" : "open");
    },
  ];
}

function rememberedWidth(): number {
  const width = Number(remembered(SIDE_WIDTH_KEY) ?? Number.NaN);
  return Number.isFinite(width) && width > 0 ? width : DEFAULT_WIDTH;
}

function subscribeToResize(changed: () => void): () => void {
  window.addEventListener("resize", changed);
  return () => {
    window.removeEventListener("resize", changed);
  };
}

/** The window's width, as a value React redraws on. */
function useViewportWidth(): number {
  return useSyncExternalStore(
    subscribeToResize,
    () => window.innerWidth,
    () => 1024,
  );
}

export function SidePanel({
  conversation,
  offers,
  renderers,
  drawer,
  onCloseDrawer,
  closeRef,
}: Readonly<SidePanelProps>) {
  const asDrawer = !useWide();
  const open = asDrawer && drawer;
  const offScreen = asDrawer && !drawer;
  const self = useRef<HTMLElement>(null);
  useTrappedFocus(self, open);
  useLockedScroll(open);

  const [collapsed, setCollapsed] = useCollapsed();
  // The rail is only a rail beside the page; a drawer is always drawn whole.
  const rail = collapsed && !asDrawer;

  const [width, setWidth] = useState(rememberedWidth);
  const max = Math.max(MIN_WIDTH, Math.floor(useViewportWidth() * MAX_SHARE));
  const shown = clamp(width, max);
  const resizeTo = (next: number, keep: boolean) => {
    const clamped = clamp(next, max);
    setWidth(clamped);
    if (keep) remember(SIDE_WIDTH_KEY, String(clamped));
  };

  return (
    <aside
      id={SIDE_ID}
      ref={self}
      aria-label="Side panel"
      // As the left drawer: closed, it is off the screen and out of the way.
      inert={offScreen}
      {...(open ? { role: "dialog" as const, "aria-modal": true } : {})}
      data-collapsed={rail}
      data-drawer={drawer ? "open" : "closed"}
      className="side"
      // The width is the one value here that is genuinely dynamic.
      style={{ "--side-width": `${String(shown)}px` } as CSSProperties}
    >
      {!asDrawer && !rail && (
        <Resizer width={shown} max={max} onResize={resizeTo} />
      )}
      <div className="side-head">
        <button
          type="button"
          title={rail ? "Expand the side panel" : "Collapse the side panel"}
          aria-label={
            rail ? "Expand the side panel" : "Collapse the side panel"
          }
          aria-expanded={!rail}
          onClick={() => {
            setCollapsed(!collapsed);
          }}
          className="side-tool side-collapse"
        >
          <PanelRightIcon className="side-icon" />
        </button>
        <button
          type="button"
          ref={closeRef}
          title="Close the side panel"
          aria-label="Close the side panel"
          onClick={onCloseDrawer}
          className="side-tool side-close"
        >
          <XIcon className="side-icon" />
        </button>
      </div>
      {/* Hidden in the rail rather than unmounted: a tab keeps what it
          holds while the panel is folded away. */}
      <SideTabs
        key={conversation}
        conversation={conversation}
        offers={offers}
        renderers={renderers}
        hidden={rail}
      />
    </aside>
  );
}

/**
 * The left edge, which a pointer drags and the arrow keys move.
 *
 * A focusable `separator` with a value is how ARIA names a splitter: its
 * value is the panel's width, and the edge being on the panel's left, Left
 * widens it and Right narrows it. Home and End go to the two ends.
 */
function Resizer({
  width,
  max,
  onResize,
}: Readonly<{
  width: number;
  max: number;
  onResize: (width: number, keep: boolean) => void;
}>) {
  // Where a drag began: the pointer's x and the width then.
  const drag = useRef<{ x: number; width: number } | null>(null);
  const [dragging, setDragging] = useState(false);

  const follow = (event: PointerEvent<HTMLDivElement>, keep: boolean) => {
    const from = drag.current;
    if (from === null) return;
    onResize(from.width + (from.x - event.clientX), keep);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const next = new Map([
      ["ArrowLeft", width + STEP],
      ["ArrowRight", width - STEP],
      ["Home", 0],
      ["End", max],
    ]).get(event.key);
    if (next === undefined) return;
    event.preventDefault();
    onResize(next, true);
  };

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize the side panel"
      aria-controls={SIDE_ID}
      aria-valuemin={MIN_WIDTH}
      aria-valuemax={max}
      aria-valuenow={width}
      tabIndex={0}
      data-dragging={dragging}
      className="side-resize"
      onKeyDown={onKeyDown}
      onPointerDown={(event) => {
        if (event.button !== 0) return;
        event.preventDefault();
        drag.current = { x: event.clientX, width };
        setDragging(true);
        try {
          event.currentTarget.setPointerCapture(event.pointerId);
        } catch {
          // No capture (an old browser, a test without layout): the drag
          // still follows the pointer while it is over the edge.
        }
      }}
      onPointerMove={(event) => {
        follow(event, false);
      }}
      onPointerUp={(event) => {
        follow(event, true);
        drag.current = null;
        setDragging(false);
      }}
      onPointerCancel={() => {
        // Taken away by the browser: what was reached stays, and is kept.
        onResize(width, true);
        drag.current = null;
        setDragging(false);
      }}
    />
  );
}

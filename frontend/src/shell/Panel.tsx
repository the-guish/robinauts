// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The left panel: the collapse button and the brand, "new chat", the
 * history, and the profile block pinned to the bottom
 * (`docs/specs/frontend.md`).
 *
 * **The panel is ours** (ADR 0001, kept by ADR 0006): it reads the API
 * directly and knows nothing of what draws the chat, and nothing here ever
 * will.
 *
 * There is **no projects section**: projects are out of the POC. The spec's
 * panel has one, and it goes here, between the history and the profile
 * block, when they exist.
 *
 * The shape is neorc's, written again for this project; recorded in
 * `docs/legal/ip-clearance.md`.
 */
import { useRef, useState, type RefObject } from "react";

import { ApiError } from "../api/client";
import type { ConversationId } from "../chat";
import type { History } from "../history/history";
import { HistoryList } from "../history/HistoryList";
import {
  LogOutIcon,
  MonitorIcon,
  PanelLeftIcon,
  PlusIcon,
  XIcon,
} from "../icons";
import { DEVICES, formatRoute } from "../router";
import type { User } from "../session/session";
import { useWide } from "./breakpoint";
import { useLockedScroll, useTrappedFocus } from "./drawer";
import { ThemeToggle } from "./ThemeToggle";
import "./panel.css";

/** What the button that opens the drawer says it controls. */
export const PANEL_ID = "robinauts-panel";

export interface PanelProps {
  user: User | null;
  /** The conversations, and what can be done to one. */
  history: History;
  /** The conversation the page is on, so the list can mark it. */
  current: ConversationId | null;
  /** Whether the page is "Linked devices", so its link can say so. */
  onDevices?: boolean;
  /** Collapsed to a rail of icons. Only on a screen wide enough for one. */
  collapsed: boolean;
  onCollapse: (collapsed: boolean) => void;
  /** Open as an overlay drawer. Only on a small screen. */
  drawer: boolean;
  onCloseDrawer: () => void;
  onNewChat: () => void;
  signOut: () => Promise<void>;
  /** Whether there is a sign-in to end. The local mode has none. */
  signInConfigured: boolean;
  /** What is focused when the drawer opens. */
  closeRef: RefObject<HTMLButtonElement | null>;
}

export function Panel({
  user,
  history,
  current,
  onDevices = false,
  collapsed,
  onCollapse,
  drawer,
  onCloseDrawer,
  onNewChat,
  signOut,
  signInConfigured,
  closeRef,
}: Readonly<PanelProps>) {
  // Below the breakpoint the panel *is* the drawer; above it, it stands
  // beside the page and none of what follows applies.
  const asDrawer = !useWide();
  const open = asDrawer && drawer;
  const offScreen = asDrawer && !drawer;
  const self = useRef<HTMLElement>(null);
  useTrappedFocus(self, open);
  useLockedScroll(open);
  return (
    <aside
      id={PANEL_ID}
      ref={self}
      aria-label="Panel"
      // A closed drawer is translated off the screen, not removed, so
      // without this it is a handful of invisible tab stops -- sign-out
      // among them -- that a keyboard walks into and cannot see.
      inert={offScreen}
      // Open, it covers the page and is the only thing to be in.
      {...(open ? { role: "dialog" as const, "aria-modal": true } : {})}
      // The two attributes are what `./panel.css` draws the rail and the
      // drawer by: what is hidden in the rail, and where the drawer is.
      data-collapsed={collapsed}
      data-drawer={drawer ? "open" : "closed"}
      className="panel"
    >
      <div className="panel-head">
        <button
          type="button"
          title={collapsed ? "Expand the panel" : "Collapse the panel"}
          aria-label={collapsed ? "Expand the panel" : "Collapse the panel"}
          aria-expanded={!collapsed}
          onClick={() => {
            onCollapse(!collapsed);
          }}
          className="panel-tool panel-collapse"
        >
          <PanelLeftIcon className="panel-icon" />
        </button>
        <button
          type="button"
          ref={closeRef}
          title="Close the panel"
          aria-label="Close the panel"
          onClick={onCloseDrawer}
          className="panel-tool panel-close"
        >
          <XIcon className="panel-icon" />
        </button>
        {/* The brand is text. No logo image: this project ships no mark. */}
        <span className="panel-brand panel-wide">Robinauts</span>
      </div>

      <button
        type="button"
        title="New chat"
        onClick={onNewChat}
        className="panel-new"
      >
        <PlusIcon className="panel-icon" />
        <span className="panel-wide">New chat</span>
      </button>

      {/* The one part of the panel that scrolls: a long history must not
          push the profile block off the bottom of the screen. */}
      <nav aria-label="History" className="panel-history panel-wide">
        <h2 className="panel-heading">History</h2>
        <HistoryList
          history={history}
          current={current}
          onOpened={onCloseDrawer}
        />
      </nav>

      {/* Everything that is not the history sits at the foot of the panel,
          because `docs/specs/frontend.md` leaves no top bar to put it in.
          In the rail the toggle goes with the labels: three buttons do not
          fit in it, and the theme is not what a rail is for. */}
      <div className="panel-foot">
        <div className="panel-wide">
          <ThemeToggle />
        </div>
        <ProfileBlock
          user={user}
          signOut={signOut}
          signInConfigured={signInConfigured}
          onDevices={onDevices}
          onLeft={onCloseDrawer}
        />
      </div>
    </aside>
  );
}

/**
 * Who is signed in, their linked devices, and the way out. Pinned to the
 * bottom of the panel.
 */
function ProfileBlock({
  user,
  signOut,
  signInConfigured,
  onDevices,
  onLeft,
}: Readonly<{
  user: User | null;
  signOut: () => Promise<void>;
  signInConfigured: boolean;
  onDevices: boolean;
  /** The drawer has somewhere else to be: a page, or the sign-in page. */
  onLeft: () => void;
}>) {
  const [leaving, setLeaving] = useState(false);
  const [refused, setRefused] = useState<string | null>(null);
  if (user === null) return null;
  const name = user.name ?? user.email ?? "Signed in";
  const leave = async () => {
    setLeaving(true);
    setRefused(null);
    try {
      await signOut();
      // The drawer belongs to a page that is about to be replaced by the
      // sign-in page; left open it would cover it.
      onLeft();
    } catch (failure) {
      // The session is still open, and the reader has to be told: a button
      // that quietly did nothing is worse than a refusal.
      setRefused(
        failure instanceof ApiError ? failure.detail : String(failure),
      );
    } finally {
      // However it went. Usually this component is on its way out, and
      // setting state on one that has gone is a no-op; when it is not --
      // signing out succeeded but the page is still here -- a button left
      // disabled is one nobody can press again.
      setLeaving(false);
    }
  };
  return (
    // A region rather than a labelled div: a name on a <div> names nothing.
    <section aria-label="Profile" className="panel-profile">
      {/* The initial is a picture of the name, and in the rail it is the
          only thing left of the profile, so it says whose it is rather than
          being hidden from a reader who cannot see it. */}
      <span role="img" aria-label={name} title={name} className="panel-avatar">
        {name.slice(0, 1).toUpperCase()}
      </span>
      <span className="panel-who panel-wide">
        <span className="panel-name" title={name}>
          {name}
        </span>
        {user.email !== undefined && user.email !== null && (
          <span className="panel-email" title={user.email}>
            {user.email}
          </span>
        )}
      </span>
      {/* Outside the block above, so that the rail keeps them: collapsed,
          each is the icon alone, with its name on the control rather than
          beside it. "Linked devices" is a link, since it goes to a page;
          opened from the drawer it closes the drawer, as a conversation
          does. In the local development mode there is no session to end --
          the route answers 204 and the same local user comes back -- and a
          button that changes nothing is worse than saying why there is
          none. */}
      <div className="panel-account">
        <a
          href={formatRoute(DEVICES)}
          title="Linked devices"
          aria-label="Linked devices"
          aria-current={onDevices ? "page" : undefined}
          onClick={onLeft}
          className="panel-devices"
        >
          <MonitorIcon className="panel-icon-small" />
          <span className="panel-wide">Linked devices</span>
        </a>
        {signInConfigured ? (
          <button
            type="button"
            onClick={() => void leave()}
            disabled={leaving}
            title="Sign out"
            aria-label="Sign out"
            className="panel-signout"
          >
            <LogOutIcon className="panel-icon-small" />
            <span className="panel-wide">Sign out</span>
          </button>
        ) : (
          <span className="panel-nosignin panel-wide">Sign-in is off</span>
        )}
      </div>
      {refused !== null && (
        <span role="alert" className="panel-refused">
          Not signed out: {refused}
        </span>
      )}
    </section>
  );
}

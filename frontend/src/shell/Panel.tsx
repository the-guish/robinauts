// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The left panel: the collapse button and the brand, "new chat" and the
 * history, and the profile block pinned to the bottom
 * (`docs/specs/frontend.md`).
 *
 * **Sessions or Flows.** When the server has an orchestrator, a switch of two
 * tabs under the brand chooses what the middle of the panel holds: the
 * sessions -- "New chat" and the history -- or the orchestrator's navigation
 * (`../flows/FlowsSection.tsx`). One at a time, each with the whole height,
 * so a long history and a long list of runs never squeeze each other. The
 * tab follows the page: a flows page shows Flows, a conversation Sessions;
 * the reader may switch without leaving the page, and the browser remembers
 * the last choice. Both tabs stay mounted, the one not shown hidden. With no
 * orchestrator there is no switch.
 *
 * **The panel is ours** (ADR 0001, kept by ADR 0006): it reads the API
 * directly and knows nothing of what draws the chat, and nothing here ever
 * will.
 *
 * **The history is drawn a project at a time** (`../projects/`): each
 * project folds away under its heading, and "New project", at the head of
 * the history, makes another. Projects are the user's own; sharing one
 * with other people is not there yet.
 *
 * The shape is neorc's, written again for this project; recorded in
 * `docs/legal/ip-clearance.md`.
 */
import {
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
  type RefObject,
} from "react";

import { ApiError, detailOf } from "../api/client";
import type { ConversationId } from "../chat";
import type { History } from "../history/history";
import { HistoryList } from "../history/HistoryList";
import {
  CheckIcon,
  LogOutIcon,
  MessageSquareIcon,
  MonitorIcon,
  PanelLeftIcon,
  PlusIcon,
  WorkflowIcon,
  XIcon,
} from "../icons";
import {
  MAX_PROJECT_NAME_CHARS,
  type Folded,
  type Project,
} from "../projects/projects";
import { DEVICES, formatRoute, type FlowsView } from "../router";
import { FlowsSection } from "../flows/FlowsSection";
import type { User } from "../session/session";
import { useWide } from "./breakpoint";
import { useLockedScroll, useTrappedFocus } from "./drawer";
import { remember, remembered } from "./storage";
import { ThemeToggle } from "./ThemeToggle";
import "./panel.css";

/** What the button that opens the drawer says it controls. */
export const PANEL_ID = "robinauts-panel";

/** Where the browser keeps which of the panel's two tabs was last shown. */
export const PANEL_TAB_KEY = "panel.tab";

/** The panel's two tabs. */
export type PanelTab = "sessions" | "flows";

export interface PanelProps {
  user: User | null;
  /** The conversations, and what can be done to one. */
  history: History;
  /** The conversation the page is on, so the list can mark it. */
  current: ConversationId | null;
  /** The user's projects, once they have come, to group the history by. */
  projects?: readonly Project[] | null;
  /** Which projects are folded away. */
  folded?: Folded;
  /** Start a new chat in that project. */
  onNewChatIn?: (project: string) => void;
  /** Make a project of that name; a refusal is thrown, to be shown here. */
  onNewProject?: (name: string) => Promise<void>;
  /** Whether the page is "Linked devices", so its link can say so. */
  onDevices?: boolean;
  /**
   * Where the server serves an orchestrator's page, which the "Flows" tab
   * opens; with none, there is no "Flows" (`../flows/`).
   */
  flowsUrl?: string | null;
  /** Which of its pages is up, if one is, so its link can say so. */
  flowsView?: FlowsView | null;
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
  projects = null,
  folded,
  onNewChatIn,
  onNewProject,
  onDevices = false,
  flowsUrl = null,
  flowsView = null,
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
  const hasFlows = flowsUrl !== null;
  const [tab, setTab] = useShownTab(flowsView, current);
  const shown: PanelTab = hasFlows ? tab : "sessions";
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

      {hasFlows && <PanelTabs shown={shown} onShow={setTab} />}

      {/* Both tabs stay mounted and the one not shown is hidden, so a
          switch costs nothing and loses nothing: the history keeps where it
          was scrolled to and what was typed into it, and the Flows lists are
          there at once, kept up to date all along, as when they sat under
          the history. */}
      <PanelView tab="sessions" shown={shown} framed={hasFlows}>
        <button
          type="button"
          title="New chat"
          onClick={onNewChat}
          className="panel-new"
        >
          <PlusIcon className="panel-icon" />
          <span className="panel-wide">New chat</span>
        </button>

        {/* The one part of the panel that scrolls: a long history must
                not push the profile block off the bottom of the screen. It
                has no heading: the tab above it says what it is. */}
        <nav aria-label="History" className="panel-history panel-wide">
          {onNewProject !== undefined && (
            <div className="panel-history-head">
              <NewProject onNewProject={onNewProject} />
            </div>
          )}
          <HistoryList
            history={history}
            current={current}
            onOpened={onCloseDrawer}
            projects={projects}
            {...(folded === undefined ? {} : { folded })}
            {...(onNewChatIn === undefined
              ? {}
              : {
                  onNewChatIn: (project: string) => {
                    onNewChatIn(project);
                    onCloseDrawer();
                  },
                })}
          />
        </nav>
      </PanelView>
      {flowsUrl !== null && (
        // The orchestrator's navigation, which its framed page leaves out,
        // so there is one panel and one menu button.
        <PanelView tab="flows" shown={shown} framed>
          <FlowsSection
            url={flowsUrl}
            view={flowsView}
            onOpened={onCloseDrawer}
          />
        </PanelView>
      )}

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
 * Which tab is shown: the one the browser remembers, until the page says
 * otherwise. Arriving at a flows page shows Flows, opening a conversation
 * shows Sessions; in between, the reader's own choice stands, on any page.
 *
 * The page is compared by its address, not by the object that describes it,
 * which is a new one at every render and would undo every choice.
 */
function useShownTab(
  flowsView: FlowsView | null,
  current: ConversationId | null,
): [PanelTab, (tab: PanelTab) => void] {
  const flowsPage =
    flowsView === null ? null : formatRoute({ kind: "flows", view: flowsView });
  const [tab, setTab] = useState<PanelTab>(() =>
    flowsPage !== null
      ? "flows"
      : current !== null
        ? "sessions"
        : remembered(PANEL_TAB_KEY) === "flows"
          ? "flows"
          : "sessions",
  );
  // The page the tab last followed; a new one is followed while rendering,
  // so the panel never draws a frame with the wrong tab.
  const [followed, setFollowed] = useState({ flowsPage, current });
  if (followed.flowsPage !== flowsPage || followed.current !== current) {
    setFollowed({ flowsPage, current });
    if (flowsPage !== null && flowsPage !== followed.flowsPage) {
      setTab("flows");
    } else if (current !== null && current !== followed.current) {
      setTab("sessions");
    }
  }
  useEffect(() => {
    remember(PANEL_TAB_KEY, tab);
  }, [tab]);
  return [tab, setTab];
}

const TABS: readonly {
  tab: PanelTab;
  label: string;
  Icon: typeof MessageSquareIcon;
}[] = [
  { tab: "sessions", label: "Sessions", Icon: MessageSquareIcon },
  { tab: "flows", label: "Flows", Icon: WorkflowIcon },
];

/** The id of a tab, and of the tab panel it controls. */
const tabId = (tab: PanelTab) => `${PANEL_ID}-tab-${tab}`;
const viewId = (tab: PanelTab) => `${PANEL_ID}-view-${tab}`;

/**
 * The switch: two tabs, the shown one marked. One tab stop for the pair, and
 * the arrow keys, Home and End move between them and show the one they land
 * on, as a tab list does. In the rail it is a column of the two icons, so
 * up and down move too.
 */
function PanelTabs({
  shown,
  onShow,
}: Readonly<{ shown: PanelTab; onShow: (tab: PanelTab) => void }>) {
  const buttons = useRef(new Map<PanelTab, HTMLButtonElement>());
  const go = (to: PanelTab) => {
    onShow(to);
    buttons.current.get(to)?.focus();
  };
  const onKeyDown = (event: KeyboardEvent) => {
    const at = TABS.findIndex((entry) => entry.tab === shown);
    const last = TABS.length - 1;
    const to =
      event.key === "ArrowRight" || event.key === "ArrowDown"
        ? at === last
          ? 0
          : at + 1
        : event.key === "ArrowLeft" || event.key === "ArrowUp"
          ? at === 0
            ? last
            : at - 1
          : event.key === "Home"
            ? 0
            : event.key === "End"
              ? last
              : null;
    const entry = to === null ? undefined : TABS[to];
    if (entry === undefined) return;
    event.preventDefault();
    go(entry.tab);
  };
  return (
    <div
      role="tablist"
      aria-label="Panel"
      onKeyDown={onKeyDown}
      className="panel-tabs"
    >
      {TABS.map(({ tab, label, Icon }) => (
        <button
          key={tab}
          ref={(button) => {
            if (button === null) buttons.current.delete(tab);
            else buttons.current.set(tab, button);
          }}
          type="button"
          role="tab"
          id={tabId(tab)}
          aria-selected={tab === shown}
          aria-controls={viewId(tab)}
          tabIndex={tab === shown ? 0 : -1}
          title={label}
          onClick={() => {
            onShow(tab);
          }}
          className="panel-tab"
        >
          <Icon className="panel-icon-small" />
          <span className="panel-wide">{label}</span>
        </button>
      ))}
    </div>
  );
}

/**
 * What one tab holds, between the switch and the foot: under a switch, that
 * tab's tab panel, hidden while the other is shown; with none, only the box
 * that lets the history scroll.
 */
function PanelView({
  tab,
  shown,
  framed,
  children,
}: Readonly<{
  tab: PanelTab;
  shown: PanelTab;
  framed: boolean;
  children: ReactNode;
}>) {
  return (
    <div
      {...(framed
        ? {
            id: viewId(tab),
            role: "tabpanel",
            "aria-labelledby": tabId(tab),
          }
        : {})}
      hidden={tab !== shown}
      className="panel-view"
    >
      {children}
    </div>
  );
}

/**
 * "New project": a button that opens a name box in its place. Enter or the
 * check makes it, Escape or the cross leaves it; a refusal is said under the
 * box, which keeps what was typed.
 */
function NewProject({
  onNewProject,
}: Readonly<{ onNewProject: (name: string) => Promise<void> }>) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [refused, setRefused] = useState<string | null>(null);
  const box = useRef<HTMLInputElement>(null);
  const opener = useRef<HTMLButtonElement>(null);
  const wasOpen = useRef(false);
  useEffect(() => {
    if (open) box.current?.focus();
    else if (wasOpen.current) opener.current?.focus();
    wasOpen.current = open;
  }, [open]);
  const close = () => {
    setOpen(false);
    setName("");
    setRefused(null);
  };
  const save = async () => {
    if (busy) return;
    const trimmed = name.trim();
    if (trimmed === "") {
      setRefused("A project needs a name.");
      return;
    }
    if ([...trimmed].length > MAX_PROJECT_NAME_CHARS) {
      setRefused(
        `A project's name is at most ${MAX_PROJECT_NAME_CHARS} characters long.`,
      );
      return;
    }
    setBusy(true);
    setRefused(null);
    try {
      await onNewProject(trimmed);
      close();
    } catch (failure) {
      setRefused(detailOf(failure));
    } finally {
      setBusy(false);
    }
  };
  if (!open) {
    return (
      <button
        ref={opener}
        type="button"
        title="New project"
        aria-label="New project"
        onClick={() => {
          setOpen(true);
        }}
        className="panel-tool panel-new-project"
      >
        <PlusIcon className="panel-icon-small" />
        <span>New project</span>
      </button>
    );
  }
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
      onKeyDown={(event) => {
        if (event.key !== "Escape") return;
        event.stopPropagation();
        close();
      }}
      className="panel-project-form"
    >
      <input
        ref={box}
        aria-label="Project name"
        placeholder="Project name"
        value={name}
        readOnly={busy}
        aria-disabled={busy}
        onChange={(event) => {
          setName(event.target.value);
        }}
        className="panel-project-box"
      />
      <button
        type="submit"
        title="Create the project"
        aria-label="Create the project"
        aria-disabled={busy}
        className="panel-tool"
      >
        <CheckIcon className="panel-icon-small" />
      </button>
      <button
        type="button"
        title="Cancel the new project"
        aria-label="Cancel the new project"
        onClick={close}
        className="panel-tool"
      >
        <XIcon className="panel-icon-small" />
      </button>
      {refused !== null && (
        <p role="alert" className="panel-project-refused">
          {refused}
        </p>
      )}
    </form>
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

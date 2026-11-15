// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The left panel: the collapse button and the brand, "New chat" and the
 * history (`docs/specs/frontend.md`). The theme toggle, the account and
 * sign-out are oneberth's shell's, on its rail.
 *
 * **Where it stands** is the shell's to decide (`ShellPanel`, in
 * `./Workspace.tsx`): beside the page on a wide screen, where it collapses to
 * a rail of icons, and in the shell's drawer on a small one, whole.
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
import { useEffect, useRef, useState } from "react";

import { detailOf } from "../api/client";
import type { ConversationId } from "../chat";
import type { History } from "../history/history";
import { HistoryList } from "../history/HistoryList";
import { CheckIcon, PanelLeftIcon, PlusIcon, XIcon } from "../icons";
import {
  MAX_PROJECT_NAME_CHARS,
  type Folded,
  type Project,
} from "../projects/projects";
import { useWide } from "./breakpoint";
import "./panel.css";

/** The panel's id. */
export const PANEL_ID = "robinauts-panel";

export interface PanelProps {
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
  /** Collapsed to a rail of icons. Only on a screen wide enough for one. */
  collapsed: boolean;
  onCollapse: (collapsed: boolean) => void;
  onNewChat: () => void;
}

export function Panel({
  history,
  current,
  projects = null,
  folded,
  onNewChatIn,
  onNewProject,
  collapsed,
  onCollapse,
  onNewChat,
}: Readonly<PanelProps>) {
  // In the shell's drawer, on a small screen, the panel is drawn whole.
  const wide = useWide();
  const rail = collapsed && wide;
  return (
    <aside
      id={PANEL_ID}
      aria-label="Panel"
      // What `./panel.css` draws the rail by: what is hidden in it.
      data-collapsed={rail}
      className="panel"
    >
      <div className="panel-head">
        <button
          type="button"
          title={rail ? "Expand the panel" : "Collapse the panel"}
          aria-label={rail ? "Expand the panel" : "Collapse the panel"}
          aria-expanded={!rail}
          onClick={() => {
            onCollapse(!rail);
          }}
          className="panel-tool panel-collapse"
        >
          <PanelLeftIcon className="panel-icon" />
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

      {/* The one part of the panel that scrolls. */}
      <nav aria-label="History" className="panel-history panel-wide">
        {onNewProject !== undefined && (
          <div className="panel-history-head">
            <NewProject onNewProject={onNewProject} />
          </div>
        )}
        <HistoryList
          history={history}
          current={current}
          projects={projects}
          {...(folded === undefined ? {} : { folded })}
          {...(onNewChatIn === undefined ? {} : { onNewChatIn })}
        />
      </nav>
    </aside>
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

// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The history, in the panel: a link per conversation, and the two things
 * that can be done to one (`docs/specs/frontend.md`).
 *
 * **Links, not buttons.** A conversation has an address -- `#/c/<id>` -- so
 * the way to it is an `<a href>`: it can be opened in a new tab, copied, and
 * the browser's own "you are here" is `aria-current="page"`.
 *
 * Archiving is one click on the row's archive button, with no question: it
 * takes the conversation off the list and keeps everything, and the next
 * message sent in it brings it back.
 *
 * Renaming and deleting are inline, in the row. No `window.confirm`, which
 * is the browser's dialogue rather than this interface's and which a person
 * cannot read the title of the thing they are deleting in; a "Delete?
 * Yes / No" in the row says what is about to go and can be left alone.
 *
 * Everything here is reachable with a keyboard alone: the actions button is
 * always drawn rather than appearing on hover, the archive button shows
 * while the row has the focus, Escape closes what is open
 * and puts the focus back on the button that opened it, and the focus
 * follows into the rename box and onto "No".
 *
 * **A project at a time**, once the projects are known: each is a group
 * with a heading that folds it away and opens it again, and a "+" that
 * starts a new chat in it. A project nobody has a conversation in yet is
 * still drawn, so that one just made is there to start a chat in. A
 * conversation of a project the list does not know -- made since the
 * projects were last asked for -- goes under the first group, "Default",
 * rather than nowhere.
 */
import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";

import { ApiError, detailOf } from "../api/client";
import type { ConversationId } from "../chat";
import type { ListedConversation } from "../conversation/conversation";
import { shownTitle } from "../conversation/conversation";
import {
  ArchiveIcon,
  CheckIcon,
  ChevronDownIcon,
  EllipsisIcon,
  PencilIcon,
  PlusIcon,
  TrashIcon,
  XIcon,
} from "../icons";
import type { Folded, Project } from "../projects/projects";
import { formatRoute, navigate, NEW_CHAT } from "../router";
import type { History } from "./history";
import "./history.css";

/**
 * What a conversation that is still answering is told when it is deleted.
 *
 * The API refuses with 409 (`RunAlreadyActiveError`), and its detail names
 * the run and its state for an operator's log. What a person needs to know
 * is what to do next (`docs/specs/conversations.md`, "Deletion").
 */
export const STILL_ANSWERING =
  "This conversation is still answering. Stop the answer first, then delete it.";

/**
 * How long a title may be, and what is said of one that is longer.
 *
 * `domain.MAX_TITLE_CHARS`, which the document carries as the field's
 * `maxLength`, and the sentence is the one the route would answer with
 * (`api/errors.py`, `UNREADABLE_RULES["string_too_long"]`). Saying it here
 * saves a round trip and keeps the box open on what was typed.
 *
 * **Counted as the backend counts**, in code points: Python's `len` counts
 * those, and an HTML `maxlength` counts UTF-16 units, so a `maxlength` of
 * 120 would cut a title of 61 emoji that the backend would have taken.
 */
export const MAX_TITLE_CHARS = 120;
export const TOO_LONG = "body.title: is longer than this field allows";

export interface HistoryListProps {
  history: History;
  /** The conversation the page is on, if it is one. */
  current: ConversationId | null;
  /** Opening one closes the drawer it was opened from, on a small screen. */
  onOpened?: () => void;
  /**
   * The user's projects, oldest first, to draw the list a project at a
   * time; left out or `null` -- not asked for yet, or not answered -- the
   * list is drawn as one.
   */
  projects?: readonly Project[] | null;
  /** Which projects are folded away. */
  folded?: Folded;
  /** "+" on a project: start a new chat in it. */
  onNewChatIn?: (project: string) => void;
}

/** The conversations of each project, in the projects' order. */
export function groupByProject(
  projects: readonly Project[],
  items: readonly ListedConversation[],
): { project: Project; items: ListedConversation[] }[] {
  const groups = projects.map((project) => ({
    project,
    items: [] as ListedConversation[],
  }));
  const byId = new Map(groups.map((group) => [group.project.id, group]));
  for (const item of items) {
    (byId.get(item.project_id) ?? groups[0])?.items.push(item);
  }
  return groups;
}

export function HistoryList({
  history,
  current,
  onOpened,
  projects = null,
  folded,
  onNewChatIn,
}: Readonly<HistoryListProps>) {
  const { status, items, more, loadingMore, refreshing, error } = history;
  /**
   * Where the focus goes when the row it was in is not there any more.
   *
   * A write is followed by a fresh first page, and the conversation whose row
   * held the focus may not be on it: deleted, or pushed past the end of the
   * page by something else being written to. The focus would then be dropped
   * on `<body>`, which is nowhere -- a keyboard would have to come back in
   * from the top of the document. The list is somewhere, and `tabindex="-1"`
   * is what lets it be focused without becoming a tab stop of its own.
   */
  const list = useRef<HTMLUListElement>(null);
  const toTheList = useCallback(() => {
    list.current?.focus();
  }, []);
  const row = (conversation: ListedConversation) => (
    <HistoryItem
      key={conversation.id}
      conversation={conversation}
      isCurrent={conversation.id === current}
      history={history}
      onGone={toTheList}
      {...(onOpened === undefined ? {} : { onOpened })}
    />
  );
  const grouped = projects !== null && projects.length > 0;
  return (
    <>
      <ul
        ref={list}
        tabIndex={-1}
        aria-label="Conversations"
        className="history-list"
      >
        {grouped
          ? groupByProject(projects, items).map((group) => (
              <ProjectGroup
                key={group.project.id}
                project={group.project}
                count={group.items.length}
                folded={folded?.has(group.project.id) ?? false}
                onFold={() => folded?.toggle(group.project.id)}
                {...(onNewChatIn === undefined ? {} : { onNewChatIn })}
              >
                {group.items.map(row)}
              </ProjectGroup>
            ))
          : items.map(row)}
      </ul>
      {status === "loading" && (
        <p className="history-note">Loading the conversations…</p>
      )}
      {status === "ready" && items.length === 0 && !grouped && (
        <p className="history-note">No conversations yet.</p>
      )}
      {/* Already a sentence: which ask failed is known where it failed, not
          here (`history.ts`). */}
      {error !== null && (
        <p role="alert" className="history-failed">
          {error}
        </p>
      )}
      {/* Whatever failed -- a first page, a refresh, a page after it -- the
          way out is to ask again. */}
      {error !== null && (
        <button
          type="button"
          onClick={history.refresh}
          className="history-retry"
        >
          Try again
        </button>
      )}
      {more && (
        <button
          type="button"
          onClick={history.loadMore}
          // A page cannot be added to a list that is being replaced: the
          // cursor belongs to the listing the refresh is throwing away.
          disabled={loadingMore || refreshing}
          className="history-more"
        >
          {loadingMore ? "Loading…" : "Load more"}
        </button>
      )}
    </>
  );
}

/**
 * One project: its heading, which folds it away and opens it again, a "+"
 * for a new chat in it, and its conversations.
 *
 * The heading is a button with `aria-expanded` that controls the list under
 * it: a disclosure, which is what a reader is told it is.
 */
function ProjectGroup({
  project,
  count,
  folded,
  onFold,
  onNewChatIn,
  children,
}: Readonly<{
  project: Project;
  count: number;
  folded: boolean;
  onFold: () => void;
  onNewChatIn?: (project: string) => void;
  children: ReactNode;
}>) {
  const listId = useId();
  return (
    <li className="history-project" data-folded={folded}>
      <div className="history-project-head">
        <button
          type="button"
          aria-expanded={!folded}
          aria-controls={listId}
          title={folded ? `Open ${project.name}` : `Fold ${project.name}`}
          onClick={onFold}
          className="history-project-toggle"
        >
          <ChevronDownIcon className="history-icon-small history-chevron" />
          <span className="history-project-name">{project.name}</span>
          <span className="history-project-count">{count}</span>
        </button>
        {onNewChatIn !== undefined && (
          <button
            type="button"
            title={`Start a conversation in ${project.name}`}
            aria-label={`Start a conversation in ${project.name}`}
            onClick={() => {
              onNewChatIn(project.id);
            }}
            className="history-tool"
          >
            <PlusIcon className="history-icon-small" />
          </button>
        )}
      </div>
      <ul
        id={listId}
        aria-label={project.name}
        hidden={folded}
        className="history-project-list"
      >
        {children}
        {count === 0 && <li className="history-note">No conversations yet.</li>}
      </ul>
    </li>
  );
}

/** What a row is showing: the link, its menu, a rename box, or a confirm. */
type Mode = "idle" | "menu" | "rename" | "confirm";

function HistoryItem({
  conversation,
  isCurrent,
  history,
  onGone,
  onOpened,
}: Readonly<{
  conversation: ListedConversation;
  isCurrent: boolean;
  history: History;
  /** Called when a write has left this row off the list it came back with. */
  onGone: () => void;
  onOpened?: () => void;
}>) {
  const [mode, setMode] = useState<Mode>("idle");
  const [draft, setDraft] = useState(conversation.title);
  const [busy, setBusy] = useState(false);
  const [refused, setRefused] = useState<string | null>(null);
  const actions = useRef<HTMLButtonElement>(null);
  const box = useRef<HTMLInputElement>(null);
  const keep = useRef<HTMLButtonElement>(null);

  const name = shownTitle(conversation.title);
  /**
   * What a control of this row is called, where its own words are not enough.
   *
   * "Rename", "Delete" and "Yes, delete" say what they do and not what they
   * do it to, and a panel holds one of each per conversation. The title goes
   * in the accessible name -- after the visible words, never instead of
   * them, so that speech input still reaches the control by what it says.
   * A conversation nobody has named has the beginning of its id after
   * "Untitled", because several of those are otherwise one name.
   */
  const label =
    conversation.title.trim() === ""
      ? `${name} ${conversation.id.slice(0, 8)}`
      : name;
  /**
   * Whether the focus is this row's to hand back.
   *
   * It cannot simply be focused where `close` is called: in the rename box
   * the actions button is not on the screen yet -- the box is drawn in its
   * place -- so the focus would be dropped on `<body>` and the keyboard left
   * nowhere. So the flag is set, and whichever happens next reads it: the
   * effect below, once the row has been drawn again, or the cleanup after
   * it, if the row has gone instead.
   */
  const returning = useRef(false);

  // The focus follows what opened, so a keyboard carries on where the eye
  // is: into the rename box, onto "No", which is the safe answer, and back
  // to the button that opened whichever of them was closed.
  useEffect(() => {
    if (mode === "rename") {
      box.current?.focus();
      box.current?.select();
    } else if (mode === "confirm") {
      keep.current?.focus();
    } else if (returning.current) {
      returning.current = false;
      actions.current?.focus();
    }
  }, [mode]);

  /**
   * The row went while it had the focus to hand back.
   *
   * Every write is followed by a fresh first page, and this conversation may
   * not be on it -- deleted, or pushed off the end by something else being
   * written to. There is then no button here to come back to, and the list
   * is where the focus goes instead of `<body>`.
   */
  useEffect(
    () => () => {
      if (returning.current) onGone();
    },
    [onGone],
  );

  const close = () => {
    returning.current = true;
    setMode("idle");
    setRefused(null);
  };

  const save = async () => {
    // A second Enter while the first is in the air would send the same
    // rename twice; the controls say `aria-disabled` rather than `disabled`,
    // so ignoring it is this function's job and not the browser's.
    if (busy) return;
    const title = draft.trim();
    if (title === "") {
      // The API would refuse this (a title is one line with something on
      // it); saying so here saves a round trip and keeps the box open.
      setRefused("A title needs something in it.");
      return;
    }
    if ([...title].length > MAX_TITLE_CHARS) {
      // Counted in code points, as the backend counts (see TOO_LONG).
      setRefused(TOO_LONG);
      return;
    }
    if (title === conversation.title) {
      close();
      return;
    }
    setBusy(true);
    setRefused(null);
    try {
      await history.rename(conversation.id, title);
      close();
    } catch (failure) {
      setRefused(detailOf(failure));
    } finally {
      setBusy(false);
    }
  };

  const archive = async () => {
    if (busy) return;
    setBusy(true);
    setRefused(null);
    // The focus is on the archive button, which goes with the row.
    returning.current = true;
    try {
      await history.archive(conversation.id);
      // The page was showing what the list no longer holds.
      if (isCurrent) navigate(NEW_CHAT);
    } catch (failure) {
      setRefused(detailOf(failure));
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (busy) return;
    setBusy(true);
    setRefused(null);
    // The focus is on "Yes, delete", which is inside this row: wherever it
    // goes next, it is not to be left on <body>.
    returning.current = true;
    try {
      await history.remove(conversation.id);
      // The page was showing what has just gone. There is no trash in this
      // version, so there is nothing to go back to but the empty chat.
      if (isCurrent) navigate(NEW_CHAT);
    } catch (failure) {
      setRefused(
        failure instanceof ApiError && failure.status === 409
          ? STILL_ANSWERING
          : detailOf(failure),
      );
    } finally {
      // Usually this row has gone with the conversation, and setting state
      // on a component that has been unmounted is a no-op.
      setBusy(false);
    }
  };

  // Escape closes the row's own menu, box or confirm rather than the drawer
  // around it: the innermost thing that is open is what it shuts.
  const onKeyDown = (event: KeyboardEvent) => {
    if (event.key !== "Escape" || mode === "idle") return;
    event.stopPropagation();
    close();
  };

  return (
    <li onKeyDown={onKeyDown} className="history-item" data-current={isCurrent}>
      {mode === "rename" ? (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void save();
          }}
          className="history-rename"
        >
          {/* `readOnly`, never `disabled`: this box holds the focus while
              the rename is in the air, and a disabled control loses it to
              `<body>` -- where Escape would reach the shell's own listener
              and close the drawer instead of this row. No `maxlength`
              either: see TOO_LONG. */}
          <input
            ref={box}
            aria-label="Title"
            value={draft}
            readOnly={busy}
            aria-disabled={busy}
            onChange={(event) => {
              setDraft(event.target.value);
            }}
            // Enter is handled here rather than left to the form's own
            // implicit submission, which not every environment has; the
            // default is prevented, so this is the only way it is saved.
            onKeyDown={(event) => {
              if (event.key !== "Enter") return;
              event.preventDefault();
              void save();
            }}
            className="history-title-box"
          />
          <button
            type="submit"
            title="Save the title"
            aria-label="Save the title"
            aria-disabled={busy}
            className="history-tool"
          >
            <CheckIcon className="history-icon" />
          </button>
          <button
            type="button"
            title="Cancel the rename"
            aria-label="Cancel the rename"
            onClick={close}
            className="history-tool"
          >
            <XIcon className="history-icon" />
          </button>
        </form>
      ) : (
        <div className="history-row">
          <a
            href={formatRoute({ kind: "conversation", id: conversation.id })}
            aria-current={isCurrent ? "page" : undefined}
            title={name}
            onClick={onOpened}
            className="history-link"
          >
            {name}
          </a>
          {/* A turn is running in it, here or in another client
              (`docs/specs/wire.md`, "The change feed"). */}
          {conversation.running && (
            <span
              role="img"
              aria-label="Answering"
              className="history-running"
            />
          )}
          {/* Drawn on hover, or while the row has the focus, where there is
              a hover; always, where there is none (history.css). */}
          <button
            type="button"
            title="Archive"
            aria-label={`Archive ${label}`}
            aria-disabled={busy}
            onClick={() => void archive()}
            className="history-tool history-archive"
          >
            <ArchiveIcon className="history-icon" />
          </button>
          <button
            ref={actions}
            type="button"
            title={`Actions for ${label}`}
            aria-label={`Actions for ${label}`}
            aria-expanded={mode !== "idle"}
            onClick={() => {
              setRefused(null);
              setMode(mode === "idle" ? "menu" : "idle");
            }}
            className="history-tool history-actions"
          >
            <EllipsisIcon className="history-icon" />
          </button>
        </div>
      )}

      {/* A disclosure of two buttons, not a `role="menu"`: a menu promises
          arrow-key navigation and a focus of its own, and two buttons a Tab
          reaches are what this is. */}
      {mode === "menu" && (
        <div
          role="group"
          aria-label={`Actions for ${label}`}
          className="history-menu"
        >
          <button
            type="button"
            onClick={() => {
              setDraft(conversation.title);
              setMode("rename");
            }}
            className="history-menu-item"
          >
            <PencilIcon className="history-icon-small" />
            Rename
          </button>
          <button
            type="button"
            onClick={() => {
              setMode("confirm");
            }}
            className="history-menu-item"
          >
            <TrashIcon className="history-icon-small" />
            Delete
          </button>
        </div>
      )}

      {mode === "confirm" && (
        <div className="history-confirm">
          <span>Delete this conversation?</span>
          {/* The title goes after the visible words, not instead of them:
              a control has to stay reachable by what it says. */}
          <button
            type="button"
            onClick={() => void remove()}
            aria-label={`Yes, delete: ${label}`}
            aria-disabled={busy}
            className="history-answer history-yes"
          >
            Yes, delete
          </button>
          <button
            ref={keep}
            type="button"
            onClick={close}
            aria-label={`No, keep it: ${label}`}
            className="history-answer"
          >
            No, keep it
          </button>
        </div>
      )}

      {refused !== null && (
        <p role="alert" className="history-refused">
          {refused}
        </p>
      )}
    </li>
  );
}

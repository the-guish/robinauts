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
 * Renaming and deleting are inline, in the row. No `window.confirm`, which
 * is the browser's dialogue rather than this interface's and which a person
 * cannot read the title of the thing they are deleting in; a "Delete?
 * Yes / No" in the row says what is about to go and can be left alone.
 *
 * Everything here is reachable with a keyboard alone: the actions button is
 * always drawn rather than appearing on hover, Escape closes what is open
 * and puts the focus back on the button that opened it, and the focus
 * follows into the rename box and onto "No".
 */
import { Check, MoreHorizontal, Pencil, Trash2, X } from "lucide-react";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";

import { ApiError, detailOf } from "../api/client";
import type { ConversationId } from "../chat";
import type { Conversation } from "../conversation/conversation";
import { shownTitle } from "../conversation/conversation";
import { formatRoute, navigate, NEW_CHAT } from "../router";
import type { History } from "./history";

/**
 * What a conversation whose answer did not stop in time is told when it is
 * deleted.
 *
 * A delete stops the answer first, whichever server runs it, and waits for
 * it to end. The API refuses with 409 when it has not ended in time -- its
 * server went away, and the answer ends when its lease runs out -- and its
 * detail names the run for an operator's log. What a person needs to know is
 * what to do next (`docs/specs/conversations.md`, "Deletion").
 */
export const STILL_ANSWERING =
  "This conversation's answer has not stopped yet. Try deleting it again in a minute.";

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
}

export function HistoryList({
  history,
  current,
  onOpened,
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
  return (
    <>
      <ul
        ref={list}
        tabIndex={-1}
        aria-label="Conversations"
        className="m-0 flex list-none flex-col p-0 outline-none"
      >
        {items.map((conversation) => (
          <HistoryItem
            key={conversation.id}
            conversation={conversation}
            isCurrent={conversation.id === current}
            history={history}
            onGone={toTheList}
            {...(onOpened === undefined ? {} : { onOpened })}
          />
        ))}
      </ul>
      {status === "loading" && (
        <p className="px-2 text-sm text-muted-foreground">
          Loading the conversations…
        </p>
      )}
      {status === "ready" && items.length === 0 && (
        <p className="px-2 text-sm text-muted-foreground">
          No conversations yet.
        </p>
      )}
      {/* Already a sentence: which ask failed is known where it failed, not
          here (`history.ts`). */}
      {error !== null && (
        <p role="alert" className="px-2 text-sm text-bad">
          {error}
        </p>
      )}
      {/* Whatever failed -- a first page, a refresh, a page after it -- the
          way out is to ask again. */}
      {error !== null && (
        <button
          type="button"
          onClick={history.refresh}
          className="mt-1 self-start rounded-ui border border-edge bg-paper px-2 py-1 text-sm hover:bg-hover"
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
          className="mt-1 self-start rounded-ui px-2 py-1 text-sm text-muted-foreground hover:bg-hover hover:text-ink disabled:opacity-60"
        >
          {loadingMore ? "Loading…" : "Load more"}
        </button>
      )}
    </>
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
  conversation: Conversation;
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
    <li
      onKeyDown={onKeyDown}
      className="flex flex-col rounded-ui hover:bg-hover"
      data-current={isCurrent}
    >
      {mode === "rename" ? (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void save();
          }}
          className="flex items-center gap-1 p-1"
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
            className="min-w-0 flex-1 rounded-ui border border-edge bg-paper px-2 py-1 text-sm text-ink"
          />
          <button
            type="submit"
            title="Save the title"
            aria-label="Save the title"
            aria-disabled={busy}
            className="rounded-ui p-1 text-muted-foreground hover:bg-tint hover:text-ink aria-disabled:opacity-60"
          >
            <Check size={16} aria-hidden="true" />
          </button>
          <button
            type="button"
            title="Cancel the rename"
            aria-label="Cancel the rename"
            onClick={close}
            className="rounded-ui p-1 text-muted-foreground hover:bg-tint hover:text-ink"
          >
            <X size={16} aria-hidden="true" />
          </button>
        </form>
      ) : (
        <div className="flex items-center gap-1">
          <a
            href={formatRoute({ kind: "conversation", id: conversation.id })}
            aria-current={isCurrent ? "page" : undefined}
            title={name}
            onClick={onOpened}
            className={`min-w-0 flex-1 truncate rounded-ui px-2 py-1.5 text-sm no-underline ${
              isCurrent ? "bg-tint font-semibold text-ink" : "text-ink"
            }`}
          >
            {name}
          </a>
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
            className="mr-1 shrink-0 rounded-ui p-1 text-muted-foreground hover:bg-tint hover:text-ink"
          >
            <MoreHorizontal size={16} aria-hidden="true" />
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
          className="flex gap-1 px-1 pb-1"
        >
          <button
            type="button"
            onClick={() => {
              setDraft(conversation.title);
              setMode("rename");
            }}
            className="flex items-center gap-1 rounded-ui px-2 py-1 text-sm text-muted-foreground hover:bg-tint hover:text-ink"
          >
            <Pencil size={14} aria-hidden="true" />
            Rename
          </button>
          <button
            type="button"
            onClick={() => {
              setMode("confirm");
            }}
            className="flex items-center gap-1 rounded-ui px-2 py-1 text-sm text-muted-foreground hover:bg-tint hover:text-ink"
          >
            <Trash2 size={14} aria-hidden="true" />
            Delete
          </button>
        </div>
      )}

      {mode === "confirm" && (
        <div className="flex flex-wrap items-center gap-2 px-2 pb-1 text-sm">
          <span>Delete this conversation?</span>
          {/* The title goes after the visible words, not instead of them:
              a control has to stay reachable by what it says. */}
          <button
            type="button"
            onClick={() => void remove()}
            aria-label={`Yes, delete: ${label}`}
            aria-disabled={busy}
            className="rounded-ui border border-edge bg-paper px-2 py-0.5 text-bad hover:bg-hover aria-disabled:opacity-60"
          >
            Yes, delete
          </button>
          <button
            ref={keep}
            type="button"
            onClick={close}
            aria-label={`No, keep it: ${label}`}
            className="rounded-ui border border-edge bg-paper px-2 py-0.5 hover:bg-hover"
          >
            No, keep it
          </button>
        </div>
      )}

      {refused !== null && (
        <p role="alert" className="px-2 pb-1 text-xs text-bad">
          {refused}
        </p>
      )}
    </li>
  );
}

// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The search page: a box at the top, the filters under it, a bar of bulk
 * actions, and the conversations that match, one to a row with a checkbox,
 * its project, name and agent, a page at a time.
 *
 * The listing is the backend's (`GET /robinauts/api/conversations`). With the
 * box empty it lists every conversation that is not archived; what is typed
 * narrows it to the conversations that hold every word in their name, their
 * project's name or their agent. The project filter narrows it to one
 * project, and "Include archived" adds the archived ones, marked as such. The
 * box is debounced, and an answer to a listing that has been changed since is
 * dropped. Pages are walked with the cursor each answer hands back, and kept
 * as a stack so that "Previous" asks for nothing.
 *
 * The selection is either the rows ticked, on any page, or -- with the box at
 * the head of the bar -- every conversation that matches, the pages not shown
 * included, but the rows unticked since. "Archive" and "Delete" send it to
 * `POST /robinauts/api/conversations/bulk` as the one or the other: ids, or
 * the filters with the exceptions. Delete asks first, inline, as the panel's
 * rows do.
 */
import { useEffect, useRef, useState } from "react";

import { detailOf, request } from "../api/client";
import type { ListedConversation } from "../conversation/conversation";
import { shownTitle } from "../conversation/conversation";
import type { Project } from "../projects/projects";
import { useRouter } from "../router";
import "./search.css";

/** How many rows a page of results has. */
export const RESULTS_PER_PAGE = 10;

/** How long the box rests before what is typed is asked. */
export const DEBOUNCE_MS = 250;

interface Answer {
  /** What it was asked with: the filters, the cursor and the reload. */
  key: string;
  items: ListedConversation[];
  next: string | null;
  error: string | null;
}

/**
 * What is selected: with `all`, every conversation that matches but those of
 * `ids`; without, those of `ids` alone.
 */
interface Selection {
  all: boolean;
  ids: ReadonlySet<string>;
}

const NOTHING: Selection = { all: false, ids: new Set() };

function plural(count: number): string {
  return `${count} conversation${count === 1 ? "" : "s"}`;
}

export function SearchPage({
  projects,
  agentName,
}: Readonly<{
  projects: readonly Project[] | null;
  agentName: (id: string) => string;
}>) {
  const { href } = useRouter();
  const [query, setQuery] = useState("");
  const [typed, setTyped] = useState("");
  const [project, setProject] = useState("");
  const [archived, setArchived] = useState(false);
  // The cursors of the pages walked so far: the page shown is the last.
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  // Moved by every bulk action, so that the listing is asked for again.
  const [reload, setReload] = useState(0);
  const [answer, setAnswer] = useState<Answer | null>(null);
  const [selection, setSelection] = useState<Selection>(NOTHING);
  const [confirming, setConfirming] = useState(false);
  const [acting, setActing] = useState(false);
  const [outcome, setOutcome] = useState<{
    text: string;
    failed: boolean;
  } | null>(null);
  const head = useRef<HTMLInputElement>(null);

  const cursor = cursors[cursors.length - 1] ?? null;
  const asked = typed.trim();
  const key = [asked, project, archived, cursor ?? "", reload].join("\n");

  /** The listing changed: back to its first page, with nothing selected. */
  const relist = () => {
    setCursors([null]);
    setSelection(NOTHING);
    setConfirming(false);
  };

  // Other words asked: the listing changed under the selection.
  const [listedFor, setListedFor] = useState(asked);
  if (listedFor !== asked) {
    setListedFor(asked);
    relist();
  }

  useEffect(() => {
    const timer = setTimeout(() => {
      setTyped(query);
    }, DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
    };
  }, [query]);

  useEffect(() => {
    const controller = new AbortController();
    request("get", "/robinauts/api/conversations", {
      query: {
        limit: RESULTS_PER_PAGE,
        cursor,
        q: asked === "" ? null : asked,
        project_id: project === "" ? null : project,
        archived,
      },
      signal: controller.signal,
    }).then(
      (page) => {
        setAnswer({
          key,
          items: page.items,
          next: page.next_cursor,
          error: null,
        });
      },
      (failure: unknown) => {
        if (controller.signal.aborted) return;
        setAnswer({ key, items: [], next: null, error: detailOf(failure) });
      },
    );
    return () => {
      controller.abort();
    };
  }, [asked, project, archived, cursor, key]);

  // The answer to what is on the screen; until it comes, the one before it
  // stays, so the rows do not flicker away on every keystroke.
  const loading = answer?.key !== key;
  const rows = answer?.error === null ? answer.items : [];
  const projectName = (id: string) =>
    projects?.find((one) => one.id === id)?.name ?? "";

  const ticked = (id: string) =>
    selection.all ? !selection.ids.has(id) : selection.ids.has(id);
  const anything = selection.all || selection.ids.size > 0;
  const everything = selection.all && selection.ids.size === 0;

  useEffect(() => {
    if (head.current) head.current.indeterminate = anything && !everything;
  }, [anything, everything]);

  const toggle = (id: string) => {
    const ids = new Set(selection.ids);
    if (ids.has(id)) ids.delete(id);
    else ids.add(id);
    setSelection({ all: selection.all, ids });
    setConfirming(false);
  };

  const act = async (action: "archive" | "delete") => {
    setActing(true);
    setOutcome(null);
    const ids = [...selection.ids];
    try {
      const done = await request("post", "/robinauts/api/conversations/bulk", {
        body: selection.all
          ? {
              action,
              matching: {
                q: asked === "" ? null : asked,
                project_id: project === "" ? null : project,
                archived,
              },
              except_ids: ids,
            }
          : { action, ids },
      });
      setOutcome({
        text: `${action === "archive" ? "Archived" : "Deleted"} ${plural(done.count)}.`,
        failed: false,
      });
      relist();
      setReload((count) => count + 1);
    } catch (failure: unknown) {
      setOutcome({
        text: `The ${action === "archive" ? "archive" : "delete"} failed: ${detailOf(failure)}`,
        failed: true,
      });
    } finally {
      setActing(false);
      setConfirming(false);
    }
  };

  const selected = selection.all
    ? selection.ids.size === 0
      ? "All matching conversations selected"
      : `All matching conversations selected but ${selection.ids.size}`
    : selection.ids.size > 0
      ? `${plural(selection.ids.size)} selected`
      : null;

  return (
    <section aria-label="Search" className="search">
      <h1 className="search-title">Search</h1>
      <input
        type="search"
        aria-label="Search conversations"
        placeholder="Search conversations"
        autoFocus
        value={query}
        onChange={(event) => {
          setQuery(event.target.value);
        }}
        className="search-box"
      />
      <div role="group" aria-label="Filters" className="search-filters">
        <label className="search-filter">
          <span>Project</span>
          <select
            value={project}
            onChange={(event) => {
              setProject(event.target.value);
              relist();
            }}
          >
            <option value="">All projects</option>
            {(projects ?? []).map((one) => (
              <option key={one.id} value={one.id}>
                {one.name}
              </option>
            ))}
          </select>
        </label>
        <label className="search-filter">
          <span>Include archived</span>
          <select
            value={archived ? "yes" : "no"}
            onChange={(event) => {
              setArchived(event.target.value === "yes");
              relist();
            }}
          >
            <option value="no">No</option>
            <option value="yes">Yes</option>
          </select>
        </label>
      </div>
      <div role="toolbar" aria-label="Bulk actions" className="search-bulk">
        <input
          ref={head}
          type="checkbox"
          aria-label="Select all matching conversations"
          checked={everything}
          onChange={() => {
            setSelection(anything ? NOTHING : { all: true, ids: new Set() });
            setConfirming(false);
          }}
          className="search-check"
        />
        <button
          type="button"
          disabled={!anything || acting}
          onClick={() => void act("archive")}
          className="search-action"
        >
          Archive
        </button>
        <button
          type="button"
          disabled={!anything || acting || confirming}
          onClick={() => {
            setConfirming(true);
          }}
          className="search-action search-delete"
        >
          Delete
        </button>
        {selected !== null && (
          <span className="search-selected">{selected}</span>
        )}
      </div>
      {confirming && (
        <div className="search-confirm">
          <span>
            {selection.all
              ? "Delete every selected conversation, on every page?"
              : `Delete ${plural(selection.ids.size)}?`}
          </span>
          <button
            type="button"
            disabled={acting}
            onClick={() => void act("delete")}
            className="search-action search-delete"
          >
            Yes, delete
          </button>
          <button
            type="button"
            disabled={acting}
            onClick={() => {
              setConfirming(false);
            }}
            className="search-action"
          >
            Cancel
          </button>
        </div>
      )}
      {outcome !== null && (
        <p
          role={outcome.failed ? "alert" : "status"}
          className={outcome.failed ? "search-failed" : "search-done"}
        >
          {outcome.text}
        </p>
      )}
      {rows.length > 0 && (
        <table aria-busy={loading} className="search-results">
          <thead>
            <tr>
              <th scope="col" className="search-tick">
                <span className="search-hidden">Selected</span>
              </th>
              <th scope="col">Project</th>
              <th scope="col">Name</th>
              <th scope="col">Agent</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((c) => (
              <tr
                key={c.id}
                aria-selected={ticked(c.id)}
                className="search-row"
              >
                <td className="search-tick">
                  <input
                    type="checkbox"
                    aria-label={`Select ${shownTitle(c.title)}`}
                    checked={ticked(c.id)}
                    onChange={() => {
                      toggle(c.id);
                    }}
                    className="search-check"
                  />
                </td>
                <td>{projectName(c.project_id)}</td>
                <td>
                  <a href={href({ kind: "conversation", id: c.id })}>
                    {shownTitle(c.title)}
                  </a>
                  {c.archived && (
                    <span className="search-archived">Archived</span>
                  )}
                </td>
                <td>{agentName(c.agent)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {loading && (
        <p role="status" className="search-empty">
          Searching…
        </p>
      )}
      {!loading && answer.error !== null && (
        <p role="alert" className="search-empty">
          The search failed: {answer.error}
        </p>
      )}
      {!loading && answer.error === null && rows.length === 0 && (
        <p role="status" className="search-empty">
          No conversations found.
        </p>
      )}
      {answer?.error === null &&
        (cursors.length > 1 || answer.next !== null) && (
          <nav aria-label="Pages" className="search-pages">
            <button
              type="button"
              disabled={loading || cursors.length === 1}
              onClick={() => {
                setCursors(cursors.slice(0, -1));
              }}
            >
              Previous
            </button>
            <span>Page {cursors.length}</span>
            <button
              type="button"
              disabled={loading || answer.next === null}
              onClick={() => {
                setCursors([...cursors, answer.next]);
              }}
            >
              Next
            </button>
          </nav>
        )}
    </section>
  );
}

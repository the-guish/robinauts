// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The search page: a box at the top, and under it the conversations that
 * match, one to a row with its project, name and agent, a page at a time.
 *
 * The search is the backend's (`GET /robinauts/api/conversations?q=`): a
 * conversation matches when every word typed is in its name, its project's
 * name or its agent. Nothing is asked, and nothing listed, until something is
 * typed; the box is debounced, and an answer to a query that has been typed
 * over is dropped. Pages are walked with the cursor each answer hands back,
 * and kept as a stack so that "Previous" asks for nothing.
 */
import { useEffect, useState } from "react";

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
  /** What it was asked with: the query and the cursor. */
  key: string;
  items: ListedConversation[];
  next: string | null;
  error: string | null;
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
  // The cursors of the pages walked so far: the page shown is the last.
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [answer, setAnswer] = useState<Answer | null>(null);

  const cursor = cursors[cursors.length - 1] ?? null;
  const asked = typed.trim();
  const key = `${asked}\n${cursor ?? ""}`;

  useEffect(() => {
    const timer = setTimeout(() => {
      setTyped(query);
      setCursors([null]);
    }, DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
    };
  }, [query]);

  useEffect(() => {
    if (asked === "") return;
    const controller = new AbortController();
    request("get", "/robinauts/api/conversations", {
      query: { q: asked, limit: RESULTS_PER_PAGE, cursor },
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
  }, [asked, cursor, key]);

  // The answer to what is on the screen; until it comes, the one before it
  // stays, so the rows do not flicker away on every keystroke.
  const fresh = asked !== "" && answer?.key === key;
  const current = asked !== "" ? answer : null;
  const loading = asked !== "" && !fresh;
  const projectName = (id: string) =>
    projects?.find((project) => project.id === id)?.name ?? "";

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
      {current !== null &&
        current.error === null &&
        current.items.length > 0 && (
          <table className="search-results">
            <thead>
              <tr>
                <th scope="col">Project</th>
                <th scope="col">Name</th>
                <th scope="col">Agent</th>
              </tr>
            </thead>
            <tbody>
              {current.items.map((c) => (
                <tr key={c.id}>
                  <td>{projectName(c.project_id)}</td>
                  <td>
                    <a href={href({ kind: "conversation", id: c.id })}>
                      {shownTitle(c.title)}
                    </a>
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
      {!loading && current?.error != null && (
        <p role="alert" className="search-empty">
          The search failed: {current.error}
        </p>
      )}
      {!loading && current?.error === null && current.items.length === 0 && (
        <p role="status" className="search-empty">
          No conversations found.
        </p>
      )}
      {current?.error === null &&
        (cursors.length > 1 || current.next !== null) && (
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
              disabled={loading || current.next === null}
              onClick={() => {
                setCursors([...cursors, current.next]);
              }}
            >
              Next
            </button>
          </nav>
        )}
    </section>
  );
}

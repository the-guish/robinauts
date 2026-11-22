// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The search page: a box at the top, and under it the conversations that
 * match, one to a row with its project, name and agent, a page at a time.
 *
 * Nothing is listed until something is typed. A conversation matches when
 * every word typed is found, case aside, in its name, its project's name or
 * its agent. The backend has no search route, so this matches the list the
 * panel holds; while a search is typed and the list has more pages, they are
 * asked for, so the results are those of every conversation.
 */
import { useEffect, useState } from "react";

import type { History } from "../history/history";
import { shownTitle } from "../conversation/conversation";
import type { ListedConversation } from "../conversation/conversation";
import type { Project } from "../projects/projects";
import { useRouter } from "../router";
import "./search.css";

/** How many rows a page of results has. */
export const RESULTS_PER_PAGE = 10;

/** The words of a query, lower-cased. */
function wordsOf(query: string): string[] {
  return query
    .toLowerCase()
    .split(/\s+/)
    .filter((word) => word !== "");
}

export function matches(
  words: readonly string[],
  fields: readonly string[],
): boolean {
  const haystack = fields.join("\n").toLowerCase();
  return words.every((word) => haystack.includes(word));
}

export function SearchPage({
  history,
  projects,
  agentName,
}: Readonly<{
  history: History;
  projects: readonly Project[] | null;
  agentName: (id: string) => string;
}>) {
  const { href } = useRouter();
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(0);
  const words = wordsOf(query);
  const searching = words.length > 0;

  const { more, loadingMore, refreshing, loadMore } = history;
  useEffect(() => {
    if (searching && more && !loadingMore && !refreshing) loadMore();
  }, [searching, more, loadingMore, refreshing, loadMore]);

  const projectName = (id: string) =>
    projects?.find((project) => project.id === id)?.name ?? "";
  const rows: ListedConversation[] = searching
    ? history.items.filter((c) =>
        matches(words, [
          shownTitle(c.title),
          projectName(c.project_id),
          agentName(c.agent),
        ]),
      )
    : [];
  const pages = Math.max(1, Math.ceil(rows.length / RESULTS_PER_PAGE));
  const shown = Math.min(page, pages - 1);
  const visible = rows.slice(
    shown * RESULTS_PER_PAGE,
    (shown + 1) * RESULTS_PER_PAGE,
  );
  const stillLooking = searching && (more || loadingMore || refreshing);

  return (
    <section aria-label="Search" className="search">
      <input
        type="search"
        aria-label="Search conversations"
        placeholder="Search conversations"
        autoFocus
        value={query}
        onChange={(event) => {
          setQuery(event.target.value);
          setPage(0);
        }}
        className="search-box"
      />
      {searching && (
        <>
          <table className="search-results">
            <thead>
              <tr>
                <th scope="col">Project</th>
                <th scope="col">Name</th>
                <th scope="col">Agent</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((c) => (
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
          {rows.length === 0 && (
            <p role="status" className="search-empty">
              {stillLooking ? "Searching…" : "No conversations found."}
            </p>
          )}
          {rows.length > RESULTS_PER_PAGE && (
            <nav aria-label="Pages" className="search-pages">
              <button
                type="button"
                disabled={shown === 0}
                onClick={() => {
                  setPage(shown - 1);
                }}
              >
                Previous
              </button>
              <span>
                Page {shown + 1} of {pages}
                {stillLooking ? "+" : ""}
              </span>
              <button
                type="button"
                disabled={shown >= pages - 1}
                onClick={() => {
                  setPage(shown + 1);
                }}
              >
                Next
              </button>
            </nav>
          )}
        </>
      )}
    </section>
  );
}

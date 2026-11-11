// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The conversations of whoever is signed in, as the panel lists them.
 *
 * `GET /api/conversations`, most recently updated first, a page at a time
 * with the opaque cursor the page before handed back
 * (`docs/specs/conversations.md`, "Listing"). No data-fetching library and no
 * state library: one list, asked for once and again after anything changes
 * it, which is what `src/session/session.ts` already does for the session.
 *
 * A hook rather than a module store, because there is one of these on the
 * page -- the shell owns it, passes it to the panel, and reads the open
 * conversation's title out of it -- and a module store would be state to
 * reset between two tests for no gain.
 *
 * **Paging walks a list that is changing**, not a transaction over a frozen
 * one: a conversation written to while somebody is paging moves in the order
 * and may be seen twice. The spec says an interface that cares folds by id,
 * and this one does.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { detailOf, request } from "../api/client";
import type { ConversationId } from "../chat";
import type { ListedConversation } from "../conversation/conversation";

/**
 * How many are asked for at a time.
 *
 * Enough that the panel of somebody who has been here a while is full
 * without a second request, and far under the bound the route documents.
 */
export const PAGE = 30;

/** The list, and everything that can be done to it. */
export interface History {
  status: "loading" | "ready" | "failed";
  items: ListedConversation[];
  /** There is another page to ask for. */
  more: boolean;
  /** That page is on its way. */
  loadingMore: boolean;
  /** The whole list is being asked for again; there is no page to add to. */
  refreshing: boolean;
  /**
   * What went wrong, as the sentence to show.
   *
   * Framed where it happens rather than where it is drawn: a first page that
   * did not arrive and a page after it that did not are two different pieces
   * of news, and only the ask knows which of them this was.
   */
  error: string | null;
  /** Ask again, from the first page. */
  refresh: () => void;
  loadMore: () => void;
  /**
   * Rename it, and ask again. A refusal is thrown to the caller, which is
   * the row it was asked from: that is where it has to be shown.
   */
  rename: (id: ConversationId, title: string) => Promise<void>;
  /**
   * Take it off the list, and ask again. It keeps everything, and its next
   * message brings it back.
   */
  archive: (id: ConversationId) => Promise<void>;
  /** Delete it for good -- there is no trash in this version -- and ask again. */
  remove: (id: ConversationId) => Promise<void>;
}

interface Listing {
  status: "loading" | "ready" | "failed";
  items: ListedConversation[];
  /** What to ask the next page with; `null` when this was the last. */
  cursor: string | null;
  loadingMore: boolean;
  refreshing: boolean;
  error: string | null;
}

const EMPTY: Listing = {
  status: "loading",
  items: [],
  cursor: null,
  loadingMore: false,
  refreshing: true,
  error: null,
};

/** What is said of a first page, or of a whole list asked for again. */
const NO_LIST = "The conversations could not be loaded";
/** What is said of a page after the first. */
const NO_MORE = "More conversations could not be loaded";

/** The same conversation twice is one conversation; the first wins. */
function fold(items: ListedConversation[]): ListedConversation[] {
  const seen = new Set<string>();
  const folded: ListedConversation[] = [];
  for (const item of items) {
    if (seen.has(item.id)) continue;
    seen.add(item.id);
    folded.push(item);
  }
  return folded;
}

export function useHistory(): History {
  const [listing, setListing] = useState<Listing>(EMPTY);
  /**
   * Which ask is the current one.
   *
   * A refresh started while a "load more" is in the air must not have that
   * page appended to it when it lands: it is a page of a listing that no
   * longer exists. Every ask takes a number, and an answer whose number is
   * not the latest is dropped. The same counter is what makes an answer
   * arriving after this went away a no-op.
   */
  const asked = useRef(0);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const load = useCallback(async (after: string | null): Promise<void> => {
    const mine = (asked.current += 1);
    setListing((was) => ({
      ...was,
      // A list already on the screen stays on it while it is asked for
      // again; only a first ask has nothing to show but "Loading…".
      status: was.items.length === 0 ? "loading" : was.status,
      // **A refresh takes the list over.** Bumping the number above has
      // already thrown away whatever a "load more" in the air will answer --
      // it is a page of a listing that no longer exists -- and this is what
      // stops another being asked for in the meantime, with a cursor from
      // that same gone listing.
      loadingMore: after !== null,
      refreshing: after === null,
      error: null,
    }));
    try {
      const page = await request("get", "/api/conversations", {
        query: { limit: PAGE, cursor: after },
      });
      if (!mounted.current || asked.current !== mine) return;
      setListing((was) => ({
        status: "ready",
        items:
          after === null
            ? fold(page.items)
            : fold([...was.items, ...page.items]),
        cursor: page.next_cursor,
        loadingMore: false,
        refreshing: false,
        error: null,
      }));
    } catch (failure) {
      if (!mounted.current || asked.current !== mine) return;
      setListing((was) => ({
        ...was,
        // A page that did not arrive leaves the pages that did: the list is
        // still usable, and the refusal is said above it.
        status: was.items.length === 0 ? "failed" : was.status,
        loadingMore: false,
        refreshing: false,
        error: `${after === null ? NO_LIST : NO_MORE}: ${detailOf(failure)}`,
      }));
    }
  }, []);

  useEffect(() => {
    void load(null);
  }, [load]);

  const refresh = useCallback(() => {
    void load(null);
  }, [load]);

  const { cursor, loadingMore, refreshing } = listing;
  const loadMore = useCallback(() => {
    if (cursor !== null && !loadingMore && !refreshing) void load(cursor);
  }, [cursor, loadingMore, refreshing, load]);

  /**
   * After a write, the list is asked for again from its first page.
   *
   * Any further page that was loaded is asked for again by whoever wants it.
   * A cursor is a position in a listing, and the listing a write left behind
   * is not the one that cursor was written in.
   */
  const rename = useCallback(
    async (id: ConversationId, title: string): Promise<void> => {
      await request("patch", "/api/conversations/{conversation_id}", {
        path: { conversation_id: id },
        body: { title },
      });
      await load(null);
    },
    [load],
  );

  const archive = useCallback(
    async (id: ConversationId): Promise<void> => {
      await request("post", "/api/conversations/{conversation_id}/archive", {
        path: { conversation_id: id },
      });
      await load(null);
    },
    [load],
  );

  const remove = useCallback(
    async (id: ConversationId): Promise<void> => {
      await request("delete", "/api/conversations/{conversation_id}", {
        path: { conversation_id: id },
      });
      await load(null);
    },
    [load],
  );

  return {
    status: listing.status,
    items: listing.items,
    more: listing.cursor !== null,
    loadingMore: listing.loadingMore,
    refreshing: listing.refreshing,
    error: listing.error,
    refresh,
    loadMore,
    rename,
    archive,
    remove,
  };
}

/** The conversation of that id, if this page of the listing holds it. */
export function conversationIn(
  items: readonly ListedConversation[],
  id: ConversationId | null,
): ListedConversation | null {
  if (id === null) return null;
  return items.find((item) => item.id === id) ?? null;
}

// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The change feed: one long poll per tab (`docs/specs/wire.md`, "The change
 * feed").
 *
 * The feed only says that something of the signed-in user changed. What
 * changed is read again by whoever shows it: the panel's list and the open
 * conversation (`src/shell/Shell.tsx`). The first answer is the version to
 * start from, and calls nothing.
 *
 * A hidden tab stops polling. A tab streaming a turn already holds two
 * connections, and a browser allows six per origin over HTTP/1.1. Visible
 * again, it polls at once, and the server answers at once when the version
 * moved in the meantime.
 *
 * A poll that fails waits before the next (`retryDelay`), longer after each
 * failure in a row, and the first poll that succeeds starts the count again.
 */
import { useEffect, useRef } from "react";

import { request } from "../api/client";

/** The seconds the server is asked to hold a poll; it allows up to 30. */
export const POLL_SECONDS = 25;

/** The first wait after a failed poll, in milliseconds. */
export const FIRST_RETRY_MS = 1_000;

/** The longest wait after failed polls, in milliseconds. */
export const LONGEST_RETRY_MS = 30_000;

/**
 * How long to wait before polling again, after that many failures in a row.
 *
 * Nothing after none; then twice as long after each, up to the longest.
 */
export function retryDelay(failures: number): number {
  if (failures <= 0) return 0;
  return Math.min(FIRST_RETRY_MS * 2 ** (failures - 1), LONGEST_RETRY_MS);
}

/**
 * Poll the feed while this is mounted, and call `onChange` each time the
 * version moves.
 */
export function useChanges(onChange: () => void): void {
  const latest = useRef(onChange);
  useEffect(() => {
    latest.current = onChange;
  }, [onChange]);

  useEffect(() => {
    let stopped = false;
    let version: number | null = null;
    let failures = 0;
    /** The poll in the air, aborted when the tab hides or this goes away. */
    let polling: AbortController | null = null;
    /** What ends a wait early: a retry's delay, or a hidden tab's pause. */
    let wake: (() => void) | null = null;

    const hidden = () => document.visibilityState === "hidden";

    function pause(ms: number | null): Promise<void> {
      return new Promise((resolve) => {
        const timer = ms === null ? undefined : setTimeout(resolve, ms);
        wake = () => {
          clearTimeout(timer);
          resolve();
        };
      });
    }

    async function poll(): Promise<void> {
      while (!stopped) {
        if (hidden()) {
          await pause(null);
          continue;
        }
        const control = new AbortController();
        polling = control;
        try {
          const answer = await request("get", "/api/changes", {
            query:
              version === null ? {} : { after: version, timeout: POLL_SECONDS },
            signal: control.signal,
          });
          // An answer that is not the feed's -- a proxy's page, say -- is a
          // failed poll, so that it is waited after and never spun on.
          if (!Number.isInteger(answer.version)) {
            throw new Error("the change feed answered without a version");
          }
          failures = 0;
          const moved = version !== null && answer.version !== version;
          version = answer.version;
          if (moved && !stopped) latest.current();
        } catch {
          // Aborted by a hidden tab or by going away: nothing failed.
          if (stopped || control.signal.aborted) continue;
          failures += 1;
          await pause(retryDelay(failures));
        } finally {
          if (polling === control) polling = null;
        }
      }
    }

    const onVisibility = () => {
      if (hidden()) {
        polling?.abort();
        return;
      }
      wake?.();
    };
    document.addEventListener("visibilitychange", onVisibility);
    void poll();
    return () => {
      stopped = true;
      document.removeEventListener("visibilitychange", onVisibility);
      polling?.abort();
      wake?.();
    };
  }, []);
}

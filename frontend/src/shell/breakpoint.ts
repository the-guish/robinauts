// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * Which of the two layouts is on, as a value React can read.
 *
 * The panel is a rail beside the page on a wide screen and an overlay drawer
 * on a narrow one, and CSS decides which by a breakpoint. Most of that needs
 * no JavaScript -- `./panel.css` and `./shell.css` do it -- but three things
 * cannot be said in CSS at all: `inert`, `role="dialog"` and a focus trap. A
 * closed drawer is translated off the screen, not removed, so without
 * `inert` it is six invisible tab stops, sign-out among them.
 *
 * So the same breakpoint is read once more, here, through `matchMedia`: one
 * number in two places, and the comment below is the reason it is allowed to
 * be.
 */
import { useSyncExternalStore } from "react";

/**
 * The breakpoint of `./panel.css` and `./shell.css`, in the unit they write
 * it in (it was Tailwind's `md`, and kept its value when Tailwind went).
 *
 * `rem`, not the 768px it usually comes to: a `rem` in a media query is the
 * browser's *default* font size, which a reader may have set to anything,
 * and at 24px `48rem` is 1152px while a hard-coded 768px is not.
 * The two would then disagree about which layout is on -- CSS drawing the
 * drawer while this said "wide", so the off-screen panel would keep every
 * tab stop and the open drawer would be no dialog at all. A browser
 * resolves `48rem` here exactly as it does in the stylesheet, so there is
 * one answer rather than two. If the breakpoint moves, both move.
 */
export const WIDE = "(min-width: 48rem)";

function media(): MediaQueryList | null {
  // Storage may be off and `matchMedia` may be absent (jsdom without a
  // polyfill, an ancient browser); neither is worth a blank page.
  try {
    return window.matchMedia(WIDE);
  } catch {
    return null;
  }
}

function subscribe(changed: () => void): () => void {
  const query = media();
  if (query === null) return () => undefined;
  query.addEventListener("change", changed);
  return () => {
    query.removeEventListener("change", changed);
  };
}

/**
 * Wide enough for the panel to stand beside the page.
 *
 * Without `matchMedia` the answer is "wide", which is the layout that needs
 * none of what this drives: no drawer, so nothing to make inert or to trap.
 */
export function useWide(): boolean {
  return useSyncExternalStore(
    subscribe,
    () => media()?.matches ?? true,
    () => true,
  );
}

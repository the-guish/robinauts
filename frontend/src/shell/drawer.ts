// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * What an overlay drawer needs that CSS cannot give it: Tab kept inside it
 * while it is open, and the page behind it held still.
 *
 * Two drawers use it: the left panel (`./Panel.tsx`) and the right side
 * panel (`../side/SidePanel.tsx`), each a drawer below the breakpoint of
 * `./breakpoint.ts`. Escape, the backdrop and where the focus goes on
 * opening and closing are the shell's (`./Shell.tsx`).
 */
import { useEffect, type RefObject } from "react";

/**
 * What can be tabbed to, in the order a browser would tab through it.
 *
 * `[tabindex="-1"]` is excluded from **every** clause, not only the last:
 * a `tabindex` of -1 takes an element out of the tab order whatever it is,
 * and a button carrying one would otherwise come back in through
 * `button:not([disabled])`.
 */
const FOCUSABLE = [
  "a[href]",
  "button:not([disabled])",
  "select:not([disabled])",
  "input:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]",
]
  .map((one) => `${one}:not([tabindex="-1"])`)
  .join(", ");

/**
 * The stops a Tab would actually land on, in order.
 *
 * **What is drawn, not what is written.** A drawer holds controls that
 * belong to the other layout -- the left panel's "Collapse the panel" is
 * hidden by `./panel.css` below the breakpoint, so at drawer width it is
 * `display: none` -- and a selector cannot see that.
 * An element that is not rendered cannot take the focus, so `.focus()` on it
 * does nothing; having already called `preventDefault()`, the trap would
 * then leave the focus where it was and Tab would be dead.
 *
 * Four ways an element is not a stop, and they need different questions:
 *
 * - **`aria-hidden`**: drawn, but not there for a reader. The two must not
 *   disagree about what the panel contains.
 * - **`inert`**: the browser takes a whole subtree out of the tab order.
 *   A drawer is never `inert` while it is open -- that is what
 *   `inert` is for when it is closed -- so this only ever matches an inert
 *   subtree *inside* it.
 * - **`visibility: hidden`**: it has a box, so `getClientRects()` says
 *   nothing, and only the computed style does.
 * - **no box at all**: `display: none`, a hidden ancestor,
 *   `content-visibility`. `getClientRects()` is the question a browser
 *   itself answers, and the drawer is asked first, because an environment
 *   that lays nothing out (jsdom, which has no layout engine) reports no
 *   boxes for *everything*: filtering on that would drop every stop and
 *   leave a trap that traps nothing. If the drawer itself has no box, boxes
 *   are not being computed and the rest of the DOM is all there is to go on.
 *
 * What is **not** covered: an element scrolled out of an overflow container
 * (still a stop, and rightly), `opacity: 0` (still focusable, and still a
 * stop for a browser), and `tabindex` above 0, which nothing here uses and
 * which would reorder the tab sequence rather than leave it in DOM order.
 */
function unreachable(element: HTMLElement): boolean {
  if (element.closest("[aria-hidden='true']") !== null) return true;
  if (element.closest("[inert]") !== null) return true;
  const view = element.ownerDocument.defaultView;
  return (
    view !== null && view.getComputedStyle(element).visibility === "hidden"
  );
}

function stops(node: HTMLElement): HTMLElement[] {
  const named = [...node.querySelectorAll<HTMLElement>(FOCUSABLE)].filter(
    (element) => !unreachable(element),
  );
  if (node.getClientRects().length === 0) return named;
  return named.filter((element) => element.getClientRects().length > 0);
}

/**
 * While the drawer is open, Tab stays inside it.
 *
 * It covers the page, so tabbing to what is behind it means a focus ring
 * nobody can see. The listener is on the document rather than on the panel,
 * because the focus may already be outside it -- on the backdrop's
 * neighbours, or nowhere at all -- and that is the case a trap has to catch.
 * Escape and the backdrop are how it is left (`./Shell.tsx`).
 */
export function useTrappedFocus(
  panel: RefObject<HTMLElement | null>,
  open: boolean,
): void {
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Tab") return;
      const node = panel.current;
      if (node === null) return;
      const inside = stops(node);
      const first = inside[0];
      const last = inside.at(-1);
      if (first === undefined || last === undefined) return;
      const active = document.activeElement;
      if (!node.contains(active)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
        return;
      }
      if (event.shiftKey && active === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
    };
  }, [panel, open]);
}

/** While the drawer is open, the page behind it does not scroll under it. */
export function useLockedScroll(open: boolean): void {
  useEffect(() => {
    if (!open) return;
    const { body } = document;
    const before = body.style.overflow;
    body.style.overflow = "hidden";
    return () => {
      body.style.overflow = before;
    };
  }, [open]);
}

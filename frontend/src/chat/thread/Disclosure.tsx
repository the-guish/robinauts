// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * Something that opens and closes: a button, and the panel it shows.
 *
 * Three things in the thread are this -- the thinking of an answer, a group
 * of tool calls, and each call in it -- and they differ only in what they
 * draw. The button is a `<button>` with `aria-expanded` and `aria-controls`;
 * the panel opens and closes on a transition of its grid row, from nothing to
 * its height, which is the one way CSS can animate to a height it does not
 * know.
 *
 * **A closed panel is not in the page.** Only while it closes is it kept, for
 * the length of the transition, and it is inert while it is. Kept closed and
 * merely hidden, its text would still be text: found by a search, counted by
 * a test that counts a sentence, read in a list of the answer's paragraphs.
 */
import { useEffect, useId, useState, type ReactNode } from "react";

/** How long a panel takes to open or to close, as `thread.css` says. */
export const DISCLOSURE_MS = 200;

/** Whether the person has asked for less motion. */
export function lessMotion(): boolean {
  return (
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

/**
 * Whether the panel is in the page: while it is open, and while it closes.
 *
 * Set during the render that opens it rather than after it, so the panel and
 * the `aria-expanded="true"` arrive together.
 */
function usePresent(open: boolean): boolean {
  const [present, setPresent] = useState(open);
  if (open && !present) setPresent(true);
  useEffect(() => {
    if (open || !present) return;
    const timer = setTimeout(
      () => {
        setPresent(false);
      },
      lessMotion() ? 0 : DISCLOSURE_MS,
    );
    return () => {
      clearTimeout(timer);
    };
  }, [open, present]);
  return open || present;
}

/** What a disclosure's button and its panel share. */
interface DisclosureState {
  open: boolean;
  /** The panel's id, which the button names. */
  id: string;
  /** Whether the panel is in the page. */
  present: boolean;
  /** For the button: what it says about the panel. */
  trigger: {
    "aria-expanded": boolean;
    "aria-controls": string | undefined;
  };
}

export function useDisclosure(open: boolean): DisclosureState {
  const id = useId();
  const present = usePresent(open);
  return {
    open,
    id,
    present,
    trigger: {
      "aria-expanded": open,
      // Only while there is a panel to name: one that is not in the page is
      // not something a reference can point at.
      "aria-controls": present ? id : undefined,
    },
  };
}

/** The panel: drawn while it is present, and inert while it closes. */
export function DisclosurePanel({
  state,
  className,
  children,
}: Readonly<{
  state: DisclosureState;
  className?: string;
  children: ReactNode;
}>) {
  if (!state.present) return null;
  return (
    <div
      id={state.id}
      className={
        className === undefined
          ? "chat-disclosure"
          : `chat-disclosure ${className}`
      }
      data-state={state.open ? "open" : "closed"}
      inert={!state.open}
    >
      <div className="chat-disclosure-inner">{children}</div>
    </div>
  );
}

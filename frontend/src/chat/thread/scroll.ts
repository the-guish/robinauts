// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The thread's viewport keeps to the bottom while an answer arrives, and lets
 * go the moment the person scrolls up.
 *
 * **Stuck** is the one piece of state: while it holds and an answer is
 * arriving (`follow`), every change in the height of what is in the viewport
 * scrolls it to the bottom again. Not otherwise: a disclosure somebody opens
 * in a thread that is standing still grows the page under their pointer, and
 * pulling the page up past it would move what they just clicked. Going to the
 * bottom is also done once, by hand (`toBottom`), when a conversation is read
 * and when a message is sent. Scrolling up unsticks it;
 * reaching the bottom again, by hand or with the button, sticks it. Only a
 * move *up* is the person's: the viewport's own scrolling only ever goes
 * down, and content that shrinks under a viewport at the bottom leaves it at
 * the bottom.
 */
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type RefObject,
} from "react";

import { lessMotion } from "./Disclosure";

/** How near the bottom counts as at it: a pixel's rounding and a little. */
const NEAR = 4;

interface StickToBottom {
  /** The element that scrolls. */
  viewport: RefObject<HTMLDivElement | null>;
  /** What is in it, whose height is watched. */
  content: RefObject<HTMLDivElement | null>;
  /** Whether the viewport is at the bottom, which is when no button is shown. */
  atBottom: boolean;
  /** Go to the bottom, and stay there. */
  toBottom: (smooth?: boolean) => void;
}

export function useStickToBottom(follow: boolean): StickToBottom {
  const viewport = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const stuck = useRef(true);
  const following = useRef(follow);
  useEffect(() => {
    following.current = follow;
  });
  const [atBottom, setAtBottom] = useState(true);

  useEffect(() => {
    const scroller = viewport.current;
    const inner = content.current;
    if (scroller === null || inner === null) return;
    const near = () =>
      scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight <=
      NEAR;
    let lastTop = scroller.scrollTop;
    const scrolled = () => {
      const bottom = near();
      if (bottom) stuck.current = true;
      else if (scroller.scrollTop < lastTop) stuck.current = false;
      lastTop = scroller.scrollTop;
      setAtBottom(bottom);
    };
    const grown = () => {
      if (stuck.current && following.current) {
        scroller.scrollTop = scroller.scrollHeight;
        lastTop = scroller.scrollTop;
      }
      setAtBottom(near());
    };
    scroller.addEventListener("scroll", scrolled, { passive: true });
    const observer = new ResizeObserver(grown);
    observer.observe(inner);
    observer.observe(scroller);
    return () => {
      scroller.removeEventListener("scroll", scrolled);
      observer.disconnect();
    };
  }, []);

  const toBottom = useCallback((smooth = false) => {
    const scroller = viewport.current;
    stuck.current = true;
    if (scroller === null) return;
    scroller.scrollTo({
      top: scroller.scrollHeight,
      behavior: smooth && !lessMotion() ? "smooth" : "auto",
    });
  }, []);

  return { viewport, content, atBottom, toBottom };
}

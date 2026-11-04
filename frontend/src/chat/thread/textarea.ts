// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * What the two boxes of the chat share: they grow with what is written, and
 * Enter sends.
 *
 * A box grows to fit its text and no further than the `max-height` its class
 * gives it, past which it scrolls (`thread.css`). Its height is the one
 * `style` written from here, because it is the one genuinely dynamic value:
 * what the text measures to, which no stylesheet can know.
 */
import {
  useEffect,
  useLayoutEffect,
  useRef,
  type KeyboardEvent,
  type RefObject,
} from "react";

/** The box made as tall as its text, or left to the stylesheet's minimum. */
function fit(element: HTMLTextAreaElement): void {
  // Back to its smallest first, so that a box that is emptied shrinks:
  // `scrollHeight` is never less than the height it already has.
  element.style.height = "auto";
  const needed = element.scrollHeight;
  // Nothing to measure where there is no layout (a test's DOM): the
  // stylesheet's minimum is the height then.
  if (needed > 0) element.style.height = `${needed}px`;
  else element.style.removeProperty("height");
}

/**
 * A box that grows to its text whenever the text changes, or its width does:
 * the ref to give the `<textarea>` that shows `text`.
 *
 * The same text takes more lines in a narrower box, so a window made
 * narrower, or a panel opened beside the chat, has to measure it again. Only
 * a change of width does: the height is what this writes, and measuring on
 * that would answer every change with another.
 */
export function useAutosize(
  text: string,
): RefObject<HTMLTextAreaElement | null> {
  const box = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    const element = box.current;
    if (element !== null) fit(element);
  }, [text]);
  useEffect(() => {
    const element = box.current;
    // A browser without it keeps the height the text last gave the box.
    if (element === null || typeof ResizeObserver === "undefined") {
      return undefined;
    }
    let width = element.clientWidth;
    const watcher = new ResizeObserver(() => {
      if (element.clientWidth === width) return;
      width = element.clientWidth;
      fit(element);
    });
    watcher.observe(element);
    return () => {
      watcher.disconnect();
    };
  }, []);
  return box;
}

/**
 * Whether that key press is "send": Enter on its own.
 *
 * Shift+Enter is a new line, and an Enter that ends the composition of a
 * character -- in an input method for Japanese, say -- is the input method's
 * and not a send.
 */
export function isSend(event: KeyboardEvent<HTMLTextAreaElement>): boolean {
  return (
    event.key === "Enter" &&
    !event.shiftKey &&
    !event.nativeEvent.isComposing &&
    // Some browsers report the composing Enter only as this key code.
    event.keyCode !== 229
  );
}

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
  useLayoutEffect,
  useRef,
  type KeyboardEvent,
  type RefObject,
} from "react";

/**
 * A box that grows to its text whenever the text changes: the ref to give
 * the `<textarea>` that shows `text`.
 */
export function useAutosize(
  text: string,
): RefObject<HTMLTextAreaElement | null> {
  const box = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    const element = box.current;
    if (element === null) return;
    // Back to its smallest first, so that a box that is emptied shrinks:
    // `scrollHeight` is never less than the height it already has.
    element.style.height = "auto";
    const needed = element.scrollHeight;
    // Nothing to measure where there is no layout (a test's DOM): the
    // stylesheet's minimum is the height then.
    if (needed > 0) element.style.height = `${needed}px`;
    else element.style.removeProperty("height");
  }, [text]);
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

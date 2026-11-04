// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The thinking of an answer, behind a disclosure labelled "Reasoning".
 *
 * **Thinking is kept apart from the answer** (`docs/specs/conversations.md`,
 * "Reasoning"): it is shown while it arrives and never stored, so it is
 * something to open rather than something to read. The disclosure is open
 * while the thinking streams -- a live preview, held to its newest line --
 * and closes when the answer moves on, unless the person has opened or closed
 * it themselves, after which it does what they said.
 *
 * Consecutive stretches of thinking are one disclosure, each stretch drawn as
 * Markdown in turn.
 */
import { useEffect, useRef, useState, type ReactNode } from "react";

import { BrainIcon, ChevronDownIcon } from "../../icons";
import { Markdown } from "../markdown/Markdown";
import type { ChatTextPart } from "../state";
import { DisclosurePanel, useDisclosure } from "./Disclosure";

export function Reasoning({
  parts,
  streaming,
  streamingPart,
}: Readonly<{
  /** The stretches of thinking, in the order they arrived. */
  parts: readonly ChatTextPart[];
  /** Whether the thinking is still arriving. */
  streaming: boolean;
  /** Which of `parts` is arriving, if one is. */
  streamingPart: number | null;
}>) {
  const [chosen, setChosen] = useState<boolean | null>(null);
  const open = chosen ?? streaming;
  const disclosure = useDisclosure(open);
  // Following the newest line is only for an open disclosure of thinking
  // that is still arriving.
  const preview = streaming && open;

  return (
    <div className="chat-reasoning" data-slot="reasoning-root">
      <button
        type="button"
        className="chat-trigger chat-reasoning-trigger"
        data-slot="reasoning-trigger"
        {...disclosure.trigger}
        onClick={() => {
          setChosen(!open);
        }}
      >
        <BrainIcon className="chat-trigger-icon" />
        <span className="chat-trigger-label">Reasoning</span>
        <ChevronDownIcon className="chat-chevron" />
      </button>
      <DisclosurePanel state={disclosure} className="chat-reasoning-panel">
        <div className="chat-reasoning-content" aria-busy={streaming}>
          <div className="chat-fade chat-fade-top" />
          <Following follow={preview}>
            {parts.map((part, at) => (
              <Markdown
                key={part.id ?? at}
                text={part.text}
                streaming={at === streamingPart}
              />
            ))}
          </Following>
          {preview && <div className="chat-fade chat-fade-bottom" />}
        </div>
      </DisclosurePanel>
    </div>
  );
}

/**
 * A box of its own height that keeps to its newest line while `follow`.
 *
 * Held to the bottom as the thinking grows, and let go of the moment the
 * reader scrolls up in it; back at the bottom, it is held again. Only an
 * upward move at an unchanged height is the reader's: the box's own pin can
 * land after new text has grown it, and that is not somebody scrolling.
 */
function Following({
  follow,
  children,
}: Readonly<{ follow: boolean; children: ReactNode }>) {
  const box = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const scroller = box.current;
    const inner = content.current;
    if (!follow || scroller === null || inner === null) return;
    let pinned = true;
    let lastTop = scroller.scrollTop;
    let lastHeight = scroller.scrollHeight;
    const atBottom = () =>
      scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight <= 1;
    const pin = () => {
      if (pinned) scroller.scrollTop = scroller.scrollHeight;
    };
    const scrolled = () => {
      if (atBottom()) pinned = true;
      else if (
        scroller.scrollTop < lastTop &&
        scroller.scrollHeight === lastHeight
      ) {
        pinned = false;
      }
      lastTop = scroller.scrollTop;
      lastHeight = scroller.scrollHeight;
    };
    pin();
    scroller.addEventListener("scroll", scrolled, { passive: true });
    const observer = new ResizeObserver(pin);
    observer.observe(inner);
    return () => {
      scroller.removeEventListener("scroll", scrolled);
      observer.disconnect();
    };
  }, [follow]);
  return (
    <div ref={box} className="chat-reasoning-text" data-slot="reasoning-text">
      <div ref={content} className="chat-reasoning-stretches">
        {children}
      </div>
    </div>
  );
}

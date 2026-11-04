// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * An answer: its parts in the order they arrived, what went wrong with it if
 * something did, and what can be done with it.
 *
 * **The parts are grouped as they are read.** Thinking and tool calls that
 * come together are one stretch of working-out, and within it consecutive
 * thinking is one "Reasoning" disclosure and consecutive calls one group
 * named by their count; what the answer says is drawn as Markdown between
 * them. A delta only ever adds to the last part, so a group's key -- the
 * index of its first part -- never moves while an answer streams.
 *
 * **One thread** (ADR 0003): nothing here switches between versions of an
 * answer, and there is no feedback and no attachment. An answer can be
 * exported, as it always could, from its "More" menu.
 */
import {
  memo,
  useEffect,
  useId,
  useRef,
  useState,
  type FocusEvent,
  type KeyboardEvent,
} from "react";

import {
  CheckIcon,
  CopyIcon,
  EllipsisIcon,
  PlayIcon,
  RefreshCwIcon,
} from "../../icons";
import { IconButton } from "../IconButton";
import { Markdown } from "../markdown/Markdown";
import { textOf } from "../runtime";
import type {
  ChatMessage,
  ChatPart,
  ChatTextPart,
  ChatToolCall,
} from "../state";
import { useCopied } from "../useCopied";
import { Reasoning } from "./Reasoning";
import { ToolGroup } from "./ToolCall";

/** What an answer can be asked to do, made once for the whole thread. */
export interface AnswerActions {
  regenerate: (answerId: string) => Promise<void>;
  resume: (answerId: string) => Promise<void>;
}

/** A stretch of the answer, as it is drawn. */
type Piece =
  | { kind: "text"; at: number; part: ChatTextPart }
  | { kind: "thought"; at: number; groups: Group[] };

/** Within a stretch of working-out: thinking, or calls. */
type Group =
  | { kind: "reasoning"; at: number; parts: ChatTextPart[]; indices: number[] }
  | { kind: "tools"; at: number; calls: ChatToolCall[] };

/** The answer's parts, grouped as they are drawn. */
function grouped(parts: readonly ChatPart[]): Piece[] {
  const pieces: Piece[] = [];
  parts.forEach((part, at) => {
    if (part.kind === "text") {
      pieces.push({ kind: "text", at, part });
      return;
    }
    let thought = pieces.at(-1);
    if (thought?.kind !== "thought") {
      thought = { kind: "thought", at, groups: [] };
      pieces.push(thought);
    }
    const group = thought.groups.at(-1);
    if (part.kind === "tool-call") {
      if (group?.kind === "tools") group.calls.push(part);
      else thought.groups.push({ kind: "tools", at, calls: [part] });
      return;
    }
    if (group?.kind === "reasoning") {
      group.parts.push(part);
      group.indices.push(at);
    } else {
      thought.groups.push({
        kind: "reasoning",
        at,
        parts: [part],
        indices: [at],
      });
    }
  });
  return pieces;
}

/**
 * Whether the answer is still working with nothing to show for it yet: no
 * part at all, or a call as the last thing it did. Text or thinking that is
 * arriving shows itself.
 */
export function working(message: ChatMessage): boolean {
  if (message.state !== "running") return false;
  const last = message.parts.at(-1);
  return last === undefined || last.kind === "tool-call";
}

/** Whether there is anything to copy: words, and none still arriving. */
function copyable(message: ChatMessage): boolean {
  return (
    message.state !== "running" &&
    message.parts.some((part) => part.kind === "text" && part.text !== "")
  );
}

export const AssistantMessage = memo(function AssistantMessage({
  message,
  last,
  running,
  placeholder = false,
  actions,
}: Readonly<{
  message: ChatMessage;
  /** The last message of the thread, whose actions are always shown. */
  last: boolean;
  /** A run is in flight, and no action is offered while it is. */
  running: boolean;
  /** Not a message yet: what stands where the answer will arrive. */
  placeholder?: boolean;
  actions: AnswerActions;
}>) {
  const streaming = message.state === "running";
  const lastPart = message.parts.length - 1;

  return (
    <div
      className="chat-message chat-answer"
      data-role="assistant"
      data-message-id={placeholder ? undefined : message.id}
      data-last={last}
    >
      <div className="chat-answer-content">
        {grouped(message.parts).map((piece) =>
          piece.kind === "text" ? (
            <Markdown
              key={`text-${piece.at}`}
              text={piece.part.text}
              streaming={streaming && piece.at === lastPart}
            />
          ) : (
            <div key={`thought-${piece.at}`} className="chat-thought">
              {piece.groups.map((group) =>
                group.kind === "reasoning" ? (
                  <Reasoning
                    key={`reasoning-${group.at}`}
                    parts={group.parts}
                    streaming={streaming && group.indices.includes(lastPart)}
                    streamingPart={
                      streaming && group.indices.includes(lastPart)
                        ? group.indices.indexOf(lastPart)
                        : null
                    }
                  />
                ) : (
                  <ToolGroup
                    key={`tools-${group.at}`}
                    calls={group.calls}
                    message={message}
                  />
                ),
              )}
            </div>
          ),
        )}
        {working(message) && (
          <span
            className="chat-working"
            role="img"
            aria-label="Assistant is working"
          >
            ●
          </span>
        )}
        {message.state === "failed" && (
          // Where the answer would be, in the palette's quiet colours
          // (`docs/specs/ui.md`), and an alert, as it always was: a box that
          // arrives already holding its words is read out only as an alert,
          // and a polite live region would say nothing at all.
          <div className="chat-error" role="alert">
            <span className="chat-error-text">{message.detail ?? ""}</span>
          </div>
        )}
      </div>
      <div className="chat-answer-footer">
        {!running && !placeholder && (
          <AnswerActionBar message={message} last={last} actions={actions} />
        )}
      </div>
    </div>
  );
});

/**
 * Copy, Refresh, Resume on the last answer when it failed, and More.
 *
 * Refresh is a regeneration, or a retry on an answer that failed; Resume goes
 * on with a failed answer's turn, and the backend resumes a conversation's
 * latest turn alone, so it is offered on the last answer only
 * (`docs/specs/ui.md`).
 */
function AnswerActionBar({
  message,
  last,
  actions,
}: Readonly<{
  message: ChatMessage;
  last: boolean;
  actions: AnswerActions;
}>) {
  const { copied, copy } = useCopied();

  return (
    <div className="chat-actions chat-answer-actions">
      <IconButton
        label="Copy"
        onClick={() => {
          const text = textOf(message);
          if (text !== "") copy(text);
        }}
        disabled={!copyable(message)}
        className={copied ? "chat-copied" : undefined}
      >
        {copied ? <CheckIcon /> : <CopyIcon />}
      </IconButton>
      <IconButton
        label="Refresh"
        onClick={() => {
          void actions.regenerate(message.id);
        }}
      >
        <RefreshCwIcon />
      </IconButton>
      {last && message.state === "failed" && (
        <IconButton
          label="Resume"
          onClick={() => {
            void actions.resume(message.id);
          }}
        >
          <PlayIcon />
        </IconButton>
      )}
      <MoreMenu message={message} />
    </div>
  );
}

/**
 * The answer saved as a Markdown file: its words, as Copy copies them, in a
 * download named for the moment it was made.
 *
 * The browser is handed the file through an object URL and a link clicked on
 * its behalf; the URL is let go of once the click has handed it over, so that
 * the file is not kept in memory for as long as the page is open.
 */
function exportMarkdown(message: ChatMessage): void {
  const file = new Blob([textOf(message)], { type: "text/markdown" });
  const url = URL.createObjectURL(file);
  const link = document.createElement("a");
  link.href = url;
  link.download = `message-${Date.now()}.md`;
  link.rel = "noopener";
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => {
    URL.revokeObjectURL(url);
  }, 0);
}

/**
 * "More": a menu of what is done with an answer less often, which is one
 * thing, Export as Markdown.
 *
 * A menu button as the ARIA pattern has it: opening it puts the keyboard on
 * its item, Escape shuts it and gives the keyboard back to the button, and a
 * click anywhere else or a Tab out of it shuts it where the reader went. A
 * Shift+Tab back onto the button leaves it open, as the button is the
 * menu's own; pressing it, or Escape there, shuts it.
 */
function MoreMenu({ message }: Readonly<{ message: ChatMessage }>) {
  const [open, setOpen] = useState(false);
  const menuId = useId();
  const button = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const item = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return undefined;
    item.current?.focus();
    // A press anywhere but the menu and its button shuts it. The button's
    // own press is its click's to answer, which shuts it as it opened it.
    function pressed(event: PointerEvent) {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (menu.current?.contains(target) === true) return;
      if (button.current?.contains(target) === true) return;
      setOpen(false);
    }
    document.addEventListener("pointerdown", pressed);
    return () => {
      document.removeEventListener("pointerdown", pressed);
    };
  }, [open]);

  function shut() {
    setOpen(false);
    button.current?.focus();
  }

  function keyed(event: KeyboardEvent) {
    if (event.key !== "Escape" || !open) return;
    // Escape is the menu's here, and not the chat's, which would stop a run.
    event.preventDefault();
    event.stopPropagation();
    shut();
  }

  function left(event: FocusEvent) {
    const to = event.relatedTarget;
    if (!(to instanceof Node)) return;
    if (menu.current?.contains(to) === true) return;
    if (button.current?.contains(to) === true) return;
    setOpen(false);
  }

  return (
    <span className="chat-more">
      <IconButton
        ref={button}
        label="More"
        className={open ? "chat-more-open" : undefined}
        onClick={() => {
          setOpen((was) => !was);
        }}
        onKeyDown={keyed}
        popup={{ id: menuId, open }}
      >
        <EllipsisIcon />
      </IconButton>
      {open && (
        <div
          ref={menu}
          id={menuId}
          className="chat-menu"
          role="menu"
          aria-label="More"
          onKeyDown={keyed}
          onBlur={left}
        >
          <button
            ref={item}
            type="button"
            className="chat-menu-item"
            role="menuitem"
            onClick={() => {
              exportMarkdown(message);
              shut();
            }}
          >
            Export as Markdown
          </button>
        </div>
      )}
    </span>
  );
}

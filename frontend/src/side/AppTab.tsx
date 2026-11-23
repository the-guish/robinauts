// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * An MCP app's tab: one view of one tool server, with a sub-tab for each
 * call of the agent that the view shows (`docs/architecture/mcp-apps.md`).
 *
 * **The sub-tabs are the conversation's.** They come from the tool calls of
 * the thread on the screen (`./apps.ts`, `subTabsOf`), so a tab opened by
 * hand is empty until the agent calls a tool of the view, and a conversation
 * opened again shows the sub-tabs it had.
 *
 * **A sub-tab closes on its own.** This browser remembers the call it
 * showed as closed, per conversation (`./tabs.ts`), so it stays closed when
 * the conversation is opened again, and a later call of the agent's for the
 * same thing opens it again. The others keep their names.
 *
 * **The one on top** is the one the person picked, until the agent asks for
 * another (`asked`); with neither, the one of the newest call.
 *
 * **A sub-tab's frame is made when it is first on top**, and stays in the
 * page after, hidden while another is: what a view holds is not thrown away
 * when another is looked at, and a conversation with many calls loads one
 * view, not all of them.
 *
 * **Keyboard.** The strip is a tab list: Left and Right move to the
 * neighbouring sub-tab and bring it to the top, Home and End go to the ends,
 * Delete closes the one on top. Its close button is the next Tab stop.
 */
import { type KeyboardEvent, useEffect, useId, useRef, useState } from "react";

import { XIcon } from "../icons";
import { AppFrame } from "./AppFrame";
import { type App, callRef, type SubTab } from "./apps";
import { rememberClosed, rememberedClosed } from "./tabs";

/** The sub-tab the agent last asked for, and a count that grows each time. */
export interface Asked {
  readonly sub: string;
  readonly at: number;
}

export function AppTab({
  agent,
  app,
  conversation,
  subTabs,
  asked,
}: Readonly<{
  agent: string;
  app: App;
  /** The conversation on the screen; `null` on the empty chat. */
  conversation: string | null;
  subTabs: readonly SubTab[];
  asked: Asked | null;
}>) {
  // The calls whose sub-tabs the person closed, as this browser remembers.
  const [closed, setClosed] = useState<ReadonlySet<string>>(
    () => new Set(conversation === null ? [] : rememberedClosed(conversation)),
  );
  const open = subTabs.filter((sub) => !closed.has(callRef(sub.call)));

  // The agent's last asking, kept once its turn is over and `asked` is gone.
  const [asking, setAsking] = useState(asked);
  if (asked !== null && asked.at !== asking?.at) setAsking(asked);
  const askedAt = asking?.at ?? 0;
  // What the person picked, and the asking it came after: a later asking of
  // the agent's takes the top back.
  const [picked, setPicked] = useState<{ key: string; after: number } | null>(
    null,
  );
  const wanted =
    picked !== null && picked.after === askedAt ? picked.key : asking?.sub;
  const newest = open.reduce<SubTab | null>(
    (latest, sub) => (latest === null || sub.last > latest.last ? sub : latest),
    null,
  );
  const selected = open.find((sub) => sub.key === wanted) ?? newest;

  // The sub-tabs that have been on top, which are the ones with a frame.
  const [framed, setFramed] = useState<ReadonlySet<string>>(new Set());
  if (selected !== null && !framed.has(selected.key)) {
    setFramed(new Set([...framed, selected.key]));
  }
  // "Try again" is a new frame, and a new handshake with it.
  const [attempts, setAttempts] = useState<ReadonlyMap<string, number>>(
    new Map(),
  );

  const base = useId();
  const tabs = useRef(new Map<string, HTMLButtonElement>());
  const tabId = (key: string) => `${base}-sub-${encodeURIComponent(key)}`;
  const panelId = (key: string) => `${base}-view-${encodeURIComponent(key)}`;

  // Where the keyboard goes once a sub-tab has closed: the one now on top.
  const focusNext = useRef<string | null>(null);
  useEffect(() => {
    const next = focusNext.current;
    if (next === null) return;
    focusNext.current = null;
    tabs.current.get(next)?.focus();
  });

  const pick = (key: string) => {
    setPicked({ key, after: askedAt });
    tabs.current.get(key)?.focus();
  };

  /** Close a sub-tab. The one on top goes to its right-hand neighbour, or
   * the left-hand one when it was the last, as the tabs above do. */
  const close = (sub: SubTab) => {
    if (conversation === null) return;
    const at = open.indexOf(sub);
    const next = open[at + 1] ?? open[at - 1];
    const ref = callRef(sub.call);
    rememberClosed(conversation, ref);
    setClosed(new Set([...closed, ref]));
    setFramed(new Set([...framed].filter((key) => key !== sub.key)));
    if (sub.key === selected?.key && next !== undefined) {
      setPicked({ key: next.key, after: askedAt });
      focusNext.current = next.key;
    }
  };

  const onKey = (event: KeyboardEvent<HTMLButtonElement>, at: number) => {
    const current = open[at];
    if (current === undefined) return;
    if (event.key === "Delete") {
      event.preventDefault();
      close(current);
      return;
    }
    const to = new Map([
      ["ArrowRight", (at + 1) % open.length],
      ["ArrowLeft", (at - 1 + open.length) % open.length],
      ["Home", 0],
      ["End", open.length - 1],
    ]).get(event.key);
    const target = to === undefined ? undefined : open[to];
    if (target === undefined) return;
    event.preventDefault();
    pick(target.key);
  };

  if (conversation === null || selected === null) {
    return (
      <p className="side-empty">
        Nothing to show. {app.title} shows here when the agent uses it.
      </p>
    );
  }

  return (
    <div className="side-subs">
      <div
        role="tablist"
        aria-label={`${app.title}, shown`}
        className="side-sub-strip"
      >
        {open.map((sub, at) => {
          const isSelected = sub.key === selected.key;
          return (
            // The close button sits beside its tab, not inside it: a button
            // in a button is no button at all to a reader.
            <div
              key={sub.key}
              role="presentation"
              data-selected={isSelected}
              className="side-sub"
            >
              <button
                type="button"
                role="tab"
                id={tabId(sub.key)}
                ref={(node) => {
                  if (node === null) tabs.current.delete(sub.key);
                  else tabs.current.set(sub.key, node);
                }}
                aria-selected={isSelected}
                aria-controls={panelId(sub.key)}
                tabIndex={isSelected ? 0 : -1}
                title={sub.title}
                onClick={() => {
                  pick(sub.key);
                }}
                onKeyDown={(event) => {
                  onKey(event, at);
                }}
                className="side-sub-name"
              >
                {sub.title}
              </button>
              <button
                type="button"
                aria-label={`Close ${sub.title}`}
                title={`Close ${sub.title}`}
                tabIndex={isSelected ? 0 : -1}
                onClick={() => {
                  close(sub);
                }}
                className="side-sub-close"
              >
                <XIcon className="side-icon-small" />
              </button>
            </div>
          );
        })}
      </div>
      {open
        .filter((sub) => framed.has(sub.key) || sub.key === selected.key)
        .map((sub) => (
          <div
            key={sub.key}
            role="tabpanel"
            id={panelId(sub.key)}
            aria-labelledby={tabId(sub.key)}
            hidden={sub.key !== selected.key}
            className="side-sub-view"
          >
            <AppFrame
              key={attempts.get(sub.key) ?? 0}
              agent={agent}
              server={app.server}
              conversation={conversation}
              sub={sub}
              onRetry={() => {
                setAttempts((was) =>
                  new Map(was).set(sub.key, (was.get(sub.key) ?? 0) + 1),
                );
              }}
            />
          </div>
        ))}
    </div>
  );
}

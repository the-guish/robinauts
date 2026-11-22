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
 * **The one on top** is the one the person picked, until the agent asks for
 * another (`asked`); with neither, the one of the newest call.
 *
 * **A sub-tab's frame is made when it is first on top**, and stays in the
 * page after, hidden while another is: what a view holds is not thrown away
 * when another is looked at, and a conversation with many calls loads one
 * view, not all of them.
 *
 * **Keyboard.** The strip is a tab list: Left and Right move to the
 * neighbouring sub-tab and bring it to the top, Home and End go to the ends.
 */
import { type KeyboardEvent, useId, useRef, useState } from "react";

import { AppFrame } from "./AppFrame";
import type { App, SubTab } from "./apps";

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
  const newest = subTabs.reduce<SubTab | null>(
    (latest, sub) => (latest === null || sub.last > latest.last ? sub : latest),
    null,
  );
  const selected = subTabs.find((sub) => sub.key === wanted) ?? newest;

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

  const pick = (key: string) => {
    setPicked({ key, after: askedAt });
    tabs.current.get(key)?.focus();
  };

  const onKey = (event: KeyboardEvent<HTMLButtonElement>, at: number) => {
    const to = new Map([
      ["ArrowRight", (at + 1) % subTabs.length],
      ["ArrowLeft", (at - 1 + subTabs.length) % subTabs.length],
      ["Home", 0],
      ["End", subTabs.length - 1],
    ]).get(event.key);
    const target = to === undefined ? undefined : subTabs[to];
    if (target === undefined) return;
    event.preventDefault();
    pick(target.key);
  };

  if (conversation === null || selected === null) {
    return (
      <p className="side-empty">
        Nothing to show yet. {app.title} shows here when the agent uses it.
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
        {subTabs.map((sub, at) => {
          const isSelected = sub.key === selected.key;
          return (
            <button
              key={sub.key}
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
              data-selected={isSelected}
              onClick={() => {
                pick(sub.key);
              }}
              onKeyDown={(event) => {
                onKey(event, at);
              }}
              className="side-sub"
            >
              {sub.title}
            </button>
          );
        })}
      </div>
      {subTabs
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

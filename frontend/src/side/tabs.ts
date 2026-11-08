// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The tabs of the right side panel: what can be opened, what is open, and
 * how this browser remembers it (`docs/specs/frontend.md`).
 *
 * **A tab is an offer the person took.** The panel is handed the list of
 * tabs that can be opened, in the order the [+] menu shows them -- today the
 * agent's MCP apps -- and a renderer for each kind. What is open is a list
 * of the offers' keys, per conversation.
 *
 * **The extension point is `TabKind` and `TabRenderers`.** A new kind of
 * tab -- a built-in every conversation can open, say -- is a new member of
 * `TabKind`; `TabRenderers` is a record over it, so the shell does not
 * compile until it says how that kind is drawn. An MCP app's tab is kind
 * `"app"`, keyed `app:<server>/<tool>` (`appTabKey`), one offer per app the
 * agent has (`./apps.ts`).
 *
 * Nothing here draws anything and nothing here asks the network: the
 * functions below are pure, and storage is read and written through
 * `../shell/storage.ts`, which survives storage being off.
 */
import type { ReactNode } from "react";

import { forget, remember, remembered } from "../shell/storage";

/**
 * The kinds of tab the panel can draw, one renderer each.
 *
 * MCP apps are one kind, `"app"`, for every app, told apart by the offer's
 * key. A tab of another sort is a kind of its own, added here.
 */
export type TabKind = "app";

/** One tab that can be opened: an entry of the [+] menu. */
export interface TabOffer {
  /**
   * Unique among the offers, stable across reloads: it is what is
   * remembered. An app's is `appTabKey`.
   */
  readonly key: string;
  /** What the tab and the menu entry say. */
  readonly title: string;
  readonly kind: TabKind;
}

/** How one kind of tab is drawn, given the offer it was opened from. */
export type TabRenderer = (offer: TabOffer) => ReactNode;

/** A renderer for every kind: the record the compiler holds complete. */
export type TabRenderers = Readonly<Record<TabKind, TabRenderer>>;

/** The key an MCP app's tab is remembered by. */
export function appTabKey(server: string, tool: string): string {
  return `app:${server}/${tool}`;
}

/** What is open in one conversation, and which of it is on top. */
export interface OpenTabs {
  readonly open: readonly string[];
  readonly selected: string | null;
}

export const NO_TABS: OpenTabs = { open: [], selected: null };

/**
 * What the open tabs of the empty chat are remembered under.
 *
 * Conversation ids are uuids, so no conversation is called this. When a
 * first message creates the conversation, what was open here moves to it
 * (`carryOpenTabs`), and the next empty chat starts with nothing open.
 */
export const NEW_CHAT_TABS = "new";

const TABS_KEY = "side.tabs.";

/**
 * What this browser left open in a conversation.
 *
 * Whatever is stored is read defensively: it is this origin's storage, and
 * anything that is not a list of strings with one of them selected is taken
 * for nothing.
 */
export function rememberedTabs(conversation: string): OpenTabs {
  const text = remembered(TABS_KEY + conversation);
  if (text === null) return NO_TABS;
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    return NO_TABS;
  }
  if (typeof value !== "object" || value === null) return NO_TABS;
  const { open, selected } = value as Record<string, unknown>;
  if (!Array.isArray(open)) return NO_TABS;
  const keys = [
    ...new Set(open.filter((key): key is string => typeof key === "string")),
  ];
  return {
    open: keys,
    selected:
      typeof selected === "string" && keys.includes(selected)
        ? selected
        : (keys[0] ?? null),
  };
}

/** Remember what is open; nothing open is nothing to keep. */
export function rememberTabs(conversation: string, tabs: OpenTabs): void {
  if (tabs.open.length === 0) {
    forget(TABS_KEY + conversation);
    return;
  }
  remember(TABS_KEY + conversation, JSON.stringify(tabs));
}

/** Move what was open under one key to another, as a new chat gets its id. */
export function carryOpenTabs(from: string, to: string): void {
  const tabs = rememberedTabs(from);
  forget(TABS_KEY + from);
  if (tabs.open.length > 0) rememberTabs(to, tabs);
}

/** Open a tab, or bring it to the top if it already is. */
export function withOpened(tabs: OpenTabs, key: string): OpenTabs {
  return {
    open: tabs.open.includes(key) ? tabs.open : [...tabs.open, key],
    selected: key,
  };
}

/**
 * Close a tab. The one on top goes to its right-hand neighbour, or the
 * left-hand one when it was the last, as tab strips usually do.
 */
export function withClosed(tabs: OpenTabs, key: string): OpenTabs {
  const at = tabs.open.indexOf(key);
  if (at === -1) return tabs;
  const open = tabs.open.filter((one) => one !== key);
  if (tabs.selected !== key) return { open, selected: tabs.selected };
  return { open, selected: open[at] ?? open[at - 1] ?? null };
}

/** The open tabs as offers, in their order, and the one on top. */
export function shownTabs(
  tabs: OpenTabs,
  offers: readonly TabOffer[],
): { open: readonly TabOffer[]; selected: TabOffer | null } {
  // An open tab nobody offers any more -- an app the agent lost, or one whose
  // list has not arrived yet -- is not drawn, and stays remembered: it comes
  // back when its offer does.
  const open = tabs.open.flatMap((key) => {
    const offer = offers.find((one) => one.key === key);
    return offer === undefined ? [] : [offer];
  });
  const selected =
    open.find((offer) => offer.key === tabs.selected) ?? open[0] ?? null;
  return { open, selected };
}

/** What the [+] menu lists: every offer not open, in the offers' order. */
export function addableTabs(
  tabs: OpenTabs,
  offers: readonly TabOffer[],
): readonly TabOffer[] {
  return offers.filter((offer) => !tabs.open.includes(offer.key));
}

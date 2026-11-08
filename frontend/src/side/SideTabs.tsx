// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The side panel's tabs for one conversation: the strip, the [+] menu, and
 * the panels (`./SidePanel.tsx`, `./tabs.ts`).
 *
 * **Keyboard**, as the ARIA practices write a tab list and a menu button:
 *
 * - in the strip, only the tab on top is a Tab stop; Left and Right move
 *   to the neighbouring tab and bring it to the top, Home and End to the
 *   ends, Delete closes it. Its close button is the next Tab stop.
 * - "Add a tab" opens the menu on Enter, Space or Down (Up opens it at the
 *   end); in the menu Up and Down move, Home and End go to the ends, Enter
 *   picks, and Escape closes it and puts the focus back on the button. Tab,
 *   or a click anywhere else, closes it too.
 *
 * **The panels stay mounted.** Every open tab's panel is in the page, and
 * those not on top are `hidden`: what a tab holds -- an app's frame and
 * what its view shows -- is not thrown away when another is looked at.
 */
import {
  type KeyboardEvent,
  type RefObject,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";

import { PlusIcon, XIcon } from "../icons";
import {
  addableTabs,
  type OpenTabs,
  rememberedTabs,
  rememberTabs,
  shownTabs,
  type TabOffer,
  type TabRenderers,
  withClosed,
  withOpened,
} from "./tabs";

/** Where the focus goes once the next render is on the screen. */
type FocusNext = { tab: string } | "add" | null;

export function SideTabs({
  conversation,
  offers,
  renderers,
  hidden,
}: Readonly<{
  conversation: string;
  offers: readonly TabOffer[];
  renderers: TabRenderers;
  hidden: boolean;
}>) {
  const [tabs, setTabs] = useState<OpenTabs>(() =>
    rememberedTabs(conversation),
  );
  const { open, selected } = shownTabs(tabs, offers);
  const addable = addableTabs(tabs, offers);
  const base = useId();
  const tabRefs = useRef(new Map<string, HTMLButtonElement>());
  const addRef = useRef<HTMLButtonElement>(null);
  const focusNext = useRef<FocusNext>(null);

  // A tab opened, closed or moved to: the focus goes where the person's
  // attention is, once that element exists.
  useEffect(() => {
    const next = focusNext.current;
    if (next === null) return;
    focusNext.current = null;
    if (next === "add") addRef.current?.focus();
    else tabRefs.current.get(next.tab)?.focus();
  });

  const change = (next: OpenTabs, focus: FocusNext) => {
    focusNext.current = focus;
    setTabs(next);
    rememberTabs(conversation, next);
  };
  const select = (key: string) => {
    change({ open: tabs.open, selected: key }, { tab: key });
  };
  const close = (key: string) => {
    const next = withClosed(tabs, key);
    const after = shownTabs(next, offers).selected;
    change(next, after === null ? "add" : { tab: after.key });
  };

  const tabId = (key: string) => `${base}-tab-${encodeURIComponent(key)}`;
  const panelId = (key: string) => `${base}-panel-${encodeURIComponent(key)}`;

  const onTabKey = (event: KeyboardEvent<HTMLButtonElement>, at: number) => {
    const current = open[at];
    if (current === undefined) return;
    if (event.key === "Delete") {
      event.preventDefault();
      close(current.key);
      return;
    }
    const to = new Map([
      ["ArrowRight", (at + 1) % open.length],
      ["ArrowLeft", (at - 1 + open.length) % open.length],
      ["Home", 0],
      ["End", open.length - 1],
    ]).get(event.key);
    if (to === undefined) return;
    event.preventDefault();
    const target = open[to];
    if (target !== undefined) select(target.key);
  };

  return (
    <>
      <div className="side-strip" hidden={hidden}>
        {open.length > 0 && (
          <div
            role="tablist"
            aria-label="Side panel tabs"
            className="side-tabs"
          >
            {open.map((offer, at) => {
              const isSelected = offer.key === selected?.key;
              return (
                // The close button sits beside its tab, not inside it: a
                // button in a button is no button at all to a reader.
                <div
                  key={offer.key}
                  role="presentation"
                  data-selected={isSelected}
                  className="side-tab"
                >
                  <button
                    type="button"
                    role="tab"
                    id={tabId(offer.key)}
                    ref={(node) => {
                      if (node === null) tabRefs.current.delete(offer.key);
                      else tabRefs.current.set(offer.key, node);
                    }}
                    aria-selected={isSelected}
                    aria-controls={panelId(offer.key)}
                    tabIndex={isSelected ? 0 : -1}
                    title={offer.title}
                    onClick={() => {
                      select(offer.key);
                    }}
                    onKeyDown={(event) => {
                      onTabKey(event, at);
                    }}
                    className="side-tab-name"
                  >
                    {offer.title}
                  </button>
                  <button
                    type="button"
                    aria-label={`Close ${offer.title}`}
                    title={`Close ${offer.title}`}
                    tabIndex={isSelected ? 0 : -1}
                    onClick={() => {
                      close(offer.key);
                    }}
                    className="side-tab-close"
                  >
                    <XIcon className="side-icon-small" />
                  </button>
                </div>
              );
            })}
          </div>
        )}
        <AddTab
          offers={addable}
          anyOffered={offers.length > 0}
          buttonRef={addRef}
          onPick={(key) => {
            change(withOpened(tabs, key), { tab: key });
          }}
        />
      </div>
      <div className="side-body" hidden={hidden}>
        {open.length === 0 && (
          <p className="side-empty">
            {offers.length > 0
              ? "No tab is open in this conversation. “Add a tab” opens one."
              : "No tab is open in this conversation, and there is none to add: its agent has no apps."}
          </p>
        )}
        {open.map((offer) => (
          <div
            key={offer.key}
            role="tabpanel"
            id={panelId(offer.key)}
            aria-labelledby={tabId(offer.key)}
            hidden={offer.key !== selected?.key}
            data-kind={offer.kind}
            // A stop of its own, so a keyboard reaches what it says even
            // when nothing in it can be focused.
            tabIndex={0}
            className="side-panel"
          >
            {renderers[offer.kind](offer)}
          </div>
        ))}
      </div>
    </>
  );
}

/**
 * The [+] button and its menu: every tab that can be opened and is not.
 *
 * With nothing left to open the button is disabled, and says why: every
 * tab offered is open, or none is offered at all.
 */
function AddTab({
  offers,
  anyOffered,
  buttonRef,
  onPick,
}: Readonly<{
  offers: readonly TabOffer[];
  anyOffered: boolean;
  buttonRef: RefObject<HTMLButtonElement | null>;
  onPick: (key: string) => void;
}>) {
  // Closed, or open with the focus going to that entry.
  const [openAt, setOpenAt] = useState<number | null>(null);
  const isOpen = openAt !== null && offers.length > 0;
  const menuId = useId();
  const self = useRef<HTMLDivElement>(null);
  const items = useRef<(HTMLButtonElement | null)[]>([]);

  useEffect(() => {
    if (openAt !== null) items.current[openAt]?.focus();
  }, [openAt]);

  // A click anywhere else closes it.
  useEffect(() => {
    if (!isOpen) return;
    const onPointer = (event: Event) => {
      if (!(event.target instanceof Node)) return;
      if (self.current?.contains(event.target) === true) return;
      setOpenAt(null);
    };
    document.addEventListener("pointerdown", onPointer);
    return () => {
      document.removeEventListener("pointerdown", onPointer);
    };
  }, [isOpen]);

  const shut = () => {
    setOpenAt(null);
    buttonRef.current?.focus();
  };

  const onMenuKey = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      // The innermost thing open is what Escape shuts: not the drawer.
      event.preventDefault();
      event.stopPropagation();
      shut();
      return;
    }
    if (event.key === "Tab") {
      setOpenAt(null);
      return;
    }
    const at = items.current.findIndex(
      (item) => item !== null && item === document.activeElement,
    );
    const last = offers.length - 1;
    const to = new Map([
      ["ArrowDown", at >= last ? 0 : at + 1],
      ["ArrowUp", at <= 0 ? last : at - 1],
      ["Home", 0],
      ["End", last],
    ]).get(event.key);
    if (to === undefined) return;
    event.preventDefault();
    items.current[to]?.focus();
  };

  const nothingLeft = offers.length === 0;
  const label = !nothingLeft
    ? "Add a tab"
    : anyOffered
      ? "Every tab is open"
      : "No tab to add";
  return (
    <div ref={self} className="side-add">
      <button
        type="button"
        ref={buttonRef}
        aria-label="Add a tab"
        title={label}
        aria-haspopup="menu"
        aria-expanded={isOpen}
        aria-controls={isOpen ? menuId : undefined}
        disabled={nothingLeft}
        onClick={() => {
          setOpenAt(isOpen ? null : 0);
        }}
        onKeyDown={(event) => {
          if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
          event.preventDefault();
          setOpenAt(event.key === "ArrowDown" ? 0 : offers.length - 1);
        }}
        className="side-tool"
      >
        <PlusIcon className="side-icon" />
      </button>
      {isOpen && (
        <div
          role="menu"
          id={menuId}
          aria-label="Tabs to add"
          onKeyDown={onMenuKey}
          className="side-menu"
        >
          {offers.map((offer, at) => (
            <button
              key={offer.key}
              type="button"
              role="menuitem"
              tabIndex={-1}
              ref={(node) => {
                items.current[at] = node;
              }}
              onClick={() => {
                setOpenAt(null);
                onPick(offer.key);
              }}
              className="side-menu-item"
            >
              {offer.title}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { createRef } from "react";
import { expect, test } from "vitest";

import {
  DEFAULT_WIDTH,
  MIN_WIDTH,
  SIDE_KEY,
  SIDE_WIDTH_KEY,
  SidePanel,
  STEP,
} from "./SidePanel";
import {
  BUILT_IN_TABS,
  carryOpenTabs,
  NEW_CHAT_TABS,
  rememberedTabs,
  type TabRenderers,
} from "./tabs";

const A = "00000000-0000-4000-8000-000000000001";
const B = "00000000-0000-4000-8000-000000000002";

const RENDERERS: TabRenderers = {
  "context-window": () => <p>What the window holds</p>,
  usage: () => <p>What was used</p>,
};

function side(conversation = A) {
  const props = {
    offers: BUILT_IN_TABS,
    renderers: RENDERERS,
    drawer: false,
    onCloseDrawer: () => undefined,
    closeRef: createRef<HTMLButtonElement>(),
  };
  const drawn = render(<SidePanel conversation={conversation} {...props} />);
  return {
    ...drawn,
    to: (next: string) => {
      drawn.rerender(<SidePanel conversation={next} {...props} />);
    },
  };
}

/** Draw it already open, as a browser that left it open would. */
function opened(conversation = A) {
  localStorage.setItem(`robinauts.${SIDE_KEY}`, "open");
  return side(conversation);
}

const panel = () => screen.getByRole("complementary", { name: "Side panel" });
const add = () => screen.getByRole("button", { name: "Add a tab" });
const menuItems = () =>
  within(screen.getByRole("menu", { name: "Tabs to add" }))
    .getAllByRole("menuitem")
    .map((item) => item.textContent);
const tabNames = () =>
  screen.queryAllByRole("tab").map((tab) => tab.textContent);

function pick(title: string) {
  fireEvent.click(add());
  fireEvent.click(screen.getByRole("menuitem", { name: title }));
}

test("it starts as the rail, and which it is is remembered", () => {
  side();
  expect(panel()).toHaveAttribute("data-collapsed", "true");
  // Folded away, but not thrown away: hidden from everyone.
  expect(screen.queryByRole("button", { name: "Add a tab" })).toBeNull();
  expect(screen.queryByRole("separator")).toBeNull();

  fireEvent.click(
    screen.getByRole("button", { name: "Expand the side panel" }),
  );
  expect(panel()).toHaveAttribute("data-collapsed", "false");
  expect(localStorage.getItem(`robinauts.${SIDE_KEY}`)).toBe("open");
  expect(add()).toBeVisible();

  fireEvent.click(
    screen.getByRole("button", { name: "Collapse the side panel" }),
  );
  expect(panel()).toHaveAttribute("data-collapsed", "true");
  expect(localStorage.getItem(`robinauts.${SIDE_KEY}`)).toBe("rail");
});

test("a browser that left it open opens it open, with nothing in it", () => {
  opened();
  expect(panel()).toHaveAttribute("data-collapsed", "false");
  expect(screen.queryByRole("tablist")).toBeNull();
  expect(screen.getByText(/No tab is open in this conversation/)).toBeVisible();
});

test("[+] lists what is not open; opening one takes it off, closing puts it back", () => {
  opened();
  fireEvent.click(add());
  expect(add()).toHaveAttribute("aria-expanded", "true");
  expect(menuItems()).toEqual(["Context window", "Usage"]);

  fireEvent.click(screen.getByRole("menuitem", { name: "Usage" }));
  expect(screen.queryByRole("menu")).toBeNull();
  expect(tabNames()).toEqual(["Usage"]);
  const usage = screen.getByRole("tab", { name: "Usage" });
  expect(usage).toHaveAttribute("aria-selected", "true");
  // The new tab has the focus, and its panel is the one shown.
  expect(usage).toHaveFocus();
  expect(screen.getByRole("tabpanel", { name: "Usage" })).toHaveTextContent(
    "What was used",
  );

  fireEvent.click(add());
  expect(menuItems()).toEqual(["Context window"]);
  fireEvent.click(screen.getByRole("menuitem", { name: "Context window" }));
  expect(tabNames()).toEqual(["Usage", "Context window"]);
  // Everything is open: nothing left to add, and the button says so.
  expect(add()).toBeDisabled();
  expect(add()).toHaveAttribute("title", "Every tab is open");

  fireEvent.click(screen.getByRole("button", { name: "Close Usage" }));
  expect(tabNames()).toEqual(["Context window"]);
  expect(add()).toBeEnabled();
  fireEvent.click(add());
  expect(menuItems()).toEqual(["Usage"]);
});

test("closing the tab on top brings up its neighbour; closing the last leaves the focus on [+]", () => {
  opened();
  pick("Context window");
  pick("Usage");
  fireEvent.click(screen.getByRole("tab", { name: "Context window" }));
  fireEvent.click(screen.getByRole("button", { name: "Close Context window" }));
  const usage = screen.getByRole("tab", { name: "Usage" });
  expect(usage).toHaveAttribute("aria-selected", "true");
  expect(usage).toHaveFocus();
  fireEvent.click(screen.getByRole("button", { name: "Close Usage" }));
  expect(screen.queryByRole("tablist")).toBeNull();
  expect(add()).toHaveFocus();
});

test("the panels not on top stay in the page, hidden", () => {
  opened();
  pick("Context window");
  pick("Usage");
  expect(screen.getByText("What the window holds")).not.toBeVisible();
  expect(screen.getByText("What was used")).toBeVisible();
  fireEvent.click(screen.getByRole("tab", { name: "Context window" }));
  expect(screen.getByText("What the window holds")).toBeVisible();
  expect(screen.getByText("What was used")).not.toBeVisible();
});

test("open tabs are remembered per conversation", () => {
  const { to, unmount } = opened(A);
  pick("Usage");
  expect(rememberedTabs(A)).toEqual({ open: ["usage"], selected: "usage" });

  to(B);
  expect(screen.queryByRole("tablist")).toBeNull();
  pick("Context window");

  to(A);
  expect(tabNames()).toEqual(["Usage"]);
  unmount();

  // And across a reload.
  side(B);
  expect(tabNames()).toEqual(["Context window"]);
});

test("closing every tab forgets the conversation's entry", () => {
  opened(A);
  pick("Usage");
  fireEvent.click(screen.getByRole("button", { name: "Close Usage" }));
  expect(localStorage.getItem(`robinauts.side.tabs.${A}`)).toBeNull();
});

test("a new chat's tabs move to the conversation it becomes", () => {
  const { to } = opened(NEW_CHAT_TABS);
  pick("Usage");
  carryOpenTabs(NEW_CHAT_TABS, A);
  to(A);
  expect(tabNames()).toEqual(["Usage"]);
  // The next new chat starts with nothing open.
  to(NEW_CHAT_TABS);
  expect(screen.queryByRole("tablist")).toBeNull();
});

test("what is remembered is read with care", () => {
  localStorage.setItem(`robinauts.side.tabs.${A}`, "{not json");
  expect(rememberedTabs(A)).toEqual({ open: [], selected: null });
  localStorage.setItem(
    `robinauts.side.tabs.${A}`,
    JSON.stringify({ open: ["usage", 3, "usage", "gone"], selected: "nope" }),
  );
  expect(rememberedTabs(A)).toEqual({
    open: ["usage", "gone"],
    selected: "usage",
  });
  // A tab nobody offers any more is not drawn, and is kept for when it is.
  opened(A);
  expect(tabNames()).toEqual(["Usage"]);
});

test("the tabs answer the arrow keys, Home, End and Delete", () => {
  opened();
  pick("Context window");
  pick("Usage");
  const usage = screen.getByRole("tab", { name: "Usage" });
  const contextWindow = screen.getByRole("tab", { name: "Context window" });
  // One Tab stop in the strip: the tab on top.
  expect(usage).toHaveAttribute("tabindex", "0");
  expect(contextWindow).toHaveAttribute("tabindex", "-1");

  fireEvent.keyDown(usage, { key: "ArrowRight" });
  expect(contextWindow).toHaveFocus();
  expect(contextWindow).toHaveAttribute("aria-selected", "true");
  fireEvent.keyDown(contextWindow, { key: "ArrowLeft" });
  expect(usage).toHaveFocus();
  fireEvent.keyDown(usage, { key: "Home" });
  expect(contextWindow).toHaveFocus();
  fireEvent.keyDown(contextWindow, { key: "End" });
  expect(usage).toHaveFocus();

  fireEvent.keyDown(usage, { key: "Delete" });
  expect(tabNames()).toEqual(["Context window"]);
  expect(screen.getByRole("tab", { name: "Context window" })).toHaveFocus();
});

test("the menu answers the keyboard, and Escape puts the focus back", () => {
  opened();
  fireEvent.keyDown(add(), { key: "ArrowDown" });
  const [first, second] = screen.getAllByRole("menuitem");
  expect(first).toHaveFocus();
  const menu = screen.getByRole("menu");
  fireEvent.keyDown(menu, { key: "ArrowDown" });
  expect(second).toHaveFocus();
  fireEvent.keyDown(menu, { key: "ArrowDown" });
  expect(first).toHaveFocus();
  fireEvent.keyDown(menu, { key: "ArrowUp" });
  expect(second).toHaveFocus();
  fireEvent.keyDown(menu, { key: "Home" });
  expect(first).toHaveFocus();

  fireEvent.keyDown(menu, { key: "Escape" });
  expect(screen.queryByRole("menu")).toBeNull();
  expect(add()).toHaveFocus();

  // Up opens it at the end.
  fireEvent.keyDown(add(), { key: "ArrowUp" });
  expect(screen.getByRole("menuitem", { name: "Usage" })).toHaveFocus();
});

test("a click elsewhere closes the menu", () => {
  opened();
  fireEvent.click(add());
  expect(screen.getByRole("menu")).toBeInTheDocument();
  fireEvent.pointerDown(document.body);
  expect(screen.queryByRole("menu")).toBeNull();
});

test("the edge is a separator the arrow keys move, and the width is remembered", () => {
  opened();
  const edge = screen.getByRole("separator", { name: "Resize the side panel" });
  expect(edge).toHaveAttribute("aria-orientation", "vertical");
  expect(edge).toHaveAttribute("aria-valuenow", String(DEFAULT_WIDTH));
  expect(edge).toHaveAttribute("aria-valuemin", String(MIN_WIDTH));
  // jsdom's window is 1024 wide; the most it may take is 60% of it.
  expect(edge).toHaveAttribute("aria-valuemax", "614");

  // The edge is on the left: Left widens, Right narrows.
  fireEvent.keyDown(edge, { key: "ArrowLeft" });
  expect(edge).toHaveAttribute("aria-valuenow", String(DEFAULT_WIDTH + STEP));
  expect(localStorage.getItem(`robinauts.${SIDE_WIDTH_KEY}`)).toBe(
    String(DEFAULT_WIDTH + STEP),
  );
  fireEvent.keyDown(edge, { key: "ArrowRight" });
  fireEvent.keyDown(edge, { key: "ArrowRight" });
  expect(edge).toHaveAttribute("aria-valuenow", String(DEFAULT_WIDTH - STEP));
  fireEvent.keyDown(edge, { key: "End" });
  expect(edge).toHaveAttribute("aria-valuenow", "614");
  fireEvent.keyDown(edge, { key: "ArrowLeft" });
  expect(edge).toHaveAttribute("aria-valuenow", "614");
  fireEvent.keyDown(edge, { key: "Home" });
  expect(edge).toHaveAttribute("aria-valuenow", String(MIN_WIDTH));
  expect(panel().style.getPropertyValue("--side-width")).toBe(
    `${String(MIN_WIDTH)}px`,
  );
});

test("dragging the edge resizes it, and the width is kept when it is let go", () => {
  opened();
  const edge = screen.getByRole("separator");
  fireEvent.pointerDown(edge, { button: 0, clientX: 700, pointerId: 1 });
  fireEvent.pointerMove(edge, { clientX: 600, pointerId: 1 });
  expect(edge).toHaveAttribute("aria-valuenow", String(DEFAULT_WIDTH + 100));
  // Not written until the drag ends.
  expect(localStorage.getItem(`robinauts.${SIDE_WIDTH_KEY}`)).toBeNull();
  fireEvent.pointerUp(edge, { clientX: 650, pointerId: 1 });
  expect(edge).toHaveAttribute("aria-valuenow", String(DEFAULT_WIDTH + 50));
  expect(localStorage.getItem(`robinauts.${SIDE_WIDTH_KEY}`)).toBe(
    String(DEFAULT_WIDTH + 50),
  );
  // A move after the drag ended moves nothing.
  fireEvent.pointerMove(edge, { clientX: 100, pointerId: 1 });
  expect(edge).toHaveAttribute("aria-valuenow", String(DEFAULT_WIDTH + 50));
});

test("a remembered width comes back, held inside the window", () => {
  localStorage.setItem(`robinauts.${SIDE_WIDTH_KEY}`, "400");
  const { unmount } = opened();
  expect(screen.getByRole("separator")).toHaveAttribute("aria-valuenow", "400");
  unmount();
  localStorage.setItem(`robinauts.${SIDE_WIDTH_KEY}`, "5000");
  opened();
  expect(screen.getByRole("separator")).toHaveAttribute("aria-valuenow", "614");
});

test("a narrower window narrows the most it may take", () => {
  opened();
  const edge = screen.getByRole("separator");
  fireEvent.keyDown(edge, { key: "End" });
  act(() => {
    window.innerWidth = 800;
    window.dispatchEvent(new Event("resize"));
  });
  expect(edge).toHaveAttribute("aria-valuemax", "480");
  expect(edge).toHaveAttribute("aria-valuenow", "480");
  act(() => {
    window.innerWidth = 1024;
    window.dispatchEvent(new Event("resize"));
  });
});

// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

/**
 * What jsdom does not have and the chat window asks a browser for.
 *
 * jsdom implements no layout, so the three below are simply absent rather
 * than wrong: the chat's message viewport observes its own size and the size
 * of the message it is anchored to, and scrolls itself to the bottom. None
 * of them is a thing a test here asserts about -- what is tested is what is
 * on the screen and what was sent -- and without them a render throws and
 * every test of a page holding the chat fails for a reason that is not about
 * the page.
 *
 * Written out rather than taken from a package: three empty methods, and a
 * dependency would be a dependency.
 */
class NoObserver {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
  takeRecords(): [] {
    return [];
  }
}
globalThis.ResizeObserver ??= NoObserver as unknown as typeof ResizeObserver;
globalThis.IntersectionObserver ??=
  NoObserver as unknown as typeof IntersectionObserver;
Element.prototype.scrollIntoView ??= () => undefined;
Element.prototype.scrollTo ??= () => undefined;

// One test leaves nothing behind for the next: the DOM, the spies, the hash
// the routing will read and the browser storage the rail state lives in.
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  // A stubbed global outlives restoreAllMocks, and a test that stubbed
  // `fetch` would hand it to the next one.
  vi.unstubAllGlobals();
  location.hash = "";
  localStorage.clear();
  // The theme is an attribute on <html>, which `cleanup` does not touch.
  document.documentElement.removeAttribute("data-theme");
});

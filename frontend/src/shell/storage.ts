// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * What this browser remembers about the interface: the rail, the panel's
 * tab (Sessions or Flows), the theme, the agent last talked to and the model
 * last picked.
 *
 * Per browser, never per account: it is a preference about this window, not
 * something the backend is told. Keys are namespaced, because the deployment
 * is at the origin root and `localStorage` is the whole origin's.
 *
 * Every read and write is wrapped. Storage can be off -- a private window
 * with site data blocked throws on the first `getItem` -- and a remembered
 * preference is never worth a blank page.
 */
const PREFIX = "robinauts.";

export function remembered(key: string): string | null {
  try {
    return localStorage.getItem(PREFIX + key);
  } catch {
    return null;
  }
}

export function forget(key: string): void {
  try {
    localStorage.removeItem(PREFIX + key);
  } catch {
    // Off. Nothing was kept, so nothing is left behind.
  }
}

export function remember(key: string, value: string): void {
  try {
    localStorage.setItem(PREFIX + key, value);
  } catch {
    // Off, or full. The preference lasts as long as the page does.
  }
}

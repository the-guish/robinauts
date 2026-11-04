// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * Light, dark, or whatever the operating system says
 * (`docs/specs/frontend.md`).
 *
 * The choice is an attribute on `<html>`, which `src/tokens.css` reads:
 * `data-theme="light"`, `data-theme="dark"`, or no attribute at all for the
 * operating system's. It is remembered per browser.
 */
import { useEffect, useState } from "react";

import { MonitorIcon, MoonIcon, SunIcon } from "../icons";
import { remember, remembered } from "./storage";
import "./theme.css";

export type Theme = "light" | "dark" | "system";

export const THEME_KEY = "theme";

const OPTIONS: { id: Theme; label: string; Icon: typeof SunIcon }[] = [
  { id: "light", label: "Light", Icon: SunIcon },
  { id: "dark", label: "Dark", Icon: MoonIcon },
  { id: "system", label: "System", Icon: MonitorIcon },
];

function isTheme(value: string | null): value is Theme {
  return value === "light" || value === "dark" || value === "system";
}

/** What this browser last chose; the operating system's until it chooses. */
export function rememberedTheme(): Theme {
  const kept = remembered(THEME_KEY);
  return isTheme(kept) ? kept : "system";
}

/** Put the choice on `<html>`. The operating system's is no attribute. */
export function applyTheme(theme: Theme): void {
  const root = document.documentElement;
  if (theme === "system") {
    delete root.dataset.theme;
    return;
  }
  root.dataset.theme = theme;
}

/**
 * The remembered choice, applied before anything is drawn.
 *
 * `main.tsx` calls this, rather than the toggle's own effect alone: an
 * effect runs after the first paint, and a person who chose dark on a
 * machine set to light would see the light page flash past first.
 */
export function applyRememberedTheme(): void {
  applyTheme(rememberedTheme());
}

export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(rememberedTheme);
  useEffect(() => {
    applyTheme(theme);
    remember(THEME_KEY, theme);
  }, [theme]);
  return (
    <div role="group" aria-label="Theme" className="theme">
      {OPTIONS.map(({ id, label, Icon }) => (
        <button
          key={id}
          type="button"
          title={label}
          aria-pressed={theme === id}
          onClick={() => {
            setTheme(id);
          }}
          className="theme-option"
        >
          <Icon className="theme-icon" />
          <span className="theme-label">{label}</span>
        </button>
      ))}
    </div>
  );
}

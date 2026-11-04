// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

// The global stylesheet before any component's: every other stylesheet is
// written against its base, so it has to come first in the cascade, where a
// rule of a component's wins over it at equal specificity.
import "./styles.css";
import { App } from "./App";
import { applyRememberedTheme } from "./shell/ThemeToggle";

// Before the first paint, so a browser set to dark by hand does not show the
// light page for a frame first.
applyRememberedTheme();

const root = document.getElementById("root");
if (root === null) {
  throw new Error("index.html has no #root to mount on");
}

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * What stands in for the shell when a render throws.
 *
 * A single-page application that throws while rendering unmounts the whole
 * tree and leaves a blank page: no message, no way back, and nothing on
 * screen to say that anything went wrong. This catches that and says so.
 *
 * Deliberately small, and deliberately a class: an error boundary is the one
 * thing React still has no hook for. It reports nothing anywhere -- there is
 * no error-reporting service in this project, and there is not going to be
 * one without a decision of its own.
 */
import { Component, type ErrorInfo, type ReactNode } from "react";

import "./session/session.css";

interface Caught {
  failure: Error | null;
}

export class ErrorBoundary extends Component<{ children: ReactNode }, Caught> {
  override state: Caught = { failure: null };

  static getDerivedStateFromError(failure: unknown): Caught {
    return {
      failure: failure instanceof Error ? failure : new Error(String(failure)),
    };
  }

  override componentDidCatch(failure: Error, info: ErrorInfo): void {
    // The console is where a developer looks, and the only place this goes.
    console.error(
      "the interface failed to render",
      failure,
      info.componentStack,
    );
  }

  override render(): ReactNode {
    const { failure } = this.state;
    if (failure === null) return this.props.children;
    return (
      <main className="page">
        <h1 className="page-title">Something went wrong</h1>
        <p role="alert" className="page-failed">
          {failure.message}
        </p>
        <p>
          <button
            type="button"
            onClick={() => {
              location.reload();
            }}
            className="page-button"
          >
            Reload
          </button>
        </p>
      </main>
    );
  }
}

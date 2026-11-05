// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * "Link a phone": the browser's half of pairing
 * (`docs/architecture/device-pairing.md`, "The flow", steps 1, 2, 5 and 6).
 *
 * Opened, it asks for a code and shows its QR. It then asks the backend how
 * the code is doing every two seconds -- while the dialog is open and the
 * tab is visible, and not otherwise -- and says so in words under the
 * picture. Once a phone has claimed the code, it asks for the number the
 * phone shows; the right one links the phone, a wrong one is counted, and
 * the third ends the pairing. **No**, Escape and closing the dialog while a
 * code waits or is claimed all delete the pairing.
 *
 * A code that expires unclaimed is replaced at once, up to five times in one
 * open dialog (`AUTO_RENEWALS`); after that the person asks for the next.
 * The name the phone gave itself is never shown here: it is information for
 * the list, not what the person decides by.
 */
import {
  type FormEvent,
  type KeyboardEvent,
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";

import { detailOf, isRefusal } from "../api/client";
import { XIcon } from "../icons";
import type { Session } from "../session/session";
import {
  approvePairing,
  BUSY,
  CONFLICT,
  deletePairing,
  FORBIDDEN,
  isGone,
  type Minted,
  mintPairing,
  needsFreshSignIn,
  type PairingView,
  pairingState,
  qrSource,
  typedNumber,
  WRONG_NUMBER,
} from "./devices";
import { FreshSignIn } from "./FreshSignIn";

/** How often the pairing is asked after (step 2). */
export const POLL_MS = 2000;

/** How many expired codes one open dialog replaces by itself. */
export const AUTO_RENEWALS = 5;

/** Wrong numbers a pairing takes; the last of them ends it. */
const TRIES = 3;

export const ALT = "QR code to scan with the Robinauts app";
export const SCAN =
  "Scan this with the Robinauts app on your phone. The app will show you a number to type here.";
export const WAITING = "Waiting for a phone to scan the code.";
export const CLAIMED = "A phone scanned your code. Type the number it shows.";
export const LINKED = "Linked.";
export const EXPIRED = "The code expired.";
export const BUSY_TEXT = "Too many codes at once. Wait a minute and try again.";
export const NO_PHONES = "This deployment allows no phones.";
export const NOT_SIX = "Type the six digits the phone shows.";

/** What the dialog is showing. */
type Stage =
  | { kind: "minting" }
  | {
      kind: "shown";
      pairing: Minted;
      state: "waiting" | "claimed";
      /** Wrong numbers typed for this pairing so far. */
      wrong: number;
    }
  | { kind: "linked" }
  | { kind: "expired" }
  | { kind: "gone"; afterWrong: boolean }
  | { kind: "fresh"; detail: string }
  | { kind: "refused"; detail: string; retry: boolean };

/** What a refused mint becomes. */
function refusedStage(failure: unknown): Stage {
  if (needsFreshSignIn(failure)) {
    return { kind: "fresh", detail: detailOf(failure) };
  }
  if (isRefusal(failure, BUSY)) {
    return { kind: "refused", detail: BUSY_TEXT, retry: true };
  }
  if (isRefusal(failure, FORBIDDEN)) {
    return { kind: "refused", detail: NO_PHONES, retry: false };
  }
  return { kind: "refused", detail: detailOf(failure), retry: true };
}

/** The state, in the words the dialog says it in. */
function sentence(stage: Stage): string {
  switch (stage.kind) {
    case "minting":
      return "Asking for a code…";
    case "shown":
      if (stage.state === "waiting") return WAITING;
      if (stage.wrong === 0) return CLAIMED;
      return TRIES - stage.wrong === 1
        ? "That is not the number. One try left."
        : "That is not the number. Two tries left.";
    case "linked":
      return LINKED;
    case "expired":
      return EXPIRED;
    case "gone":
      return stage.afterWrong
        ? "That was the third wrong number, so the code is gone."
        : "The code is gone.";
    case "fresh":
      return "Your sign-in is too old to link a phone.";
    case "refused":
      return stage.detail;
  }
}

/** A pairing nobody is going to use: deleted, and a refusal is no matter. */
function forget(id: string): void {
  deletePairing(id).catch(() => undefined);
}

function subscribeVisibility(changed: () => void): () => void {
  document.addEventListener("visibilitychange", changed);
  return () => {
    document.removeEventListener("visibilitychange", changed);
  };
}

/** Whether the tab is on the screen: polling a hidden one is for nobody. */
function useVisible(): boolean {
  return useSyncExternalStore(
    subscribeVisibility,
    () => document.visibilityState === "visible",
    () => true,
  );
}

const FOCUSABLE = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
].join(", ");

export function LinkDialog({
  session,
  onClose,
  onLinked,
}: Readonly<{
  session: Session;
  onClose: () => void;
  /** A phone was linked: the list has one more. */
  onLinked: () => void;
}>) {
  const [stage, setStage] = useState<Stage>({ kind: "minting" });
  // Each round is one code asked for; the first is the dialog opening.
  const [round, setRound] = useState(0);
  // Each answer of a poll schedules the next.
  const [tick, setTick] = useState(0);
  const [typed, setTyped] = useState("");
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const visible = useVisible();
  // How many expired codes this dialog has replaced by itself.
  const renewals = useRef(0);
  // The pairing that is still the server's to delete: waiting or claimed.
  const held = useRef<string | null>(null);
  const self = useRef<HTMLDivElement>(null);
  const field = useRef<HTMLInputElement>(null);
  const primary = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  const fieldId = useId();
  const errorId = useId();

  // Step 1: ask for a code. A code that arrives for a dialog that has gone,
  // or for a round that has been replaced, is deleted at once.
  useEffect(() => {
    let dropped = false;
    mintPairing().then(
      (minted) => {
        if (dropped) {
          forget(minted.id);
          return;
        }
        held.current = minted.id;
        setStage({
          kind: "shown",
          pairing: minted,
          state: "waiting",
          wrong: 0,
        });
      },
      (failure: unknown) => {
        if (!dropped) setStage(refusedStage(failure));
      },
    );
    return () => {
      dropped = true;
    };
  }, [round]);

  // Closing the dialog, however it happens -- Escape, the close button, the
  // page going away -- deletes a code that waits or is claimed.
  useEffect(() => {
    const pairing = held;
    return () => {
      const id = pairing.current;
      pairing.current = null;
      if (id !== null) forget(id);
    };
  }, []);

  // The focus goes into the dialog, and back to what opened it on the way
  // out, rather than being dropped on <body>.
  useEffect(() => {
    const opener =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    self.current?.focus();
    return () => {
      opener?.focus();
    };
  }, []);

  /** Another code, asked for now. */
  const renew = useCallback(() => {
    held.current = null;
    setTyped("");
    setFieldError(null);
    setStage({ kind: "minting" });
    setRound((count) => count + 1);
  }, []);

  /** The button: another code, and five more by themselves after it. */
  const showNew = () => {
    renewals.current = 0;
    renew();
  };

  /** A code that expired unclaimed: replaced, five times, then a button. */
  const expire = useCallback(() => {
    held.current = null;
    if (renewals.current < AUTO_RENEWALS) {
      renewals.current += 1;
      renew();
    } else {
      setStage({ kind: "expired" });
    }
  }, [renew]);

  const gone = useCallback((afterWrong: boolean) => {
    held.current = null;
    setStage({ kind: "gone", afterWrong });
  }, []);

  /** What a poll answered, applied to the pairing it was about. */
  const seen = useCallback(
    (id: string, view: PairingView) => {
      if (view.state === "approved") {
        held.current = null;
        setStage({ kind: "linked" });
        onLinked();
        return;
      }
      const state = view.state;
      setStage((was) =>
        was.kind === "shown" && was.pairing.id === id && was.state !== state
          ? { ...was, state }
          : was,
      );
      setTick((count) => count + 1);
    },
    [onLinked],
  );

  const shownId = stage.kind === "shown" ? stage.pairing.id : null;
  const shownState = stage.kind === "shown" ? stage.state : null;

  // Steps 2 and 5: every two seconds, while the dialog is open and the tab
  // is visible. Hidden, nothing is scheduled; visible again, it resumes.
  // Only the server's answer ends a code: a claim moves its expiry.
  useEffect(() => {
    if (shownId === null || !visible) return;
    let dropped = false;
    const timer = setTimeout(() => {
      pairingState(shownId).then(
        (view) => {
          if (!dropped) seen(shownId, view);
        },
        (failure: unknown) => {
          if (dropped) return;
          if (!isGone(failure)) {
            // A poll that did not arrive is not the end of the pairing.
            setTick((count) => count + 1);
          } else if (shownState === "waiting") {
            expire();
          } else {
            held.current = null;
            setStage({ kind: "expired" });
          }
        },
      );
    }, POLL_MS);
    return () => {
      dropped = true;
      clearTimeout(timer);
    };
  }, [shownId, shownState, visible, tick, expire, seen]);

  // Where the focus goes as the dialog moves on: the number field once a
  // phone has claimed, otherwise the one thing to press next.
  useEffect(() => {
    if (shownState === "claimed") field.current?.focus();
  }, [shownState]);
  useEffect(() => {
    if (stage.kind === "minting") self.current?.focus();
    else primary.current?.focus();
  }, [stage.kind]);

  // Escape closes, wherever the focus is.
  useEffect(() => {
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
    };
  }, [onClose]);

  /** Step 5's **Link**: the number, as the backend wants it. */
  const link = async (event: FormEvent) => {
    event.preventDefault();
    if (stage.kind !== "shown" || stage.state !== "claimed" || busy) return;
    const number = typedNumber(typed);
    if (number === null) {
      setFieldError(NOT_SIX);
      return;
    }
    const id = stage.pairing.id;
    const wrong = stage.wrong;
    setBusy(true);
    setFieldError(null);
    try {
      await approvePairing(id, number);
      held.current = null;
      setStage({ kind: "linked" });
      onLinked();
    } catch (failure) {
      if (isRefusal(failure, WRONG_NUMBER)) {
        setStage((was) =>
          was.kind === "shown" && was.pairing.id === id
            ? { ...was, wrong: was.wrong + 1 }
            : was,
        );
        setTyped("");
        field.current?.focus();
      } else if (isGone(failure)) {
        gone(wrong >= TRIES - 1);
      } else if (isRefusal(failure, CONFLICT)) {
        // Not claimed (any more): ask once what it is, and show that.
        pairingState(id).then(
          (view) => {
            seen(id, view);
          },
          (again: unknown) => {
            if (isGone(again)) gone(false);
            else setFieldError(detailOf(again));
          },
        );
      } else {
        setFieldError(detailOf(failure));
      }
    } finally {
      setBusy(false);
    }
  };

  /** Step 5's **No**: the pairing goes, and so does the dialog. */
  const refuse = async () => {
    if (stage.kind !== "shown" || busy) return;
    const id = stage.pairing.id;
    held.current = null;
    setBusy(true);
    try {
      await deletePairing(id);
    } catch {
      // Gone already, or not deleted: it expires in two minutes, and
      // nothing can be approved on it without the number.
    }
    onClose();
  };

  /** Tab goes round inside the dialog, never behind it. */
  const onKeyDown = (event: KeyboardEvent) => {
    if (event.key !== "Tab") return;
    const node = self.current;
    if (node === null) return;
    const stops = [...node.querySelectorAll<HTMLElement>(FOCUSABLE)];
    const first = stops[0];
    const last = stops.at(-1);
    if (first === undefined || last === undefined) return;
    const active = document.activeElement;
    if (event.shiftKey && (active === first || active === node)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  };

  const title =
    stage.kind === "fresh" ? "Sign in again to link a phone" : "Link a phone";

  return (
    <div className="devices-overlay">
      <div
        ref={self}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        onKeyDown={onKeyDown}
        className="devices-dialog"
      >
        <div className="devices-dialog-head">
          <h2 id={titleId} className="devices-dialog-title">
            {title}
          </h2>
          <button
            type="button"
            title="Close"
            aria-label="Close"
            onClick={onClose}
            className="devices-close"
          >
            <XIcon className="devices-icon" />
          </button>
        </div>

        {stage.kind === "shown" && (
          <figure className="devices-figure">
            <img
              src={qrSource(stage.pairing.qr_svg)}
              alt={ALT}
              className="devices-qr"
            />
            <figcaption className="devices-note">{SCAN}</figcaption>
          </figure>
        )}

        {/* Every state, said in words, and announced as it changes. */}
        <p aria-live="polite" className="devices-state">
          {sentence(stage)}
        </p>

        {stage.kind === "shown" && stage.state === "claimed" && (
          <form
            onSubmit={(event) => void link(event)}
            className="devices-number"
          >
            <label htmlFor={fieldId} className="devices-label">
              The number on the phone
            </label>
            <input
              ref={field}
              id={fieldId}
              value={typed}
              inputMode="numeric"
              autoComplete="one-time-code"
              placeholder="482 913"
              readOnly={busy}
              aria-invalid={fieldError !== null}
              aria-describedby={fieldError === null ? undefined : errorId}
              onChange={(event) => {
                setTyped(event.target.value);
              }}
              className="devices-input devices-digits"
            />
            {fieldError !== null && (
              <p id={errorId} role="alert" className="devices-refused">
                {fieldError}
              </p>
            )}
            <div className="devices-actions">
              <button
                type="submit"
                aria-disabled={busy}
                className="devices-button devices-primary"
              >
                Link
              </button>
              <button
                type="button"
                aria-disabled={busy}
                onClick={() => void refuse()}
                className="devices-button"
              >
                No
              </button>
            </div>
          </form>
        )}

        {stage.kind === "linked" && (
          <div className="devices-actions">
            <button
              ref={primary}
              type="button"
              onClick={onClose}
              className="devices-button devices-primary"
            >
              Done
            </button>
          </div>
        )}

        {(stage.kind === "expired" || stage.kind === "gone") && (
          <div className="devices-actions">
            <button
              ref={primary}
              type="button"
              onClick={showNew}
              className="devices-button devices-primary"
            >
              Show a new code
            </button>
          </div>
        )}

        {stage.kind === "refused" && stage.retry && (
          <div className="devices-actions">
            <button
              ref={primary}
              type="button"
              onClick={showNew}
              className="devices-button devices-primary"
            >
              Try again
            </button>
          </div>
        )}

        {stage.kind === "fresh" && (
          <FreshSignIn session={session} detail={stage.detail} />
        )}
      </div>
    </div>
  );
}

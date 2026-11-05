// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * "Linked devices", at `#/devices`: a page of the shell
 * (`docs/architecture/device-pairing.md`, "The interface").
 *
 * The person's phones, each with **Remove**, and below them their API
 * tokens, each with **Revoke**, and **New token**, which shows the secret
 * once. **Link a phone** opens the pairing dialog (`./LinkDialog.tsx`). This
 * page is the one place a credential is made or ended, because it runs on
 * the browser's session. Nothing of the chat is involved.
 *
 * A refusal is shown beside the control that failed, in the backend's own
 * sentence (`detailOf`), as everywhere else in the interface.
 */
import {
  type FormEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";

import { detailOf } from "../api/client";
import type { Session } from "../session/session";
import {
  type Device,
  listDevices,
  listTokens,
  type Listed,
  mintToken,
  needsFreshSignIn,
  type NewToken,
  platformName,
  removeDevice,
  revokeToken,
  shownDate,
  type Token,
  useListed,
} from "./devices";
import { FreshSignIn } from "./FreshSignIn";
import { LinkDialog } from "./LinkDialog";
import "./devices.css";

export const NO_PHONES = "No phone is linked.";
export const NO_TOKENS = "No tokens.";
export const SHOWN_ONCE =
  "Copy it now. It will not be shown again: only its name and dates are kept.";

/** An instant, in the browser's locale, with the instant itself on hover. */
function When({ instant }: Readonly<{ instant: string }>) {
  return (
    <time dateTime={instant} title={instant}>
      {shownDate(instant)}
    </time>
  );
}

export function DevicesPage({ session }: Readonly<{ session: Session }>) {
  const [devicesRound, setDevicesRound] = useState(0);
  const [tokensRound, setTokensRound] = useState(0);
  const devices = useListed(listDevices, devicesRound);
  const tokens = useListed(listTokens, tokensRound);
  const [linking, setLinking] = useState(false);
  const refreshDevices = useCallback(() => {
    setDevicesRound((count) => count + 1);
  }, []);
  const refreshTokens = useCallback(() => {
    setTokensRound((count) => count + 1);
  }, []);
  const closeDialog = useCallback(() => {
    setLinking(false);
  }, []);
  const phonesId = useId();
  const tokensId = useId();

  return (
    <div className="devices">
      <div className="devices-page">
        <h1 className="devices-title">Linked devices</h1>

        <section aria-labelledby={phonesId} className="devices-section">
          <div className="devices-head">
            <h2 id={phonesId} className="devices-heading">
              Phones
            </h2>
            <button
              type="button"
              onClick={() => {
                setLinking(true);
              }}
              className="devices-button devices-primary"
            >
              Link a phone
            </button>
          </div>
          <Items
            listed={devices}
            empty={NO_PHONES}
            label="Phones"
            what="the phones"
            row={(device) => (
              <DeviceRow
                key={device.id}
                device={device}
                onRemoved={refreshDevices}
              />
            )}
          />
        </section>

        <section aria-labelledby={tokensId} className="devices-section">
          <Tokens
            session={session}
            tokens={tokens}
            headingId={tokensId}
            onChanged={refreshTokens}
          />
        </section>
      </div>
      {linking && (
        <LinkDialog
          session={session}
          onClose={closeDialog}
          onLinked={refreshDevices}
        />
      )}
    </div>
  );
}

/** A list as it stands: loading, refused, empty, or its rows. */
function Items<T>({
  listed,
  empty,
  label,
  what,
  row,
}: Readonly<{
  listed: Listed<T>;
  empty: string;
  label: string;
  what: string;
  row: (item: T) => ReactNode;
}>) {
  if (listed.status === "loading") {
    return <p className="devices-note">Loading {what}…</p>;
  }
  if (listed.status === "failed") {
    return (
      <p role="alert" className="devices-refused">
        {`The list of ${what} could not be loaded: ${listed.detail}`}
      </p>
    );
  }
  if (listed.items.length === 0) {
    return <p className="devices-note">{empty}</p>;
  }
  return (
    <ul aria-label={label} className="devices-list">
      {listed.items.map(row)}
    </ul>
  );
}

/** One linked phone, and the way to end it. */
function DeviceRow({
  device,
  onRemoved,
}: Readonly<{ device: Device; onRemoved: () => void }>) {
  const [busy, setBusy] = useState(false);
  const [refused, setRefused] = useState<string | null>(null);
  const remove = async () => {
    if (busy) return;
    setBusy(true);
    setRefused(null);
    try {
      await removeDevice(device.id);
      onRemoved();
    } catch (failure) {
      setRefused(detailOf(failure));
    } finally {
      setBusy(false);
    }
  };
  return (
    <li className="devices-item">
      <div className="devices-what">
        <span className="devices-name">{device.name}</span>
        <span className="devices-kind">{platformName(device.platform)}</span>
      </div>
      <dl className="devices-dates">
        <div className="devices-date">
          <dt>Linked on</dt>
          <dd>
            <When instant={device.created_at} />
          </dd>
        </div>
        <div className="devices-date">
          <dt>Last used</dt>
          <dd>
            <When instant={device.last_used_at} />
          </dd>
        </div>
        <div className="devices-date">
          <dt>Expires</dt>
          <dd>
            <When instant={device.expires_at} />
          </dd>
        </div>
      </dl>
      <div className="devices-row-actions">
        <button
          type="button"
          aria-label={`Remove ${device.name}`}
          aria-disabled={busy}
          onClick={() => void remove()}
          className="devices-button"
        >
          Remove
        </button>
        {refused !== null && (
          <p role="alert" className="devices-refused">
            {`Not removed: ${refused}`}
          </p>
        )}
      </div>
    </li>
  );
}

/** One API token, and the way to end it. */
function TokenRow({
  token,
  onRevoked,
}: Readonly<{ token: Token; onRevoked: () => void }>) {
  const [busy, setBusy] = useState(false);
  const [refused, setRefused] = useState<string | null>(null);
  const revoke = async () => {
    if (busy) return;
    setBusy(true);
    setRefused(null);
    try {
      await revokeToken(token.id);
      onRevoked();
    } catch (failure) {
      setRefused(detailOf(failure));
    } finally {
      setBusy(false);
    }
  };
  return (
    <li className="devices-item">
      <div className="devices-what">
        <span className="devices-name">{token.name}</span>
      </div>
      <dl className="devices-dates">
        <div className="devices-date">
          <dt>Created</dt>
          <dd>
            <When instant={token.created_at} />
          </dd>
        </div>
        <div className="devices-date">
          <dt>Expires</dt>
          <dd>
            <When instant={token.expires_at} />
          </dd>
        </div>
      </dl>
      <div className="devices-row-actions">
        <button
          type="button"
          aria-label={`Revoke ${token.name}`}
          aria-disabled={busy}
          onClick={() => void revoke()}
          className="devices-button"
        >
          Revoke
        </button>
        {refused !== null && (
          <p role="alert" className="devices-refused">
            {`Not revoked: ${refused}`}
          </p>
        )}
      </div>
    </li>
  );
}

/** What the token section is doing beside its list. */
type Making =
  | { kind: "idle" }
  | { kind: "naming" }
  | { kind: "fresh"; detail: string }
  | { kind: "made"; token: NewToken };

/** The tokens, **New token**, and the secret shown once. */
function Tokens({
  session,
  tokens,
  headingId,
  onChanged,
}: Readonly<{
  session: Session;
  tokens: Listed<Token>;
  headingId: string;
  onChanged: () => void;
}>) {
  const [making, setMaking] = useState<Making>({ kind: "idle" });
  const opener = useRef<HTMLButtonElement>(null);
  const done = () => {
    setMaking({ kind: "idle" });
    opener.current?.focus();
  };
  return (
    <>
      <div className="devices-head">
        <h2 id={headingId} className="devices-heading">
          API tokens
        </h2>
        <button
          ref={opener}
          type="button"
          aria-expanded={making.kind === "naming"}
          aria-disabled={making.kind === "made"}
          onClick={() => {
            // The secret on the screen is shown once: Done ends it.
            if (making.kind !== "made") setMaking({ kind: "naming" });
          }}
          className="devices-button"
        >
          New token
        </button>
      </div>
      {making.kind === "naming" && (
        <NewTokenForm
          onMade={(token) => {
            setMaking({ kind: "made", token });
            onChanged();
          }}
          onFresh={(detail) => {
            setMaking({ kind: "fresh", detail });
          }}
          onCancel={done}
        />
      )}
      {making.kind === "fresh" && (
        <div className="devices-box">
          <h3 className="devices-box-title">Sign in again to make a token</h3>
          <FreshSignIn session={session} detail={making.detail} />
          <div className="devices-actions">
            <button type="button" onClick={done} className="devices-button">
              Cancel
            </button>
          </div>
        </div>
      )}
      {making.kind === "made" && <Secret token={making.token} onDone={done} />}
      <Items
        listed={tokens}
        empty={NO_TOKENS}
        label="API tokens"
        what="the tokens"
        row={(token) => (
          <TokenRow key={token.id} token={token} onRevoked={onChanged} />
        )}
      />
    </>
  );
}

/** Asks for a name, and mints. */
function NewTokenForm({
  onMade,
  onFresh,
  onCancel,
}: Readonly<{
  onMade: (token: NewToken) => void;
  onFresh: (detail: string) => void;
  onCancel: () => void;
}>) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [refused, setRefused] = useState<string | null>(null);
  const box = useRef<HTMLInputElement>(null);
  const boxId = useId();
  const errorId = useId();
  useEffect(() => {
    box.current?.focus();
  }, []);
  const make = async (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    const trimmed = name.trim();
    if (trimmed === "") {
      setRefused("A token needs a name.");
      return;
    }
    setBusy(true);
    setRefused(null);
    try {
      onMade(await mintToken(trimmed));
    } catch (failure) {
      if (needsFreshSignIn(failure)) onFresh(detailOf(failure));
      else setRefused(detailOf(failure));
    } finally {
      setBusy(false);
    }
  };
  return (
    <form
      onSubmit={(event) => void make(event)}
      onKeyDown={(event) => {
        if (event.key === "Escape") onCancel();
      }}
      className="devices-box"
    >
      <label htmlFor={boxId} className="devices-label">
        Name of the token
      </label>
      <input
        ref={box}
        id={boxId}
        value={name}
        readOnly={busy}
        aria-invalid={refused !== null}
        aria-describedby={refused === null ? undefined : errorId}
        onChange={(event) => {
          setName(event.target.value);
        }}
        className="devices-input"
      />
      {refused !== null && (
        <p id={errorId} role="alert" className="devices-refused">
          {refused}
        </p>
      )}
      <div className="devices-actions">
        <button
          type="submit"
          aria-disabled={busy}
          className="devices-button devices-primary"
        >
          Create
        </button>
        <button type="button" onClick={onCancel} className="devices-button">
          Cancel
        </button>
      </div>
    </form>
  );
}

/** How long a Copy says that it copied. */
const COPIED_MS = 2000;

/** The secret of a token just made, once, with a way to copy it. */
function Secret({
  token,
  onDone,
}: Readonly<{ token: NewToken; onDone: () => void }>) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const doneButton = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    doneButton.current?.focus();
    return () => {
      if (timer.current !== null) clearTimeout(timer.current);
    };
  }, []);
  const copy = () => {
    const clipboard =
      typeof navigator === "undefined" ? undefined : navigator.clipboard;
    // No clipboard here (an insecure origin): selecting the text still works.
    if (typeof clipboard?.writeText !== "function") return;
    clipboard.writeText(token.secret).then(
      () => {
        if (timer.current !== null) clearTimeout(timer.current);
        setCopied(true);
        timer.current = setTimeout(() => {
          timer.current = null;
          setCopied(false);
        }, COPIED_MS);
      },
      () => undefined,
    );
  };
  return (
    <section aria-label="The new token" className="devices-box">
      <h3 className="devices-box-title">{`The token "${token.name}"`}</h3>
      <p className="devices-note">{SHOWN_ONCE}</p>
      <code className="devices-secret">{token.secret}</code>
      <div className="devices-actions">
        <button
          type="button"
          onClick={copy}
          className="devices-button devices-primary"
        >
          {copied ? "Copied" : "Copy"}
        </button>
        <button
          ref={doneButton}
          type="button"
          onClick={onDone}
          className="devices-button"
        >
          Done
        </button>
      </div>
      <p aria-live="polite" className="devices-hidden-status">
        {copied ? "The token is on the clipboard." : ""}
      </p>
    </section>
  );
}

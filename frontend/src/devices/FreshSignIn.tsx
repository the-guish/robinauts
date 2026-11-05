// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * What a mint answered 403 `FreshSignInRequired` turns into: one way per
 * provider to sign in again, coming back to `#/devices`.
 *
 * Minting a pairing or a token wants a browser session younger than ten
 * minutes (`docs/architecture/device-pairing.md`, "Minting needs a fresh
 * sign-in"). The session's answer does not say how old it is, so nothing
 * here can know beforehand: the gate is met at the refusal. Each way in is
 * the sign-in page's own navigation (`../session/SignInPage.tsx`), a plain
 * link to `/auth/login/{provider}` with `return_to` set to this page.
 */
import type { Session } from "../session/session";
import { loginHref } from "../session/SignInPage";
import { DEVICES_HASH } from "./devices";

export function FreshSignIn({
  session,
  detail,
}: Readonly<{
  session: Session;
  /** The refusal's own sentence, for a deployment with no provider to offer. */
  detail: string;
}>) {
  const providers = session.sign_in ? (session.providers ?? []) : [];
  if (providers.length === 0) {
    return <p className="devices-refused">{detail}</p>;
  }
  return (
    <>
      <p className="devices-note">
        This needs a sign-in less than ten minutes old. You will come back to
        this page.
      </p>
      <ul className="devices-providers">
        {providers.map((provider) => (
          <li key={provider.id}>
            <a
              href={loginHref(provider.id, DEVICES_HASH)}
              className="devices-button devices-primary"
            >
              Sign in again with {provider.title}
            </a>
          </li>
        ))}
      </ul>
    </>
  );
}

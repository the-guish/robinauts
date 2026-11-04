// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The permanent strip of the local development mode.
 *
 * `docs/specs/sign-in.md`: the server warns at start-up and the interface
 * says so for as long as it is open. It is not dismissible, because what it
 * says stays true -- there is no sign-in and everybody is the one local
 * user -- and a banner that can be put away is a banner nobody sees.
 */
import "./shell.css";

export function LocalModeBanner({ local }: Readonly<{ local: boolean }>) {
  if (!local) return null;
  return (
    <p role="note" className="banner-local">
      Local development mode: sign-in is off and everything runs as one local
      user. For developing on your own machine, never for a deployment.
    </p>
  );
}

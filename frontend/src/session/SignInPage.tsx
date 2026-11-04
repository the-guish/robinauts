// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The page that stands in place of every page until somebody is signed in
 * (`docs/specs/sign-in.md`, `docs/specs/frontend.md`).
 *
 * One button per provider, and each one is a plain link: signing in is a
 * browser navigation to `/auth/login/{provider}`, which answers a redirect to
 * the provider. Nothing here is fetched, so nothing here needs a session.
 *
 * Text buttons, no provider logos: `docs/specs/sign-in.md`, and this project
 * ships no third-party logo at all.
 *
 * The design is neorc's, written again for this project; recorded in
 * `docs/legal/ip-clearance.md`.
 */
import type { Session } from "./session";
import "./session.css";

/**
 * What each code a failed sign-in comes back with means, for a reader.
 *
 * The keys are `domain.SignInErrorCode`, all eight of them. The set is
 * closed and the backend sends nothing else, but the sentences are for a
 * person and a code this interface does not know is still a failed sign-in:
 * an unknown one gets `GENERIC_ERROR` rather than nothing.
 *
 * **A `Map`, not an object.** The code comes out of the address bar, so
 * `?error=__proto__` and `?error=toString` are codes somebody can send, and
 * an object would answer them with something inherited -- an object or a
 * function -- which React then tries to render and throws on. A `Map` has
 * one key space, the one written here.
 */
export const SIGN_IN_ERRORS = new Map<string, string>([
  [
    "expired",
    "The sign-in took too long, or its link was used already. Try again.",
  ],
  [
    "state_mismatch",
    "The sign-in came back to a different browser, or another sign-in began since. Try again.",
  ],
  [
    "not_allowed",
    "The provider knows who you are, but this deployment does not let that account in. Ask whoever runs it.",
  ],
  ["unknown_provider", "That is not a provider this deployment signs in with."],
  ["busy", "Too many sign-ins are under way. Try again in a few minutes."],
  [
    "provider_unavailable",
    "The identity provider could not be reached. Try again shortly.",
  ],
  ["provider_refused", "The identity provider did not sign you in."],
  [
    "invalid_id_token",
    "The identity provider's answer could not be trusted, so you were not signed in.",
  ],
]);

export const GENERIC_ERROR = "Signing in failed. Try again.";

/** Longest `return_to` the backend will look at (`core.MAX_RETURN_TO`). */
const MAX_RETURN_TO = 512;

/** Where a person with nowhere to go back to lands. */
const HOME = "/";

/**
 * The code a failed sign-in came back with, out of the hash.
 *
 * The backend sends the browser to `/ui/#/sign-in?error=<code>`
 * (`api/auth_routes.py`), so the query is inside the fragment and
 * `location.search` is empty. Anything that is not that shape is no code.
 */
export function errorInHash(hash: string): string | null {
  const query = hash.indexOf("?");
  if (query === -1) return null;
  return new URLSearchParams(hash.slice(query + 1)).get("error");
}

/**
 * Where to come back to once signed in, as `return_to` for the login route.
 *
 * `core.safe_return_to` checks this again on arrival and once more before it
 * is used, so the browser cannot talk this deployment into redirecting
 * anywhere else. It is checked here too, because a target this page would
 * not have built is a target worth noticing before the round trip, and
 * because the sign-in page's own route is not somewhere to return to.
 *
 * The rule, the backend's: a path of this origin. One leading `/`, never
 * `//host` or `/\host`, which browsers read as another origin with the
 * scheme left out; no backslash, and printable ASCII only.
 */
export function returnTo(hash: string): string {
  if (!hash.startsWith("#/") || hash.startsWith("#/sign-in")) return HOME;
  // `#//x` would become `/#//x`. The backend takes what follows the `#` as
  // the route, so that is not a route of ours.
  if (hash.startsWith("#//")) return HOME;
  // A backslash anywhere: a separator to some readers of a URL and a
  // character to others, which is one target that is two.
  if (hash.includes("\\")) return HOME;
  if (hash.length + 1 > MAX_RETURN_TO) return HOME;
  for (const letter of hash) {
    if (letter < "!" || letter > "~") return HOME;
  }
  return `/${hash}`;
}

/** The navigation that begins a sign-in with one provider. */
export function loginHref(provider: string, hash: string): string {
  const target = encodeURIComponent(returnTo(hash));
  return `/auth/login/${encodeURIComponent(provider)}?return_to=${target}`;
}

export function SignInPage({ session }: Readonly<{ session: Session }>) {
  const providers = session.providers ?? [];
  const message = failure(errorInHash(location.hash));
  return (
    <main className="signin">
      <div className="signin-card">
        <h1 className="signin-title">Sign in to Robinauts</h1>
        {message !== null && (
          <p role="alert" className="signin-failed">
            {message}
          </p>
        )}
        {session.sign_in && providers.length > 0 ? (
          <ul className="signin-providers">
            {providers.map((provider) => (
              <li key={provider.id}>
                <a
                  className="signin-provider"
                  href={loginHref(provider.id, location.hash)}
                >
                  Sign in with {provider.title}
                </a>
              </li>
            ))}
          </ul>
        ) : (
          <p className="signin-none">
            This deployment has no sign-in configured. Whoever runs it starts it
            with a configuration file naming an identity provider; until then
            there is no way in.
          </p>
        )}
      </div>
    </main>
  );
}

function failure(code: string | null): string | null {
  if (code === null) return null;
  return SIGN_IN_ERRORS.get(code) ?? GENERIC_ERROR;
}

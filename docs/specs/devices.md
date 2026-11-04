# Linked devices and pairing

**Proposal, 2026-11-04. Nothing here is implemented yet.** The decision it
rests on is [ADR 0006](../adr/0006-mobile-sign-in-by-device-pairing.md). It
describes what the backend and the frontend need so that a phone — the
Android application first — gets in, is listed, and can be ended, the way
WhatsApp and Telegram link a device; and what that application has to do
against the API as it stands.

## A device

- A **device session** is the credential a channel other than the browser
  holds: a random 256-bit secret, sent as `Authorization: Bearer`, kept in
  the database as its SHA-256, as a browser session and an API token are
  ([sign-in.md](sign-in.md)).
- It belongs to one user and carries what its owner needs to recognise it:
  a **name** the device gave itself ("Pixel 8"), its **platform**
  ("android"), the **application's version**, when it was linked, when it
  was last used, when it expires.
- **Fixed life**, `device_days`, 30 by default, set by the operator beside
  `session_hours`. No renewal: a phone that reaches the end pairs again.
  Renewal while in use is a later option, if thirty days prove too short
  for what security teams accept.
- It ends when its owner removes it under "Linked devices", when the phone
  signs out (`POST /auth/logout` with its bearer), when it expires, when
  the operator clears it, and with the user (a cascade).
- It is **not an API token.** A token is for a script, is named by the
  person, lasts ninety days and is never listed as a device. The two are
  separate tables with one shape; "Linked devices" shows both.
- **A secret names its kind.** A device session's secret begins `rbd_`, an
  API token's `rbt_`; the prefix picks the table a bearer is looked up in,
  and lets a secret scanner recognise ours. A secret with no prefix is an
  API token minted before this, and keeps working until it expires.
- In the local development mode a device is linked to the mode's one user,
  as a token is minted for it. Like a session or a token naming that user,
  it signs nobody in where sign-in is on.

## Pairing

The browser is signed in; the phone is not. The browser shows a code, the
phone presents it, the browser confirms. Three secrets are involved, each
for one thing: the **pairing code** in the QR, the **claim secret** the
phone receives for presenting it, and the **device secret** the phone
keeps — which is the claim secret, promoted on approval, so that no secret
is ever stored in the clear and nothing is handed over after the approval.

### The flow

1. **The browser asks for a code.** `POST /auth/pairings`, with the session
   cookie. The server mints a pairing: an id, a 256-bit code, two minutes
   of life, owned by the signed-in user. It answers the id, the expiry and
   the QR picture, and never the code itself: the picture is what the
   browser shows, and its script has no business with the secret.
2. **The browser shows the QR** and polls `GET /auth/pairings/{id}` every
   two seconds. It says: "Scan this with the Robinauts app on your phone.
   Anyone who scans it can ask to be linked; you will be asked to confirm."
3. **The phone scans it.** The code is a URL of the deployment:

       {public_url}/#/link?v=1&c=<code>

   The app takes the origin as the server to talk to and `c` as the code.
   It accepts `https` only, with `http` on a loopback or private address
   for development builds. Scanned by a plain camera app, the URL opens the
   interface, whose `#/link` route says the code is for the app on a phone
   and **never uses it**; the code is in the fragment, which a browser does
   not send and no server or proxy logs.
4. **The phone presents it.** `POST /auth/pairings/claim`, no credential,
   body `{"code": …, "device": {"name": …, "platform": …,
   "app_version": …}}`. In one statement the server finds the pairing by
   the code's hash, requires it `waiting` and unexpired, records the
   device's description, mints a claim secret, stores its hash, and moves
   the pairing to `claimed`. It answers `{"secret": "rbd_…",
   "expires_at": …}`. A code that is unknown, spent or expired is 404, the
   same 404 for all three.
5. **The browser asks the person.** Its poll now says `claimed`, with the
   device's description, and it asks: "A device calling itself *Pixel 8*
   scanned your code. Is that your phone?" — the name rendered as text, as
   every outside string is. **Yes** is `POST /auth/pairings/{id}/approve`;
   **No** is `DELETE /auth/pairings/{id}`. Both are cookie writes and carry
   `Origin`, as every cookie write does.
6. **On approval** the server, in one statement, inserts a device session
   whose `secret_hash` is the pairing's claim hash, with `device_days` of
   life, and marks the pairing `approved`. The browser's poll says so and
   the device appears in the list. On refusal the pairing is deleted and
   the claim secret resolves to nothing, ever.
7. **The phone waits** by polling `GET /auth/session` with
   `Authorization: Bearer <secret>` every two seconds, until the pairing's
   expiry. That route is never a 401. While the pairing is `claimed` it
   answers `user: null` and `pairing: {"state": "claimed"}`; once approved
   it answers the user and the device, and the phone keeps the secret and
   is in; a pairing that is gone — refused, or expired — answers
   `user: null` and no `pairing`, and the phone says so and offers to scan
   again.

The code is spent by the first phone that presents it. A second phone with
a photograph of it gets the 404 of step 4; a first phone that is not the
person's gets refused at step 5, and received nothing it can use. In
neither case was anything issued without the person saying yes.

### Bounds

- A pairing lives **two minutes**. The browser asks for a fresh one when
  the previous expired unclaimed, five times, and then shows a button for
  the next; Telegram's thirty seconds and WhatsApp's twenty are tuned for
  services with a billion phones, and two minutes is enough to find one's
  phone without being long enough to matter.
- **Three pairings per user at once.** Asking for a fourth is 429, `busy`.
  Asking for one deletes the user's expired ones in the same statement, as
  beginning a sign-in deletes expired pending logins, so the table holds
  minutes and needs no sweep.
- A pairing that was approved stays, as `approved`, until it expires, so
  that both sides' polls can read the outcome; it grants nothing after the
  approval, since the device session is what resolves from then on.
- `device.name` is at most 64 characters of printable text, `platform` one
  of a short list (`android`, `ios`, `desktop`, `other`), `app_version` at
  most 32; anything else is 422.
- The claim is the one write a stranger can make. The code is 256 bits, so
  guessing is not a thing, and a claim creates nothing: at worst a stranger
  with a photograph spends a code the person then sees spent. A rate limit
  in front of the deployment — the operator's reverse proxy, as for
  sign-in — is the answer to volume, not a table cap.

### Routes

| route | who | purpose |
|---|---|---|
| `POST /auth/pairings` | cookie | mint; answers `{id, expires_at, qr_svg}`; 429 `busy` over the cap; 403 where `device_days` is 0 |
| `GET /auth/pairings/{id}` | cookie, owner | `{state, expires_at, device}`; 404 for another's, or one gone |
| `POST /auth/pairings/{id}/approve` | cookie, owner | links the device; answers it; 409 unless `claimed` |
| `DELETE /auth/pairings/{id}` | cookie, owner | refuse, or cancel; 204 |
| `POST /auth/pairings/claim` | nobody | present a code; answers `{secret, expires_at}`; 404 |
| `GET /auth/session` | bearer | gains `device` and `pairing`, below; never a 401 |
| `GET /auth/devices` | cookie or bearer | the person's devices, oldest first |
| `DELETE /auth/devices/{device_id}` | cookie or bearer | remove one of theirs; 404 for another's |
| `POST /auth/logout` | device bearer | ends this device, as it ends a browser's session |

`GET /auth/session` is today the cookie's question and ignores a bearer.
It answers for a bearer too: for a device session, the user and
`device: {id, name, platform, created_at, last_used_at, expires_at}`; for an
API token, the user and `token: {id, name, expires_at}`; for a bearer that
is a claimed pairing's, `user: null` and `pairing: {state: "claimed"}`;
for a bearer nobody knows, `user: null`, as for no cookie. The web ignores
the new fields.

The QR picture is rendered by the backend as SVG (`segno`, BSD-3-Clause,
pure Python, no dependencies; its own pull request under
[DEPENDENCIES.md](../../DEPENDENCIES.md)), and travels in the mint answer.
The browser shows it as `<img src="data:image/svg+xml;base64,…">`, which
the Content-Security-Policy's `img-src 'self' data:` already allows, and
never as markup. The alternative — a five-kilobyte MIT encoder in the
bundle, `uqr` or `qrcode-generator` — is a frontend dependency to vet and
carry for one picture, and would put the code in the browser's script.

### Records

Two tables, kept through the credentials port like the other three, in
`schema.sql` with its pin updated ([CONTRIBUTING.md](../../CONTRIBUTING.md),
"Changing the database schema").

    device_sessions
        id uuid primary key
        user_id uuid not null references users on delete cascade
        name text not null
        platform text not null
        app_version text
        secret_hash text not null unique, a 64-hex check
        created_at, last_used_at, expires_at timestamptz not null
        index (user_id); index (expires_at)

    pairings
        id uuid primary key
        user_id uuid not null references users on delete cascade
        code_hash text not null unique, a 64-hex check
        claim_hash text unique, a 64-hex check    -- set by the claim
        state text not null, one of waiting, claimed, approved
        device_name, device_platform, device_app_version text
        created_at, expires_at timestamptz not null
        index (user_id); index (expires_at)

`last_used_at` is written by the resolve that finds the device, at most
once a minute per device, in the same statement (an `UPDATE … RETURNING`
joined to `users`), so that the list can say "last used" without a write
on every request.

The credentials port grows accordingly: `add_pairing`, `claim_pairing`,
`pairing_of(user_id, pairing_id)`, `approve_pairing`, `delete_pairing`,
`resolve_claim`, `resolve_device_session`, `device_sessions_of`,
`delete_device_session`; each one statement, each given `now`, each with
its row in the contract suite that the memory and the PostgreSQL adapters
both pass. The flow itself — minting, claiming, approving, with the same
`now` discipline as `SignIn` — is a module beside `web/sign_in.py`.

### Configuration

    device_days = 30

One key, sign-in's, beside `session_hours`. `0` means no devices: the
pairing routes answer 403 and "Linked devices" shows tokens alone, for a
deployment whose policy allows no phones. Absent, 30. A file that names it
is a sign-in configuration, and is refused in the local development mode
like the rest of them; the mode runs on the default.

## The interface

- **"Linked devices"** is a page of the shell at `#/devices`, reached from
  the profile block at the bottom of the panel, beside sign-out. It lists
  the person's devices — name, platform, linked on, last used, expires —
  each with **Remove**, and below them their API tokens, each with
  **Revoke**, which is the screen the token routes never had. Nothing of
  the chat library is involved: the panel and the shell are ours.
- **"Link a phone"** opens a dialog that asks for a code and shows the QR,
  with the sentence of step 2 and the state in words beneath it: *waiting*;
  *a device calling itself … scanned your code — is that your phone?* with
  **Yes, link it** and **No**; *linked*; *the code expired* with **Show a
  new code**. Polling runs while the dialog is open and stops when it
  closes or the tab is hidden; closing it while a code waits deletes the
  pairing.
- **`#/link`** is a route that explains and does nothing: "This code is for
  the Robinauts app on a phone. Open the app and scan it there." It reads
  no parameter.
- The QR has alternative text; every state is said in text; no colour
  carries meaning. Nothing new is installed: the picture comes from the
  server, and the polling is `fetch` on a timer through the typed client.
- The session store (`src/session/session.ts`) is untouched: `/auth/session`
  gains fields the web does not read.

## The Android application

What it needs of the API as it stands, so that the app is written against
the same contract as the web; the app itself is a repository of its own.

- **One origin, from the code.** The app has no server address until it
  scans one, and talks to that origin alone. Release builds speak `https`
  only.
- **Headers.** `Authorization: Bearer rbd_…` on every call;
  `Accept: application/json`; `Content-Type: application/json` on every
  write, body or none, which is the rule of [sign-in.md](sign-in.md) and
  what the web's client sends; the backend does not refuse a write without
  it yet, and a client that sends it is ready for the day it does. No
  cookies, and no `Origin`: a bearer write is not subject to the origin
  check ([sign-in.md](sign-in.md)).
- **Start-up.** `GET /auth/session`. A user: carry on. No user: the device
  was removed or expired; forget the secret and show the pairing screen.
  The same for any `401` later.
- **The secret** lives in storage the operating system keeps for the app
  alone, under a key the Android Keystore holds, and is **excluded from
  backups**: a backup restored on another phone would otherwise clone the
  device, and the list would show one device that is two.
- **Streams.** A turn's answer is server-sent events over the same bearer,
  read with an HTTP client that streams the body (OkHttp or Ktor; there is
  no `EventSource` to lean on, and none is needed since a header can be
  set). The turn runs on the server whatever happens to the stream, so an
  app sent to the background, or a network that changes, loses nothing: it
  re-attaches with `Last-Event-ID`, using the ids and the two
  `X-Robinauts-*` headers of [wire.md](wire.md). A comment goes out every
  fifteen seconds while nothing else does. The "without streaming" way of
  consuming a turn that wire.md plans is the one that would matter most on
  a phone.
- **The contract** is `backend/openapi.json`, from which a Kotlin client
  is generated (openapi-generator is Apache-2.0) or written by hand; the
  streaming routes are outside the document and are specified in
  [wire.md](wire.md).
- **Scanning** is ZXing (Apache-2.0) over CameraX (Apache-2.0). Not ML Kit:
  it is proprietary, downloads models, and reports to Google, which the
  dependency rules and goal 3 both refuse. The app has the same licence
  gate as the backend and the frontend, from its first commit.
- **Push notifications** are not part of this. Firebase Cloud Messaging
  sends through Google, which is a decision against "nothing phones home"
  to take on its own, if ever, and with content-free messages if so.
- **Verified App Links**, so that a code scanned by a camera app opens the
  app directly, need every deployment to serve
  `/.well-known/assetlinks.json` naming the app's package and signing key.
  A configuration key for the backend to serve one is a later addition;
  the in-app scanner is the path that matters.

## What it defends against

- **A photographed or shared code.** Spent by the first claim; two minutes
  of life; and nothing issued until the person approves a device they
  recognise.
- **A stranger's code shown to the person**, the attack the other
  direction invites (a victim scanning an attacker's code signs the
  attacker in). Not possible here: scanning gives the one who showed the
  code nothing; it is the scanner who receives, and only after the one who
  showed it approves.
- **Two claims racing.** One statement moves `waiting` to `claimed`; the
  second finds it moved.
- **Secrets at rest.** Hashes only, in every table, as today; the claim
  secret becomes the device's so that no secret waits in the clear to be
  collected.
- **Secrets in logs.** The code travels in a fragment and a JSON body,
  never in a path or a query; the secret in a bearer, which is never
  logged.
- **Cross-site requests.** Cookie writes carry `Origin`; the claim has no
  cookie; a bearer write is exempt as today, and a browser cannot send a
  bearer it does not hold.
- **The device's name** is text the phone chose, bounded and rendered as
  data.
- **A code on a shared screen.** The sentence under the QR says what a
  scan can do, and the approval is the control.

## What it prepares for

- **The browser hand-off**, for a person with only their phone: the app
  opens the system browser on `/auth/login/{provider}` with a channel
  parameter, the callback redirects to the app with a one-time code, the
  app exchanges it at `POST /auth/devices/claim` for a device session. The
  same table, the same list.
- **The other direction**, a browser signed in by scanning with a phone,
  for a shared screen or a kiosk: the same pairing record with the roles
  swapped — minted with no owner, approved by a device bearer, promoted to
  a browser session — and a confirmation screen on the phone, which is
  where the attack above returns.
- **A typed code** beside the QR, where no camera can see the screen; it
  needs a rate limit and is deferred.
- **An operator's view**: every device of every user, and "remove all of
  a person's", with the admin screens ([privacy.md](privacy.md)).
- **The audit log** gains device linked, device removed, pairing refused,
  when it exists; until then they are log lines, as sign-ins are.

## The work, in order

Each step is a pull request or two; the dependency is its own.

1. **The API answers a bearer whole.** `GET /auth/session` resolves a
   bearer; new secrets carry their kind's prefix; `current_user` picks
   the table by it. The OpenAPI document is refreshed and the frontend's
   types with it. Fast tests beside `tests/fast/test_web_tokens.py`.
2. **Devices.** `device_sessions` in `schema.sql` and its pin; the
   records, the port, both adapters and the contract suite; `device_days`
   in `web/sign_in.py`; `GET /auth/devices`, `DELETE /auth/devices/{id}`,
   `POST /auth/logout` with a bearer; "Linked devices" in the frontend,
   tokens included, with its route and its entry in the profile block.
3. **`segno`**, on its own, under DEPENDENCIES.md.
4. **Pairing.** `pairings` and its pin; the port and the adapters; the flow
   module; the five routes; the dialog, the polling and `#/link`; a fast
   test of the whole flow on the memory credentials with a pretend phone;
   an API test that the claim's answers carry nothing but what this
   document says; and an end-to-end journey in which Playwright links a
   phone played by `httpx` against the same server, the phone reads a
   conversation, the browser removes it, and the phone is out.
5. **The application**, against the contract above.

## Open

- Thirty days, and no renewal, as the default life: enough for people and
  acceptable to security teams, or neither.
- Whether a refused sign-in — a person the allow list no longer lets in —
  should end their devices and tokens there and then, which would close
  most of the gap the fixed life opens.
- Whether `device_days = 0` is the right spelling of "no phones", or a
  key of its own.
- `platform` as a closed list, or free text bounded like the name.

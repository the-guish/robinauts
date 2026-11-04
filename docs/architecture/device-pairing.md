# Device pairing

How a phone gets in, and how its access is listed and ended. The Android
application is the first phone. Everything here is served by the web shell
over the credentials port (`web.md`, `docs/specs/sign-in.md`).

The idea is the one WhatsApp and Telegram use to link a device. One device
shows a QR code. The other device scans it. The one that is signed in
consents. The new device is then listed, and can be removed at any time.

In those products the phone is the signed-in side. Here it is the browser.
The account lives with the company's identity provider, and a browser is
what signs in with it. So the browser shows the code, and the phone scans
it. The consent is a number: the phone shows one, and the person types it
into the browser. That is number matching, as Bluetooth pairing and
sign-in prompts do it. A Yes is impossible without the phone in hand.

The code also names the server. Every company runs its own Robinauts, so
the app learns where to connect from the code, and nobody types a server
address into a phone.

## A device

- A device holds a **device session**: a random 256-bit secret, sent as
  `Authorization: Bearer`. The database holds its SHA-256, never the
  secret, as it does for a browser session and an API token.
- A device session belongs to one user. It records the name the device
  gave itself ("Pixel 8"), its platform, the application's version, when
  it was linked, when it was last used, and when it expires.
- It lives `device_days`, 30 by default. It is not renewed. A phone that
  reaches the end pairs again.
- It ends when its owner removes it under "Linked devices", from a
  browser, when the phone signs out, when it expires, when the operator
  clears it, or with the user.
- It is not an API token. A token is for a script. A person names it, it
  lasts ninety days, and it is never shown as a device. The two are
  separate tables with one shape. "Linked devices" shows both.
- A secret names its kind. A device session's secret starts with `rbd_`.
  An API token's starts with `rbt_`. The prefix picks the table a bearer is
  looked up in, and lets a secret scanner recognise ours. A secret with no
  prefix is an older API token, and works until it expires.
- In the local development mode a device is linked to the mode's one user,
  as a token is minted for it, and the code names the origin the request
  came to, `http://{Host}`. Like a session or a token that names that
  user, it signs nobody in where sign-in is on.

## What a bearer may do

- A bearer reads and writes what its owner owns: every route under
  `/api/`, and the lists of devices and tokens. A device ends itself with
  `POST /auth/logout`. A token does not end itself. Its owner revokes it
  in the browser.
- **A bearer never mints a credential, and never removes one.**
  `POST /auth/pairings`, `POST /auth/pairings/{id}/approve`,
  `POST /auth/tokens`, `DELETE /auth/devices/{device_id}` and
  `DELETE /auth/tokens/{token_id}` need the browser's session cookie. A
  bearer that asks is 403.
- The reason is persistence. A credential that mints credentials outlives
  its removal. A stolen 30-day device secret would mint a 90-day API
  token, and removing the phone from the list would achieve nothing. With
  the rule, removing the phone ends everything the phone had. The same
  rule stops a token minting a token. Only a person at a browser, with a
  session from the identity provider, creates or removes a credential.
- The shell knows how the asker was named: by the session cookie, by a
  device secret, or by a token. The routes above take the stricter
  dependency, `browsing`, where the others take `asking`; the two that
  mint take `browsing_fresh`, which also wants the session fresh. So do
  `GET` and `DELETE /auth/pairings/{id}`: they are the browser's half of
  the flow. In the local
  development mode every request is the mode's one user, and counts as
  the browser's.

## While a device lives

- **The allow list is checked again when a device is used.** The user
  record keeps what the last sign-in said: provider, subject, verified
  email, hosted domain and groups, refreshed at every sign-in. A device's
  resolve checks that against the allow list as it is now, on a
  sixty-second cache per credential, kept in each server process. A check that fails **deletes**: the
  credential that asked, and every browser session, device and token of
  that user with it, keyed by user id. A refusal that left the secret in
  place would have it working again the moment the matcher is put back.
  So a person the operator takes out of the allow list loses every
  credential within a minute. The same check runs for a token and for a
  browser session.
- **A refused sign-in ends every credential of that identity.** When the
  allow list refuses someone the provider has just vouched for, their
  browser sessions, devices and tokens are deleted in the same breath,
  keyed by provider and subject. Only the allow list's refusal does this.
  A provider that is down, a state mismatch or an unreadable token wipes
  nothing. That is the moment the platform learns the person is out.
- **Minting needs a fresh sign-in.** `POST /auth/pairings` and
  `POST /auth/tokens` require a browser session younger than ten minutes:
  fresh is the session's own age, whatever the provider did. Any other
  session is answered 403, `FreshSignInRequired`, and the page offers to
  sign in again, coming back to `#/devices`. That sign-in is the sign-in
  page's own navigation, `/auth/login/{provider}` with `return_to`. A
  copied cookie cannot pass: signing in again needs the provider's
  session, which is not on the thief's machine. A script or malware
  driving the signed-in browser is not stopped by this gate: it can sign
  in again through the provider's own session in that browser, and get a
  fresh session. Removing a device needs no fresh sign-in. Taking access
  away is never gated.
- **The operator ends devices from the command line.**
  `robinauts devices end --user <email>`, `--device <id>` or `--all`, and
  `robinauts devices list`. They read the database the server reads, and
  need no browser and no admin account: for an incident, or a deployment
  where nobody is an admin yet.

## The flow

Three secrets and one number take part. Each does one thing. The
**pairing code** is in the QR. The **claim secret** is what the phone gets
for presenting the code. The **device secret** is what the phone keeps. The
device secret is the claim secret, promoted when the browser approves. So
no secret is ever stored in the clear, and nothing is handed over after the
approval. The **number** is six digits, minted with the claim, shown on the
phone and typed into the browser. It proves that the phone that claimed
and the person at the browser are the same hands.

```mermaid
sequenceDiagram
    participant B as Browser, signed in
    participant S as Server
    participant P as Phone, the app

    B->>S: POST /auth/pairings, with the cookie
    S-->>B: id, expires_at, the QR picture
    Note over B: shows the QR
    loop every 2 s, until claimed or expired
        B->>S: GET /auth/pairings/{id}
        S-->>B: state: waiting
    end
    Note over B,P: the person scans the QR with the app
    Note over P: "Set up Robinauts at robinauts.example.com?" Yes
    P->>S: POST /auth/pairings/claim, with the code and a name
    Note over S: code spent, pairing claimed, number minted
    S-->>P: secret, inert until approved, and the number
    Note over P: shows the number, 482 913
    B->>S: GET /auth/pairings/{id}
    S-->>B: state: claimed
    Note over B: "Type the number the phone shows"
    loop every 2 s, until approved or expired
        P->>S: GET /auth/session, Bearer secret
        S-->>P: user: null, pairing: claimed
    end
    alt the person types 482 913
        B->>S: POST /auth/pairings/{id}/approve, with the number
        Note over S: the number matches, device session from the claim's hash
        S-->>B: the device
        P->>S: GET /auth/session, Bearer secret
        S-->>P: user, device
        Note over P: keeps the secret and is in
    else No, or a third wrong number
        B->>S: DELETE /auth/pairings/{id}, or the third wrong number
        S-->>B: 204, or 404
        P->>S: GET /auth/session, Bearer secret
        S-->>P: user: null, no pairing
        Note over P: offers to scan again
    end
```

1. **The browser asks for a code.** `POST /auth/pairings`, with the session
   cookie. The server mints a pairing: an id, a 256-bit code, two minutes
   of life, owned by the signed-in user. It answers the id, the expiry and
   the QR picture. It answers the code only as the picture, which is all
   the browser needs.
2. **The browser shows the QR.** It polls `GET /auth/pairings/{id}` every
   two seconds. Under the picture it says: "Scan this with the Robinauts
   app on your phone. The app will show you a number to type here."
3. **The phone scans it.** The code is a URL of the deployment:

       {public_url}/#/link?v=1&c=<code>

   The app takes the origin as the server to talk to, and `c` as the code.
   Before it sends anything, it shows the origin exactly as it will use
   it, an internationalised name in punycode, and asks: "Set up Robinauts
   at robinauts.example.com?" Only after a Yes does it present the code.
   It accepts `https` only. The exception for `http` on a loopback or a
   private address exists in development builds and is compiled out of
   release builds; there is no switch. Where the company configures the
   app with its server, the origin in the code has to equal the configured
   one, and any other is refused before the question is asked. A plain
   camera app opens the URL in a browser. The interface's `#/link` route
   then says the code is for the app on a phone, and does not use it. The
   code is in the fragment, so a browser does not send it, and no server
   or proxy logs it.
4. **The phone presents it.** `POST /auth/pairings/claim`, with no
   credential. The body is `{"code": …, "device": {"name": …,
   "platform": …, "app_version": …}}`. In one statement the server finds
   the pairing by the code's hash, requires it to be `waiting` and not
   expired, records the device's description, mints a claim secret and a
   six-digit number, stores the secret's hash and the number, gives the
   pairing a fresh two minutes, and moves it to `claimed`. It answers
   `{"secret": "rbd_…", "number": "482913", "expires_at": …}`. A code
   that is unknown, spent or expired is 404. The three cases get the same
   404. The phone shows the number, large, and says: type this number in
   your browser.
5. **The browser asks for the number.** Its poll now says `claimed`. It
   says: "A phone scanned your code. Type the number it shows." It offers
   a field for six digits, **Link**, and **No**. It does not show the name
   the phone gave itself. That name is information for the list, not what
   the person decides by. **Link** is `POST /auth/pairings/{id}/approve`
   with `{"number": "482913"}`. **No** is `DELETE /auth/pairings/{id}`.
   Both are cookie writes, and carry `Origin` like every cookie write.
6. **On the right number** the server, in one statement, inserts a device
   session whose `secret_hash` is the pairing's claim hash, with
   `device_days` of life, clears `claim_hash` on the pairing, so the hash
   moves rather than copies, and marks the pairing `approved`. The same
   statement checks the expiry and counts a wrong try, so two approves at
   once cannot both pass, and a wrong try is never lost. The browser's
   poll says so, and the device appears in the list. A wrong number is
   403, `WrongNumber`, and the browser says so and asks again. The third
   wrong number deletes the pairing, as **No** does, and is answered 404,
   as a pairing that is gone is. The claim secret then resolves to
   nothing, ever. A number that is not six digits is 422, and is not
   counted.
7. **The phone waits.** It polls `GET /auth/session` with
   `Authorization: Bearer <secret>` every two seconds, until the pairing's
   expiry. That route is never a 401. While the pairing is `claimed`, it
   answers `user: null` and `pairing: {"state": "claimed"}`. Once approved,
   it answers the user and the device. The phone keeps the secret and is
   in. A pairing that is gone, refused or expired, answers `user: null` and
   `pairing: null`. The phone says so and offers to scan again.

The code is spent by the first phone that presents it. A second phone with
a photograph of it gets the 404 of step 4. A first phone that is not the
person's gets a number the person never sees. The person's own phone
failed to claim, so it shows nothing to type, and nothing can be approved.
The person presses **No**, or lets the two minutes pass, and asks for a
new code. In every case nothing is issued without a number that only the
claiming phone shows.

The number goes the one way an attacker cannot follow. It appears on the
phone, which an attacker watching the screen does not see. It is typed
into the browser, which an attacker does not hold. Shown the other way
round, on the browser and typed on the phone, a script that claimed first
would read it off the shared screen and type it itself.

## Bounds

- A pairing lives **two minutes** from minting, and two minutes again from
  the claim, so there is time to type. When one expires unclaimed, the
  browser asks for a fresh one, five times. After that it shows a button
  for the next. Telegram rotates every thirty seconds and WhatsApp every
  twenty. Those numbers suit a service with a billion phones. Two minutes
  is enough to find one's phone, and short enough not to matter.
- **Three pairings per user at once.** A fourth is 429, `Busy`. An
  approved one counts until it expires. Asking for one deletes the user's
  expired ones in the same statement, as beginning a sign-in deletes
  expired pending logins. The table holds minutes and needs no sweep. The
  cap bounds the table: two mints at the same instant may both pass it.
- **The number** is six digits, from a random source, shown with a space
  in the middle and typed with or without it. Nobody guesses it from
  outside: only the signed-in browser can send it, and three wrong numbers
  end the pairing. It is not a secret the way the three secrets are. It is
  stored as it is: a hash of six digits hides nothing, and the row lives
  two minutes.
- An approved pairing stays, as `approved`, until it expires. Both sides'
  polls can read the outcome. It grants nothing after the approval. From
  then on the device session is what resolves.
- `device.name` is 1 to 64 characters of printable text. `platform` is
  one of `android`, `ios`, `desktop`, `other`. `app_version` is optional,
  at most 32 characters of printable text. `code` is at most 128
  characters. Anything else is 422.
- The claim is the one write a stranger can make. The code is 256 bits, so
  nobody guesses one. A claim creates nothing that resolves. At worst a
  stranger spends a code. The person's phone then has no number to show,
  the person asks for a new code, and the stranger holds a secret that
  never resolves. Volume is the job of a rate limit in front of the
  deployment, the operator's reverse proxy, as for sign-in.

## Routes

| route | who | purpose |
|---|---|---|
| `POST /auth/pairings` | fresh cookie | mint; 201 `{id, expires_at, qr_svg}`; 429 `Busy` over the cap; 403 `FreshSignInRequired` on a session older than ten minutes; 403 `Forbidden` where `device_days` is 0 |
| `GET /auth/pairings/{id}` | cookie, owner | `{state, expires_at, device}`, `device` being `{name, platform, app_version}` once claimed; 404 for another's, or one gone or expired |
| `POST /auth/pairings/{id}/approve` | cookie, owner | body `{number}`; link the device and answer it; 403 `WrongNumber`; 404 for the third wrong one, and one gone; 409 `Conflict` unless `claimed`; 403 where `device_days` is 0 |
| `DELETE /auth/pairings/{id}` | cookie, owner | refuse, or cancel; 204; on an approved one, 204, the row goes and the device stays |
| `POST /auth/pairings/claim` | nobody | present a code; answers `{secret, number, expires_at}`; 404 |
| `GET /auth/session` | bearer | also answers `device`, `token` or `pairing`; never a 401 |
| `GET /auth/devices` | cookie or bearer | the person's devices, oldest first |
| `DELETE /auth/devices/{device_id}` | cookie | remove one of theirs; 404 for another's; 403 for a bearer |
| `POST /auth/logout` | device bearer | end this device, as it ends a browser's session; 204, and with a token's bearer 204 and nothing ended |

The API token routes follow the same rule. `POST /auth/tokens` and
`DELETE /auth/tokens/{token_id}` are the browser's, and answer a bearer
403. `POST /auth/tokens` also wants the session younger than ten minutes.
`GET /auth/tokens` answers a bearer too.

`GET /auth/session` answers for a bearer as it does for a cookie. For a
device session it answers the user and
`device: {id, name, platform, created_at, last_used_at, expires_at}`. For
an API token it answers the user and `token: {id, name, expires_at}`. For
the bearer of a claimed pairing it answers `user: null` and
`pairing: {state: "claimed"}`. For a bearer nobody knows it answers
`user: null`, as for no cookie. A field that does not apply is `null`. The
web ignores the new fields.

The QR picture is rendered by the backend as SVG. `qrcodegen` (MIT, pure
Python, no dependencies) computes the modules, and the SVG is ours: one
path of the dark modules, a quiet zone of four, no script and no external
reference. It travels in the mint answer, as SVG text. The browser shows it as `<img src="data:image/svg+xml;base64,…">`.
The Content-Security-Policy's `img-src 'self' data:` allows that. It is
never put in the page as markup. The frontend carries no QR library. The
code reaches the browser inside the picture, and a script in the page
could decode it. Nothing needs to stop that: a script in a signed-in page
can mint a pairing of its own anyway.

## Records

Two tables, kept through the credentials port like the other three
(`data-model.md`). They are in `schema.sql`, with its pin.

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
        match_number text, a six-digit check        -- set by the claim
        wrong_tries smallint not null
        state text not null, one of waiting, claimed, approved
        device_name, device_platform, device_app_version text
        created_at, expires_at timestamptz not null
        index (user_id); index (expires_at)

`last_used_at` is written by the resolve that finds the device, at most
once a minute per device, in the same statement: an `UPDATE … RETURNING`
joined to `users`. The list says "last used" without a write on every
request. The same join brings the user's `hosted_domain` and `groups`,
two columns `users` gains for the allow-list check above, refreshed by
`ensure_user` at every sign-in beside the name and the email.

The credentials port has one operation per step: `add_pairing`,
`claim_pairing`, `pairing_of(user_id, pairing_id)`, `approve_pairing`,
`delete_pairing`, `resolve_claim`, `add_device_session`,
`resolve_device_session`, `device_sessions_of`, `delete_device_session`;
for the operator, `all_device_sessions`, `users_with_email` and
`delete_device_sessions`; and for the allow list, `delete_credentials_of`
and `delete_credentials_of_identity`, which delete a user's sessions,
devices, tokens and pairings at once. Each is one statement. Each that
judges an expiry is given `now`; `pairing_of` is not, and the shell judges
the expiry it answers. `approve_pairing` is given the number, and answers
one of approved, wrong number, not claimed (waiting still, or approved
already), or gone; the third wrong number deletes the pairing and answers
gone. A cookie's resolve answers the session beside the user, since its
`created_at` is what fresh means. Each has its
row in the contract suite, which the memory and the PostgreSQL adapters
both pass, and so do the details above: the expiry and the wrong try in
the approve, the hash that moves, the delete of an approved pairing, and
the wipe that only an allow-list refusal triggers. The flow lives in `web/pairing.py`, beside `web/sign_in.py`,
with the same `now` discipline.

## Configuration

    device_days = 30

One key of sign-in's, beside `session_hours`. `0` means no devices: minting
and approving a pairing answer 403, so no pairing exists for the other
routes to find, and "Linked devices" shows tokens alone. Anything but a
number 0 or more is refused at start-up. That is
for a deployment whose policy allows no phones. Absent, it is 30. A file
that names it is a sign-in configuration. The local development mode
refuses it, like the other sign-in keys, and runs on the default.

## The interface

- **"Linked devices"** is a page of the shell at `#/devices`. The profile
  block at the bottom of the panel links to it, beside sign-out. It lists
  the person's devices: name, platform, linked on, last used, expires. Each
  has **Remove**. Below them it lists their API tokens, each with
  **Revoke**, and a **New token** that asks for a name and shows the
  secret once. This page is the one place a credential is made or ended,
  because it runs on the browser's session. Nothing of the chat library is
  involved. The panel and the shell are ours.
- **"Link a phone"** and **"New token"** want a session younger than ten
  minutes. Without one the button reads "Sign in again to link a phone",
  and pressing it is the sign-in page's own navigation, with `return_to`
  set to this page. "Link a phone" then opens a dialog. It
  asks for a code and shows the QR, with the sentence of step 2. The state is written under it: *waiting*;
  *a phone scanned your code, type the number it shows* with a six-digit
  field, **Link** and **No**; *that is not the number, two tries left*;
  *linked*; *the code expired* with **Show a new code**. Polling runs while
  the dialog is open. It stops when the dialog closes or the tab is hidden.
  Closing the dialog while a code waits deletes the pairing.
- **"Devices", for admins**, is one of the admin screens. It lists every
  linked device and every API token of the instance, by person: name,
  platform, linked on, last used, expires. An admin ends one device, every
  device of one person, or every device of the instance, with one action
  each. An ended device gets 401 on its next request, and the phone
  returns to its pairing screen. The admin sees names and dates, never a
  secret and never a conversation (`docs/specs/privacy.md`). Each action
  is logged.
- **`#/link`** explains and does nothing: "This code is for the Robinauts
  app on a phone. Open the app and scan it there." It reads no parameter.
- The QR has alternative text. Every state is said in words. No colour
  carries meaning. The picture comes from the server, and the polling is
  `fetch` on a timer through the typed client. Nothing new is installed.
- The session store, `src/session/session.ts`, is unchanged.
  `/auth/session` gains fields the web does not read.

## The Android application

The app is a repository of its own. This is what it does against the API.

- **One origin, from the code.** The app has no server address until it
  scans one. It shows that origin and asks before it sends a byte, as
  step 3 says. The secret is stored with the origin it came from, and is
  sent to that origin and no other. A later code that names another
  origin does not move the secret. It starts a new pairing, and the app
  says it is leaving the first server. Release builds speak `https` only,
  and carry no exception.
- **The number.** After the claim the app shows the number, large, with
  two sentences: "Type this number in your browser. Do not read it to
  anyone." It keeps the screen on and polls until the browser does, or
  the pairing expires. It does not ask the person to type anything
  itself. Once in, its first screen names the account: the user's name
  and email, as `GET /auth/session` answered them.
- **Headers.** `Authorization: Bearer rbd_…` on every call.
  `Accept: application/json`. `Content-Type: application/json` on every
  write, with a body or without. That is the rule of
  `docs/specs/sign-in.md`, and what the web's client sends. No cookies and
  no `Origin`: a bearer write is not subject to the origin check.
- **Start-up.** `GET /auth/session`. With a user, the app carries on. With
  none, the device was removed or expired. The app forgets the secret and
  shows the pairing screen. Any `401` later means the same.
- **No credential management.** The app can show the person's devices and
  tokens, and can sign itself out. It cannot mint a token, start a
  pairing, or remove another device. It sends the person to the browser
  for that, and says so.
- **The secret** lives in storage the operating system keeps for the app
  alone, under a key the Android Keystore holds. It is excluded from
  backups. A backup restored on another phone would otherwise clone the
  device, and the list would show one device that is two.
- **Streams.** A turn's answer is server-sent events, over the same bearer.
  The app reads them with an HTTP client that streams the body, OkHttp or
  Ktor. There is no `EventSource` on a phone, and none is needed, since a
  header can be set. The turn runs on the server whatever happens to the
  stream. An app sent to the background, or a network that changes, loses
  nothing. It re-attaches with `Last-Event-ID`, using the ids and the two
  `X-Robinauts-*` headers of `docs/specs/wire.md`. A comment goes out every
  fifteen seconds while nothing else does.
- **The contract** is `backend/openapi.json`. A Kotlin client is generated
  from it with openapi-generator (Apache-2.0), or written by hand. The
  streaming routes are outside the document, and are in
  `docs/specs/wire.md`.
- **Scanning** is ZXing (Apache-2.0) over CameraX (Apache-2.0). Not ML Kit:
  it is proprietary, it downloads models, and it reports to Google. The
  dependency rules and goal 3 both refuse that. The app has the same
  licence gate as the backend and the frontend, from its first commit.
- **Push notifications** are not part of this. Firebase Cloud Messaging
  sends through Google. That is a decision against "nothing phones home",
  to take on its own, if ever.

## What it defends against

- **A photographed or shared code.** The first claim spends it. It lives
  two minutes. Nothing is issued until a number that only the claiming
  phone shows is typed into the browser.
- **A claim that beats the person's own phone.** A script watching a
  shared screen, or a recording, claims within milliseconds of the QR
  appearing and names itself anything plausible. The browser then asks
  for a number. The person's phone failed to claim, so it shows none.
  There is nothing to type, so there is nothing to approve. The name the
  script chose is never what the person decides by, and is not shown at
  that step.
- **A stranger's code shown to the person.** The person scans a code from
  the stranger's browser, and the person's phone is then linked to the
  stranger's account: what the person types goes into the stranger's
  conversations. It takes the person reading the number off their own
  phone to the stranger, and the app's first screen names the account the
  phone is now in, so a wrong one is seen at once. The sentence under the
  number says: do not read it to anyone.
- **Two claims racing.** One statement moves `waiting` to `claimed`. The
  second finds it moved.
- **Secrets at rest.** Hashes only, in every table. The claim secret
  becomes the device's, so no secret waits in the clear to be collected.
- **Secrets in logs.** The code travels in a fragment and a JSON body,
  never in a path or a query. The secret travels in a bearer, which is
  never logged. The number travels in two JSON bodies and is worthless
  without the browser's cookie.
- **Cross-site requests.** Cookie writes carry `Origin`. The claim has no
  cookie. A bearer write is exempt, as today, and a browser cannot send a
  bearer it does not hold.
- **The device's name.** Text the phone chose, bounded, shown as data in
  the list, and never part of the decision.
- **A stolen device secret.** It is the device, until the device is
  removed: it reads and writes what the person owns. It mints nothing,
  not a token and not a pairing, and removes nothing. So it cannot outlive
  its removal, and the list is the whole of the person's control. The
  secret lives in the Keystore, out of backups, and never in a log, which
  is what makes stealing it hard in the first place.
- **A copied cookie.** It is a session for up to twelve hours. It mints
  no device and no token once it is ten minutes old: minting wants a
  fresh sign-in, which needs the provider's session, and that is not on
  the thief's machine.
- **A lookalike code.** A "set up Robinauts" QR that names another host
  would make that host the account. The app shows the origin, in
  punycode, and asks before it sends anything. A company that configures
  the app's server has every other origin refused outright. The secret,
  once held, goes to the origin it came from and nowhere else.
- **A person who has left.** Taken out of the allow list, they lose every
  credential within a minute. Refused at their next sign-in, they lose every
  credential at once. Known to an admin or an operator, they lose it on
  the admins' screen or at the command line. Known to nobody, they lose it
  when `device_days` pass.

## Known limits

- **A change at the identity provider is seen late.** The allow-list
  check on use reads what the last sign-in recorded. A person removed from
  a group at the provider, but still matched by that group in the allow
  list, keeps their phone until their next sign-in is refused, until an
  admin or the operator ends it, or until `device_days` pass. The
  platform keeps no provider token, so it cannot ask the provider in
  between. The operator's controls are the allow list, which is read
  every minute, the admins' screen, the command line, and `device_days`.
- **The fresh-sign-in gate is stolen-cookie strength, not keyboard
  strength.** Malware on the machine, or a script in the page, can sign in
  again through the provider's session and mint. The compensating control
  is the "Linked devices" list. A "a phone was linked" notice is not here
  yet.
- **A person with only their phone cannot pair.** Pairing needs a second
  screen. The browser hand-off is not here yet: the app opens the system
  browser on `/auth/login/{provider}`, the callback redirects to the app
  with a one-time code, and the app exchanges it for a device session. It
  ends in the same table and the same list.
- **The other direction is not here yet.** A browser signed in by scanning
  with a phone, for a shared screen or a kiosk, is the same pairing record
  with the roles swapped, plus the same number, shown on the browser and
  typed on the phone this time, since the phone is then the side that
  approves.
- **No typed code.** Where no camera can see the screen, there is no way
  to pair. A short code needs a rate limit, and is not here.
- **No audit entries yet.** Device linked, device removed and pairing
  refused are log lines, as sign-ins are, until the audit log exists.

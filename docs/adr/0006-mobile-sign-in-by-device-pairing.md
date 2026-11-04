# ADR 0006 — A phone gets in by pairing with a signed-in browser

- Status: proposed
- Date: 2026-11-04

## Context

A mobile application is a planned channel ([core.md](../specs/core.md),
"Channels"): an Android app first, consuming the same agents and
conversations through the same API. Before it can consume anything it has
to get in, and today there are two ways in, neither made for a phone:

- **A browser session.** OpenID Connect with the company's provider, a
  `__Host-` cookie, twelve hours, no renewal ([sign-in.md](../specs/sign-in.md)).
  The flow is a browser navigation, and the cookie is the browser's.
- **An API token.** Minted by a signed-in person, shown once, ninety days,
  sent as a bearer. It reaches every route, but it is a secret for a person
  to copy into a script, not something a phone can obtain on its own, and
  nothing shows which phone holds which token.

Whatever the app does, three things are needed: a way for a phone to obtain
a credential, a record of each device that holds one, which its owner and
the operator can see and end, and the API answering a bearer as completely
as it answers a cookie.

### How WhatsApp and Telegram link a device

Both use the same picture: the device that is **not** signed in shows a QR
code, and the device that **is** signed in scans it.

- **Telegram.** The desktop client asks the server for a login token, good
  for thirty seconds, and shows it as `tg://login?token=…`. The phone scans
  it and sends it back as its own, signed-in self, after a screen naming the
  app and device asking to be let in. The server tells the desktop, which
  asks again and this time receives an authorization. A deployment with a
  cloud password asks the new device for it as a second step. Every
  session is listed under "Devices" and can be ended there, and old ones
  end after an inactivity the person chooses.
- **WhatsApp.** The browser generates a key pair and shows a code holding
  a server reference and its public key, refreshed about every twenty
  seconds. The phone scans it, and the phone — not the server — signs the
  new device's identity and hands it its credentials encrypted to that
  key, because WhatsApp's server must not be able to read them. A linked
  device is listed on the phone and can be logged out from there, and is
  disconnected after the phone has been offline for two weeks. Where a
  camera cannot scan, the browser shows an eight-character code to type
  into the phone instead.

Two things carry over. The authenticated side is the one that **consents**,
and it is shown what it is consenting to. And a device, once linked, is a
record its owner can see and end.

One thing does not. In both products the phone is the authenticated side,
because the phone is where the account lives. In Robinauts the account
lives with the company's identity provider, and the place that signs in
with it well is a browser. So the roles swap: **the browser is the
signed-in side and shows the code; the phone scans it.** And since every
company runs its own Robinauts, the code also tells the app **which server
to talk to**, which no app for one global service ever had to solve.

## Decision

**A phone gets in by pairing with a browser that is already signed in. The
browser shows a code, the phone scans it, and the browser confirms the
device by name before anything is issued. What the phone receives is a
device session: a credential of its own kind, with a record the owner sees
and ends under "Linked devices".**

- The browser shows a **pairing code** as a QR code: the deployment's
  `public_url` and a random secret, good for two minutes and spent by the
  first phone that presents it.
- The phone presents it, with a name for itself, and receives a secret
  that is **inert until the browser approves**. The browser is shown the
  name the phone gave and asks the person whether that is their phone.
- On approval the secret becomes a **device session**. On refusal, or on
  expiry, it never does.
- A device session is a bearer like an API token, kept as its hash, with a
  fixed life the operator sets (`device_days`, 30 by default). It is listed
  with the device's name and last use, and removed by its owner, by the
  operator, or by the phone signing out.
- The server mints every secret and keeps every hash. There is no key
  exchange and nothing encrypted to the phone: the server is the company's
  own and is what issues the credential anyway.
- The whole design, as routes, records and screens, is
  [specs/devices.md](../specs/devices.md).

## Consequences

Good:

- **No change at the identity provider.** No second OAuth client, no
  mobile redirect URI, and the phone never talks to the provider. The
  decision not to verify ID token signatures ([sign-in.md](../specs/sign-in.md))
  stands, because no token is ever accepted from a phone.
- **The app learns the server from the code.** Nobody types a deployment's
  address into a phone. The same code works on a network with no way out,
  as long as the phone reaches `public_url`.
- **Pairing grants nothing new.** A signed-in browser could already mint
  an API token for anything. What pairing adds is the hand-over to a device
  that has no browser session, and a record of that device.
- **The records serve the other flows later.** A browser hand-off for a
  person who has only their phone, and pairing in the other direction
  (a browser signed in by scanning with the phone), end in the same device
  session and the same list.
- **The web gets the screen the API tokens never had.** "Linked devices"
  is where tokens are listed and revoked too.

Costs:

- **A device outlives the sign-in that made it.** Sessions last twelve
  hours; a device lasts `device_days`. The known limit of the allow list —
  evaluated at sign-in only — widens by that much. The operator's control
  is `device_days`, and a person's is the list. Removing a person's devices
  when a later sign-in of theirs is refused is a tightening to consider.
- **A photographed code can be presented by a stranger.** Within its two
  minutes, and before the owner's phone does. The confirmation step is
  what answers that: a name the person does not recognise is refused, and
  nothing was issued.
- **One new dependency**, a QR encoder, on the backend (`segno`,
  BSD-3-Clause, pure Python, no dependencies), so that the frontend's
  bundle and dependency list stay as they are. Its own pull request, under
  [DEPENDENCIES.md](../../DEPENDENCIES.md).
- **A person with only their phone cannot pair.** They need a second
  screen. The browser hand-off below covers them and is the planned
  follow-up.

## Alternatives considered

- **Native OpenID Connect on the phone.** The app opens the provider in a
  custom tab with PKCE, as a public client, and sends the backend an ID
  token or a code to exchange. Standard, and the provider sees every
  device, which some security teams will want. We did not take it first:
  every deployment would register a second client with a mobile redirect,
  the backend would have to verify ID tokens (a JWT library, the
  provider's keys, and the decision sign-in.md deferred), and the app
  would still need to be told where the server is. If a deployment's
  policy requires the provider to see the phone, the browser hand-off
  below gives it that without any of this.
- **The browser hand-off.** The app opens the system browser on our own
  `/auth/login/{provider}`; the existing flow signs the person in; the
  callback redirects to the app with a one-time code the app exchanges for
  a device session. Reuses everything, works with one phone, and the
  provider is in the loop. It needs a verified App Link (an
  `assetlinks.json` served by every deployment, naming the app's signing
  key) because a custom scheme can be claimed by another app, and the app
  must know the server's address first. **Planned second**, on the same
  device records.
- **Pairing with no confirmation: the scan is the consent.** Two routes
  fewer. Rejected: a code seen on a shared screen, or photographed, would
  link a stranger with nobody told.
- **A key pair in the code, the credential encrypted to the phone,
  WhatsApp's way.** Protects the credential from a server that must not
  read it. Our server mints it. Nothing to protect, and a crypto
  dependency to carry; rejected.
- **A typed short code instead of, or beside, the QR.** WhatsApp's "link
  with phone number". Useful where no camera can see the screen. A short
  code is guessable and needs a rate limit and a binding step; deferred,
  and the browser hand-off covers the one-phone case better.

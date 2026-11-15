# Device pairing

Pairing a phone is oneberth's: oneberth's `docs/specs/identity.md`, "Device pairing", is the
spec, and its `/auth/pairings…` and `/auth/devices…` routes serve it. The browser's pages for
it are `@oneberth/ui`'s account pages.

What Robinauts adds:

- A linked phone is a device session of its person. It holds that person's roles, so it
  reaches Robinauts' API under `/robinauts/api/` as the person's browser does.
- The phone reads `GET /auth/session` and sends its secret as a bearer, as oneberth says.
- The QR code holds `{public_url}/#/link?v=1&c=<code>`. The Robinauts app reads it. A plain
  camera app opens it in a browser, where the interface shows a page outside oneberth's shell:
  open this in the Robinauts app. The page reads no parameter and needs no session.
- The operator ends devices with `robinauts devices list` and `robinauts devices end`,
  oneberth's admin scripts ([sign-in.md](../specs/sign-in.md), "Admin scripts").

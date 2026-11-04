# Frontend

## The interface

- The layout is neorc's: a collapsible left navigation panel, and the chat
  in the middle. There is no top bar.
- **The panel is ours**, and not part of the chat window
  ([ADR 0006](../adr/0006-chat-ui-on-react-alone.md)). Top to
  bottom: the collapse button and the brand; "new chat"; the conversation
  history; projects, once they exist; and, pinned to the bottom, the
  profile block with sign-out. Collapsed, it becomes an icon rail. The state is remembered
  per browser.
- The application opens on an empty chat, ready for a first message, with
  the agent to talk to and the model selectable side by side. The model
  picker lists the operator's models, with no "default" entry: until one is
  picked it shows the chosen agent's default, and a pick is remembered per
  browser, as the agent is.
- An open conversation names the agent it is with, under its title: once
  the conversation exists the agent is a fact about it and not a choice,
  so it is shown as a line, not a control. The
  model stays a choice, so the model picker sits on that line, showing the
  conversation's model; changing it applies from the next turn, a run in
  flight included. A model the deployment no longer offers is shown as
  such, and a turn refused for it says so and keeps what was written — an
  edit in its own box. A first message refused because its model or its
  agent is no longer offered says which, and keeps the text; a refused
  model is forgotten by the browser.
- On a small screen the panel becomes an overlay drawer opened from a
  button, and the chat is usable on a phone.
- The theme follows the operating system by default. A light / dark /
  system toggle is remembered per browser.
- Until someone is signed in, the sign-in page stands in place of every
  page ([sign-in.md](sign-in.md)). In local development mode there is no
  sign-in page, and a permanent banner says that sign-in is off.

## The chat window

- Written in this repository, on React alone
  ([ADR 0006](../adr/0006-chat-ui-on-react-alone.md)): the thread, the
  composer, the messages and their action bars, and the disclosures for
  tool calls and reasoning, under `src/chat/thread/`, over a runtime of our
  own (`src/chat/runtime.tsx`) that holds the state and the turns. No chat
  library is used.
- **Markdown** is rendered by our own renderer, `src/chat/markdown/`,
  straight to React elements. No HTML string is produced, so raw HTML in an
  answer is drawn as text, and a link keeps its target only for `http:`,
  `https:` and `mailto:`. Images are drawn as links: the
  Content-Security-Policy allows no remote image.
- **The seam.** The rest of the application imports `src/chat/index.ts`,
  an interface this project owns, and nothing else under `src/chat/`. A lint
  rule enforces it. The panel, the history, routing and the session read
  our API directly.
- **The discard test.** Replacing everything under `src/chat/` but
  `index.ts` breaks one thing: the implementation behind
  `src/chat/index.ts`.
- It talks to the backend over AG-UI ([wire.md](wire.md)).

## Shape and delivery

- A single-page application. No Next.js, no server-side rendering, no Node
  process in production.
- The backend serves the built files, under a strict
  Content-Security-Policy. **No CDN, no external font, no third-party
  origin**: an air-gapped install works.
- System fonts. One icon set, lucide's, carried as data in the tree rather
  than installed. No third-party logos.
- The deliverable is **one Python wheel** that contains the built
  frontend: `pip install` plus a PostgreSQL is a complete deployment. The
  bundle is built only in CI; built assets are never committed, and a
  bundle built on a laptop is never released. A container image is
  planned.

## Supply chain

neorc's rules (`neorc/contributing/js-dependencies.md`):

- few dependencies, each one looked into before adoption — including what
  `npm pack` actually serves;
- exact versions, `npm ci` only, install scripts disabled;
- a build-time licence allowlist that fails the build, and a committed
  list of bundled packages that CI compares with the build
  ([open-source.md](open-source.md));
- `npm audit` and `npm audit signatures`;
- one dependency change per pull request; a 10-day Dependabot cooldown;
- a bundle size budget.

## Details likely to change

- Vite, React 19, TypeScript in strict mode, Vitest, ESLint. Hash routing,
  so the static files need no fallback route. Served under `/ui/`.
- The typed API client is generated from the committed OpenAPI snapshot.
- Content-Security-Policy: `default-src 'none'`; `script-src`, `font-src`,
  `connect-src` `'self'`; `style-src 'self' 'unsafe-inline'`;
  `img-src 'self' data:`; `frame-ancestors 'none'`; plus `base-uri 'none'`
  and `form-action 'none'`, which `default-src` does not cover — the page's
  script and stylesheet are named relatively, so an injected `<base>` would
  re-point both, and no form here posts anywhere. Plus `nosniff`, and
  `X-Frame-Options: DENY` for a browser too old to read `frame-ancestors`.
  Every answer carries them, refusals and API answers included, with
  `Referrer-Policy: same-origin`. The policy does nothing on JSON, and one
  list is simpler than two.
- The interface's paths are read: `GET` and `HEAD`, and a write to one is a
  `405` with `Allow: GET, HEAD`. Nothing under `/ui/` whose name begins with
  a dot is served or packaged: a directory a build writes into is one an
  editor or a stray `.env` writes into too.
- Design tokens are CSS custom properties carried over from neorc, in
  `src/tokens.css`. Every colour, radius and font is one of them. The
  styling is plain CSS over the tokens: a base reset, the body and the focus
  outline in `src/styles.css`, and one stylesheet per area, with class names
  prefixed by the area. No utility classes.
- The runtime dependencies are `react` and `react-dom`, and nothing else
  (2026-11-04). The icons are 22 inline SVG components in `src/icons/`, over
  path data copied from `lucide-react` 1.61.0 (ISC, and MIT for the icons
  lucide derives from Feather), recorded in
  [legal/ip-clearance.md](../legal/ip-clearance.md). No package is reached
  through a stylesheet.
- The wheel carries `THIRD_PARTY_LICENSES.txt`, listed in its
  `license-files`.
- **The bundle size budget is 400 KB**, everything the browser downloads,
  gzipped — which is what the build's own gate measures and the only number
  this is about. neorc's 500 KB was never going to fit a chat UI with
  Markdown in it, and the real one was left open until there was a bundle to
  measure: with the chat in it that is 278.6 KB (step 21, 2026-09-23), so the
  budget is that with room for the features named above and none for a
  dependency an order of magnitude too big. With the chat on React alone it is
  101.5 KB (2026-11-04), from 303.8 KB of JavaScript and 12.3 KB of CSS
  before; `react` and `react-dom` are about 76 KB of that. The budget was left
  where it was. It is `SIZE_BUDGET_BYTES` in
  `frontend/vite.config.ts` and the build fails over it; raising it is a
  reviewed change with its reason written down
  ([contributing/js-dependencies.md](../contributing/js-dependencies.md)).

# The Robinauts web UI

A single-page application: Vite, React, TypeScript, Vitest, ESLint. It is
served by the backend under `/ui/`, and it ships inside the `robinauts` wheel
([../docs/specs/frontend.md](../docs/specs/frontend.md)).

What is here is the whole interface: the design tokens and the plain CSS
over them, the collapsible panel with the profile block, the session
and the sign-in page, the theme toggle and the banner of the local
development mode, the hash routing, the history in the panel with renaming
and deleting, the agent and model pickers, and **the chat** -- the message
box, the answer arriving a word at a time, re-attaching to a run that is
already going, stopping one, editing a question, asking for an answer again,
and retrying or resuming an answer that failed.

Everything is this project's own code on React alone: `react` and
`react-dom` are the only runtime dependencies
([../docs/adr/0006-chat-ui-on-react-alone.md](../docs/adr/0006-chat-ui-on-react-alone.md)).
The chat is behind `src/chat/index.ts`, and nothing outside `src/chat/` knows
what draws it.

## Where things are

```
src/
  tokens.css       the design tokens: the source of every colour, radius and font
  styles.css       the base reset, the body and the focus outline
  icons/           the icons, as inline SVG over lucide's path data
  shell/, history/, session/, api/, conversation/,
  App.tsx, ErrorBoundary.tsx, main.tsx, router.ts
                   the shell: the panel, the history, the session, the routes
  chat/
    index.ts       the seam: the one file the rest of the application imports
    Chat.tsx       the <Chat> of the seam, over the runtime and the thread
    runtime.tsx    the state and the turn handlers, through a React context
    state.ts       the reducer: it owns every message and every byte of a stream
    agui/          our own AG-UI client, over fetch and a ReadableStream
    thread/        the chat window's components, and thread.css
    markdown/      the Markdown renderer, its parser and markdown.css
```

The AG-UI client is ours because the chat is ours, and it adds no dependency
([../docs/specs/wire.md](../docs/specs/wire.md), "Details likely to change").
Nothing here imports a library but React.

## The routes

Hash routing, written by hand in `src/router.ts`: no router library, and the
hash is the one part of a URL no server sees, so the static files need no
fallback route and nothing depends on the `/ui/` the backend serves them
under.

| hash                    | what it is                                       |
| ----------------------- | ------------------------------------------------ |
| `#/`                    | the empty chat, with the agent and model pickers |
| `#/c/<conversation id>` | that conversation, with its model picker         |
| anything else           | the empty chat, with the hash left as it is      |

`#/sign-in?error=<code>` is where the backend sends a failed sign-in; the
sign-in page reads it for itself, and the router neither claims nor rewrites
it.

## Running it

Node.js at the version in `.nvmrc`; with [nvm](https://github.com/nvm-sh/nvm),
`nvm use` in this directory.

    npm ci                  # never `npm install`, except to change a dependency
    npm run dev             # the dev server, proxying the API to 127.0.0.1:8000

`npm run dev` expects a local `robinauts start` on port 8000: `/api`, `/auth`,
`/health` and `/openapi.json` are proxied to it, with nothing in the way of a
run's event stream.

The backend to run beside it is the **local development mode**
([../docs/specs/sign-in.md](../docs/specs/sign-in.md)): no identity provider
to register anything with, one fixed local user, loopback only, and the
interface shows its permanent banner. From `../backend/`:

    export ROBINAUTS_DATABASE_URL=postgresql://127.0.0.1/robinauts
    export ROBINAUTS_CONFIG=robinauts.toml   # every start reads one
    uv run robinauts db init          # once, on an empty database
    uv run robinauts start --dev-no-sign-in

`--dev-no-sign-in` serves the loopback interface and refuses any other
`--host`. Only the model tables of `ROBINAUTS_CONFIG` are read, and the
agents and models it defines are the ones the pickers offer.

Signing in for real needs a `public_url`, a provider registration and the
redirect URI — that is a deployment, not a laptop. To see the sign-in page
itself, use the fixture server below.

### Looking at the shell without a backend

`scripts/fixture-server.mjs` serves a built `dist/` and answers the session,
the agents, the models and the conversation routes from fixtures, which is
how the states that would otherwise take a real sign-in and a database are
looked at:

    npm run build
    node scripts/fixture-server.mjs signed-out 5173

The scenes are `signed-in`, `signed-out`, `local`, `one-agent`, `no-agents`,
`history` (enough conversations to page through) and `conversation` (three of
them: a branch, a run in flight, and a run that ended badly, which is also on
a model the fixtures no longer offer). Which conversation is open is the
hash, so both conversation scenes serve every route; renaming, deleting,
cancelling and changing a model really change what is served, for as long as
the process runs. It is a development tool: it is in no check, no bundle and
no wheel.

Four more scenes **really stream**, over server-sent events, with a position
on the last wire event derived from each of the run's own and the two headers
a client re-attaches by ([../docs/specs/wire.md](../docs/specs/wire.md)):

| scene       | what it does                                                                  |
| ----------- | ----------------------------------------------------------------------------- |
| `streaming` | sending a message streams a stretch of thinking and then an answer            |
| `reattach`  | `…002` has a run going: opening it attaches to that run at its `resume.after` |
| `drop`      | the connection goes in the middle of the answer and the run does not          |
| `error`     | the same turn, ending in a `RUN_ERROR`                                        |

A turn really writes its messages into the conversation, and stopping one
really ends its stream with `RUN_FINISHED` and AG-UI's `cancelled` outcome,
so the state after either is what is served next.

## The checks

    scripts/check-frontend.sh   # from the root of the repository: all of it

or one at a time, from here:

    node scripts/check-licences.mjs   # the licence policy, and the exact pins
    npm run check-format    # prettier
    npm run lint            # eslint, including the seam rule
    npm run typecheck       # tsc, in strict mode
    npm test -- --run       # vitest
    npm run build           # the bundle, its licence gate and its size budget

`npm run format` rewrites what `check-format` complains about.

## Things a tool writes

Two of these are generated and thrown away; one is generated and committed.

- `src/api/schema.d.ts` — **not committed.** The types of our own API, written
  by `npm run generate` from `../backend/openapi.json`, the snapshot the
  backend commits. `dev`, `build`, `typecheck`, `lint` and `test` all run
  `generate` first, so a route that changed without the snapshot being
  refreshed is a failing check here rather than a surprise in a browser.
- `dist/` — **not committed.** The built assets; CI builds them, and a bundle
  built on a laptop is never released. It is also **what goes into the
  wheel**: `scripts/build-wheel.sh` runs `npm run build` here and hatchling
  carries `dist/` into the package as `robinauts/ui/`, which is where the
  backend serves it from under `/ui/`. A wheel cannot be built without it --
  `../backend/hatch_build.py` refuses, rather than producing one that installs
  and has no interface -- and `THIRD_PARTY_LICENSES.txt`, written into `dist/`
  beside the bundle, travels with it as one of the wheel's `license-files`.
  `scripts/check-wheel.sh` is the whole of that, end to end.
- `bundled-packages.txt` — **committed**, and written by `npm run build`: it is
  how a new package in the bundle becomes a line in a diff. Never edit it by
  hand. `scripts/check-frontend.sh` and CI set `CHECK_BUNDLED=1`, which makes
  the build compare the file instead of rewriting it, so a package that entered
  the bundle is a red build rather than a quiet amendment.

## The rules

- Dependencies: [../docs/contributing/js-dependencies.md](../docs/contributing/js-dependencies.md).
  Few of them, each one vetted, exact versions, install scripts off, a licence
  allowlist the build enforces over the bundle and
  `scripts/check-licences.mjs` enforces over everything installed, and one
  dependency change per pull request — never a version published fewer than
  ten days ago, first pin included.
- The chat seam: [../docs/adr/0006-chat-ui-on-react-alone.md](../docs/adr/0006-chat-ui-on-react-alone.md).
  Nothing outside `src/chat/` imports anything under `src/chat/` but
  `src/chat/index.ts`; what the rest of the application needs from the chat
  goes there. `eslint.config.js` refuses anything else, and
  `src/test/seam-rule.test.ts` proves the rule still fires. It is what keeps
  the chat window replaceable in one directory.
- Styling: plain CSS over the design tokens. `src/tokens.css` holds them as
  CSS custom properties and is the source of truth for every colour, radius
  and font: a rule writes `var(--panel)`, never a literal colour, and dark
  mode is the tokens' job, so no stylesheet has a `prefers-color-scheme` rule
  of its own. `src/styles.css` holds the base reset, the body and the global
  `:focus-visible` outline, and no more. Each area has **one stylesheet**,
  imported by the component that uses it (`import "./thread.css"`), and its
  class names are plain and carry the area's prefix: `shell-`, `panel-`,
  `history-`, `signin-`, `chat-`, `md-`, and `icon`. A prefix says which
  stylesheet a class lives in, which is all a reader needs to find it. There
  are no utility classes, no class-merging helper and no variant helper. A
  `style` attribute is only for a value that is genuinely dynamic. Animations
  are switched off under `prefers-reduced-motion: reduce`. No CSS Modules, no
  preprocessor, no `<style>` in `index.html`: the build refuses all three.
- Icons: `src/icons/`, one component per icon (`CheckIcon`, `XIcon`, …),
  imported from `src/icons` by a relative path and sized by the class it is
  given or its `size` prop. `index.tsx` is ours; `lucide.ts` is path data
  copied from `lucide-react` under the licence beside it (`LICENSE.lucide`:
  ISC, and MIT for the icons lucide derives from Feather), and is not
  edited. To add an icon, read its data from
  `dist/esm/icons/<name>.mjs` of `lucide-react` at a recorded version (from
  `npm pack`, outside the tree), append
  it to `lucide.ts` with the React `key`s dropped, export a component for it
  from `index.tsx`, and append an entry to
  [../docs/legal/ip-clearance.md](../docs/legal/ip-clearance.md) saying which
  icon, from which version, on which day ([src/icons/README.md](src/icons/README.md)).
  The package itself is never installed for it.
- Markdown: `src/chat/markdown/` renders an answer straight to React elements,
  with a parser of its own. Its safety contract is the reason it exists, and a
  change to it keeps all of it: no HTML string is ever produced and nothing
  goes through `dangerouslySetInnerHTML`, so raw HTML in an answer is text; a
  link keeps its target only for `http:`, `https:` and `mailto:`, and opens in
  a new tab with `rel="noopener noreferrer"`; an image is drawn as a link with
  its alt text, never as `<img>`; text still arriving is normal input, and an
  unclosed construct never throws. Parsing stays linear in the size of the
  text, which a performance test holds it to: a crafted answer must not be
  able to stall the page.
- Nothing from a third-party origin: no CDN, no external font, no inline
  script. An air-gapped install has to work.
- Every file carries the SPDX header ([../CONTRIBUTING.md](../CONTRIBUTING.md));
  what cannot is listed in [../REUSE.toml](../REUSE.toml).

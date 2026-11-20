# The Robinauts web UI

A single-page application: Vite, React, TypeScript, Vitest, ESLint. It is
served by the backend under `/ui/`, and it ships inside the `robinauts` wheel
([../docs/specs/frontend.md](../docs/specs/frontend.md)).

The interface is oneberth's shell, from `@oneberth/ui`, with two
applications on its rail: Robinauts and neorc
(oneberth's `docs/specs/shell.md`). The shell holds the session, the sign-in
page, the theme toggle, the account button and the account pages: personal
tokens, linked devices with the "Link a phone" dialog, and service tokens.
neorc is `@neorc/ui`, given the address of its API and Robinauts' forms to
start a run (`src/apps.tsx`, `src/flows/forms.ts`).

What is here is Robinauts' application: the plain CSS over oneberth's
tokens, the collapsible panel, the history in it with renaming and deleting,
the agent and model pickers, the side panel, the page a pairing code opens
in a browser, and **the chat** -- the message box, the answer arriving a word
at a time, re-attaching to a run that is already going, stopping one,
editing a question, asking for an answer again, and retrying or resuming an
answer that failed.

Robinauts' own code is on React alone
([../docs/adr/0006-chat-ui-on-react-alone.md](../docs/adr/0006-chat-ui-on-react-alone.md)).
The runtime dependencies are `react`, `react-dom`, and the two packages of
the platform, packed from the neorc commit `backend/pyproject.toml` pins by
`../scripts/ui-packages-from-source.sh`. The chat is behind
`src/chat/index.ts`, and nothing outside `src/chat/` knows what draws it.

## Where things are

```
src/
  main.tsx, App.tsx    oneberth's shell, and the page a pairing code opens
  apps.tsx             the two applications of the shell: Robinauts and neorc;
                       the entry of the package, @robinauts/ui (../package/)
  RobinautsApp.tsx     Robinauts' root component, as the shell loads it
  router.ts            Robinauts' routes, from the shell's route
  styles.css           the body, the focus outline and Robinauts' base reset
  icons/               the icons, as inline SVG over lucide's path data
  shell/, history/, side/, api/, conversation/, projects/, changes/
                       the application: the panel, the history, the side panel
  flows/forms.ts       Robinauts' forms to start a run, as A2UI for neorc
  chat/
    index.ts       the seam: the one file the rest of the application imports
    Chat.tsx       the <Chat> of the seam, over the runtime and the thread
    runtime.tsx    the state and the turn handlers, through a React context
    state.ts       the reducer: it owns every message and every byte of a stream
    agui/          our own AG-UI client, over fetch and a ReadableStream
    thread/        the chat window's components, and thread.css
    markdown/      the Markdown renderer, its parser and markdown.css
vendor/            @neorc/ui and @oneberth/ui, packed; not committed
```

The AG-UI client is ours because the chat is ours, and it adds no dependency
([../docs/specs/wire.md](../docs/specs/wire.md), "Details likely to change").

## The routes

Hash routing. The shell reads the first segment of the hash, and gives the
rest to the application it names; `src/router.ts` reads Robinauts' part. The
hash is the one part of a URL no server sees, so the static files need no
fallback route.

| hash                              | what it is                                                    |
| --------------------------------- | ------------------------------------------------------------- |
| `#/robinauts`                     | the empty chat, with the agent and model pickers              |
| `#/robinauts/c/<conversation id>` | that conversation, with its model picker                      |
| `#/neorc/…`                       | neorc's pages                                                 |
| `#/account/…`                     | oneberth's account pages                                      |
| `#/link`                          | the page a pairing code opens in a browser, outside the shell |

`#/sign-in?error=<code>` is where the backend sends a failed sign-in; the
shell's sign-in page reads it. `#/link?v=1&c=<code>` is the URL inside a
pairing QR
([../docs/architecture/device-pairing.md](../docs/architecture/device-pairing.md));
the query is never read, since the code is for the app.

## Running it

Node.js at the version in `.nvmrc`; with [nvm](https://github.com/nvm-sh/nvm),
`nvm use` in this directory.

    ../scripts/ui-packages-from-source.sh   # @neorc/ui and @oneberth/ui
    npm ci                  # never `npm install`, except to change a dependency
    npm run dev             # the dev server, proxying the API to 127.0.0.1:8000

`npm run dev` expects a local `robinauts start` on port 8000: `/robinauts`,
`/neorc`, `/auth` and `/health` are proxied to it, with nothing in the way of
a run's event stream.

The backend to run beside it is oneberth's **local mode**
([../docs/specs/sign-in.md](../docs/specs/sign-in.md)): no identity provider
to register anything with, one local user, loopback only. From
`../backend/`:

    export ROBINAUTS_DATABASE_URL=postgresql://127.0.0.1/robinauts
    export ROBINAUTS_CONFIG=robinauts.toml   # every start reads one
    uv run robinauts db init          # once, on an empty database
    uv run robinauts start --local

The start prints a URL with a ticket. Opening it signs the local user in,
and the cookie it sets serves the dev server on another port of the same
host too. Only the model tables of `ROBINAUTS_CONFIG` are read, and the
agents and models it defines are the ones the pickers offer.

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

Every scene with somebody signed in also answers **"Linked devices"**
(`#/account/devices`, oneberth's page): two phones and a token, a Remove and a Revoke that really
remove, a New token that really adds one and answers its secret, and a
"Link a phone" whose code is a hand-drawn picture of a QR. Its poll answers
`waiting` twice and `claimed` after that; the number the fixture phone
shows is `482 913`, anything else is a wrong number, and the third ends the
pairing. One more scene is for this page alone:

| scene           | what it does                                                                                              |
| --------------- | --------------------------------------------------------------------------------------------------------- |
| `stale-session` | `signed-in` with a session too old to mint: "Link a phone" and New token answer 403 `FreshSignInRequired` |

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
    node scripts/check-advisories.mjs   # npm audit

`npm run format` rewrites what `check-format` complains about.

## Things a tool writes

Two of these are generated and thrown away; one is generated and committed.

- `src/api/schema.d.ts` — **not committed.** The types of our own API, written
  by `npm run generate` from `../backend/openapi.json`, the snapshot the
  backend commits. `dev`, `build`, `typecheck`, `lint` and `test` all run
  `generate` first, so a route that changed without the snapshot being
  refreshed is a failing check here.
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
  the bundle is a red build.

## The rules

- Dependencies: [../docs/contributing/js-dependencies.md](../docs/contributing/js-dependencies.md).
  Few of them, each one vetted, exact versions, install scripts off, a licence
  allowlist the build enforces over the bundle and
  `scripts/check-licences.mjs` enforces over everything installed, and
  dependency changes in pull requests of their own — never a version published
  fewer than ten days ago, first pin included.
- The chat seam: [../docs/adr/0006-chat-ui-on-react-alone.md](../docs/adr/0006-chat-ui-on-react-alone.md).
  Nothing outside `src/chat/` imports anything under `src/chat/` but
  `src/chat/index.ts`; what the rest of the application needs from the chat
  goes there. `eslint.config.js` refuses anything else, and
  `src/test/seam-rule.test.ts` proves the rule still fires. It is what keeps
  the chat window replaceable in one directory.
- Styling: plain CSS over the design tokens. `@oneberth/ui` holds them as
  CSS custom properties, the source of truth for every colour, radius and
  font of the shell and both applications: a rule writes `var(--panel)`,
  never a literal colour, and dark mode is the tokens' job, so no stylesheet
  has a `prefers-color-scheme` rule of its own. `src/styles.css` holds only
  the body, the global `:focus-visible` outline and the base reset, which
  holds inside Robinauts' application. Each area has **one stylesheet**,
  imported by the component that uses it (`import "./thread.css"`), and its
  class names are plain and carry the area's prefix: `shell-`, `panel-`,
  `history-`, `side-`, `page-`, `chat-`, `md-`, and `icon`. A prefix says which
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

# For coding agents working in frontend/

This package ships to users inside the `robinauts` wheel and runs in a browser
with a signed-in person's session cookie, so any code in it can do whatever
that person may. Its dependency rules are in
[../docs/contributing/js-dependencies.md](../docs/contributing/js-dependencies.md);
read them before touching `package.json`, `package-lock.json`, `.npmrc`,
`vite.config.ts` or `bundled-packages.txt`.

Do not:

- add, remove or bump a package as part of another change. A dependency change
  is its own pull request, vetted as the rules say, with the cooldown kept: no
  version published fewer than 10 days ago. **That holds for a first pin as
  much as for a bump** -- `npm view <pkg> version` is often a release of that
  morning. Read `npm view <pkg> time --json`, pin the newest version at least
  ten days old, and say in the pull request which dates you read. No check can
  catch this for you: the publish dates are not in the lockfile.
- loosen the licence allowlist in `vite.config.ts`, the size budget, or the
  Content-Security-Policy the backend serves, to make a library fit. Choose
  another library, or write the code.
- run `npm install`. Installs are `npm ci`; `save-exact` and `ignore-scripts`
  stay on in `.npmrc`.
- commit `dist/`, `src/api/schema.d.ts` or `node_modules/`. The assets are
  built by CI and by the wheel build.
- edit `bundled-packages.txt` by hand. The build writes it; a change in it is
  what a reviewer looks at.
- import anything under `src/chat/` from outside that directory but
  `src/chat/index.ts`, and do not weaken the rules in `eslint.config.js` or
  the tests in `src/test/seam-rule.test.ts` that prove they fire. That seam is
  [ADR 0006](../docs/adr/0006-chat-ui-on-react-alone.md); what the rest of the
  application needs from the chat goes in `src/chat/index.ts`. The rule reads
  a path **segment**, so no spelling of a path is a way round it. Nor reach
  for a specifier no rule can read: `import()` or
  `new URL(..., import.meta.url)` with a variable or an interpolated
  template, and `import.meta.glob`, are refused precisely because nothing
  could clear them, and `require` is refused everywhere.
- add a runtime dependency to stand in for code this package writes for
  itself: a chat component, a Markdown feature, an icon, a class-name helper.
  `react` and `react-dom` are the whole runtime closure on purpose
  ([ADR 0006](../docs/adr/0006-chat-ui-on-react-alone.md)), and a new one is
  the dependency change of its own described above, with that ADR to argue
  against.
- weaken the Markdown renderer's safety contract in `src/chat/markdown/`. It
  produces React elements and never an HTML string; nothing in it goes
  through `dangerouslySetInnerHTML`; a link keeps its target only for
  `http:`, `https:` and `mailto:`; an image is drawn as a link, never as
  `<img>`. Its scanners are linear in the size of the text, and its
  performance test holds them to it: an answer is text a model wrote, and a
  quadratic scan is a way to stall the page.
- edit `src/icons/lucide.ts` by hand, or install `lucide-react` to get an
  icon. To add one, read its path data from `dist/esm/icons/<name>.mjs` of
  the package at a recorded version (`npm pack lucide-react@<version>` in
  a scratch directory outside the tree and look inside; nothing is installed), append it with the React `key`s dropped,
  export a component for it from `src/icons/index.tsx`, and **append an entry
  to [../docs/legal/ip-clearance.md](../docs/legal/ip-clearance.md)**, where
  entries are appended and never edited. The data is lucide's, under the
  licence in `src/icons/LICENSE.lucide` (ISC, and MIT for the icons lucide
  derives from Feather); the copy is what is recorded.
- write a path any way but plainly. `//`, `/./`, a `..` that is not the
  leading prefix and a trailing `/` are refused, as is any path through
  `node_modules/`: a package is imported by its name. One spelling per path
  is what lets the seam rule read one.
- add a CDN, a web font, a third-party origin or an inline script. An
  air-gapped install has to work, and the Content-Security-Policy forbids it.
- use CSS Modules, a stylesheet language other than plain CSS, or a
  `<style>` block in `index.html`. Each fails the build.
- write a literal colour, radius or font, a `prefers-color-scheme` rule, a
  utility class or a class-merging helper. Style with plain CSS over the
  tokens of `src/tokens.css` (`var(--panel)`), one stylesheet per area,
  imported by the component that uses it, with class names prefixed by the
  area (`shell-`, `panel-`, `history-`, `signin-`, `chat-`, `md-`, `icon`).
  Dark mode is the tokens' job. A `style` attribute is only for a value
  that is genuinely dynamic. Keep animations off under
  `prefers-reduced-motion: reduce`, and leave the global `:focus-visible`
  outline in `src/styles.css` alone.

Do:

- treat a stylesheet `@import` or `url()` that names a **package** as a
  dependency change. None does today. Rollup's module graph does not see one,
  so the build has to be told about it by hand, as
  [../docs/contributing/js-dependencies.md](../docs/contributing/js-dependencies.md)
  says.
- run `scripts/check-frontend.sh` from the root of the repository before
  declaring a change done; CI runs that same script.
- when a package that is not on the allowed list appears or changes version,
  update its row in the "JavaScript build tooling" table of
  `../DEPENDENCIES.md`, having read the licence in the version you are
  pinning. `scripts/check-licences.mjs` refuses a row that names another
  version, another licence, or a package that no longer needs one.
- refresh `../backend/openapi.json` with `scripts/update-openapi.sh` when the
  backend's routes change. `src/api/schema.d.ts` is generated from it by every
  script here and is not committed.
- put the SPDX header on every new file, and add to `../REUSE.toml` anything
  that cannot carry one.

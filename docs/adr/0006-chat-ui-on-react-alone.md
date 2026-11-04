# ADR 0006 — The chat window on React alone

- Status: accepted. It supersedes
  [ADR 0001](0001-chat-ui-assistant-ui-with-tailwind.md). The library, the
  vendored components and Tailwind go. What survives of ADR 0001 is
  restated below: the seam in front of the chat, the design tokens, and the
  wire that knows no UI library.
- Date: 2026-11-04

## Context

ADR 0001 built the chat window from assistant-ui's styled components, copied
into the tree, on Tailwind. It rejected its own alternative B, the window
written by hand, as against *compose over build* (core spec, goal 4), and
kept it as the fallback the seam would make cheap.

Six weeks on, the frontend shipped 13 runtime packages, and their closure was
205 packages (203 distinct names), 136 of them in the bundle. The lockfile
held 558 entries, 526 distinct names. The project owner asked for that tree
to shrink, in their words because its **transitive dependencies are not well
maintained**. Measured on 2026-11-04, the tree has this shape:

- **The Markdown.** `remark-gfm` and the `react-markdown` under it are the
  unified/remark family: 100 packages. 67 of them have a single maintainer,
  29 have had no release in over three years, and one account controls 82 of
  them.
- **The chat library.** `@assistant-ui/react` and `radix-ui` are 92
  packages, maintained mostly by the accounts of two organisations, and the
  library is before 1.0. Its wiring under `src/chat/assistant-ui/` absorbed
  that churn, as ADR 0001 said it would.
- **The copy.** The vendored components behaved as the fork ADR 0001
  predicted. Every re-sync was a manual merge, a provenance entry, and a
  check of each local modification against the new upstream.
- **The styling.** Tailwind, its two plugins, `clsx`, `tailwind-merge` and
  `class-variance-authority` existed to compile and combine the copy's
  utility classes. Three of them were reached only through a stylesheet, so
  they had to be named by hand in the build's record of what ships.

Meanwhile, what the library did for us had shrunk. The chat's features are
settled, and the parts that are hard are already ours. The state reducer
(`src/chat/state.ts`) owns every message and every byte of a stream, with no
library import. The AG-UI client (`src/chat/agui/`) owns the wire, the
re-attaching and the resuming. Tool calls, reasoning, a failed turn and
Refresh versus Resume are in that state and that wire code. What assistant-ui
still gave was the rendering layer: the thread, the composer, the message
action bars and the disclosures. That is the part alternative B writes.

## Decision

**The chat window is this project's own code, on React alone. The runtime
dependencies are `react` and `react-dom`, and nothing else.**

- **The window.** `src/chat/thread/` holds the thread, the composer, the
  messages, the action bars and the disclosures for tool calls and
  reasoning, with `thread.css` beside them. `src/chat/runtime.tsx` holds the
  state and the four turn handlers, exposed to the thread through a React
  context. `src/chat/Chat.tsx` is the `<Chat>` of
  the seam over the two. The DOM the end-to-end suite drives (the roles,
  names and `data-` attributes) is kept as it was, so the suite is the proof
  that nothing visible changed.
- **The Markdown.** `src/chat/markdown/` renders Markdown **straight to React
  elements**, with a parser of its own beside it. No HTML string is ever
  produced and nothing goes through `dangerouslySetInnerHTML`, so React
  escapes every text node and raw HTML in an answer is drawn as text. A link
  keeps its target only for `http:`, `https:` and `mailto:`; any other
  scheme is drawn as text. Images are drawn as links with their alt text,
  since the Content-Security-Policy allows no remote image anyway. Text
  still arriving is ordinary input: an unclosed fence, emphasis or table is
  drawn as far as it goes and never throws.
- **The icons.** The 22 icons the interface draws are inline SVG components
  in `src/icons/`. The component (`index.tsx`) is ours. The path data
  (`lucide.ts`) is copied from `lucide-react` 1.61.0 under its licence
  (ISC, and MIT for the icons lucide derives from Feather), with the licence
  text beside it, and the copy is an entry in
  [docs/legal/ip-clearance.md](../legal/ip-clearance.md). An icon carried as
  data is reviewed once, when it is copied, and does not change under us.
- **The styling.** Plain CSS over the design tokens. `src/styles.css` holds
  the base reset that stood in for Tailwind's, the body and the focus
  outline. Each area has one stylesheet, and its classes carry the area's
  prefix (`shell-`, `panel-`, `history-`, `signin-`, `chat-`, `md-`). There
  are no utility classes, no class-merging helper and no variant helper.
- **The vendored copy is deleted**, with its two licence texts and its
  README. Nothing of assistant-ui's or shadcn/ui's remains in the tree.

### What survives of ADR 0001

- **The seam.** `src/chat/index.ts` is still the one thing the shell
  imports, and its interface (`Chat`, `ChatProps`) does not change. What is
  behind it is now this project's code. The lint rule is retargeted to
  match: **nothing outside `src/chat/` imports anything under `src/chat/`
  but `index.ts`**. The discard test becomes: replacing everything under
  `src/chat/` but `index.ts` breaks exactly the implementation behind it.
  That keeps adopting a library again, if one is ever worth it, a change in
  one directory.
- **The design tokens.** `src/tokens.css` is untouched. It was written to
  depend on neither Tailwind nor the chat library, and now neither exists.
- **The wire.** The backend knows no UI library, and the interface and the
  backend speak AG-UI ([specs/wire.md](../specs/wire.md)). Nothing on the
  backend changes.

### Why ADR 0001's judgement is reversed

ADR 0001 weighed the convenience of upstream's components against writing
them, while the chat's features were still arriving. Three things changed
the weights:

- **The churn.** A library before 1.0 changes its interface often. Each bump
  was work inside the seam, and the cooldown kept the copy a release behind
  the library it was written against.
- **The fork.** A copied component is not updated by `npm update`. It is a
  second codebase that is somebody else's, merged by hand, with a provenance
  record for every merge.
- **The supply chain.** Two hundred packages ran beside a signed-in
  person's session to draw one window. Most of what they did is now done by
  our own state and wire code, and the rest is components and CSS of our
  own, which change only when we change them.

Once the features were settled, the convenience no longer paid for the
surface. *Compose over build* still holds where composing pays.

## Consequences

Good:

- **The runtime closure is three packages**: `react`, `react-dom`, and the
  `scheduler` `react-dom` depends on, down from 205. The bundle record,
  the licence gate and every audit read three entries.
- **The bundle is 101.5 KB gzipped**, from 303.8 KB of JavaScript and
  12.3 KB of CSS. `react` and `react-dom` alone are about 76 KB of it. The
  budget stays at 400 KB.
- **A class of attack is gone rather than mitigated.** No HTML is ever
  parsed from a model's answer, so there is nothing to sanitise and no
  sanitiser to keep up to date.
- **No package is reached through a stylesheet**, so the by-hand list of
  such packages in the build is empty. The mechanism stays for the day one
  is.
- **The window changes only when we change it.** No re-sync, no vendored
  edits to re-apply, no provenance entry for an upstream release.

Costs:

- **We maintain the window.** The thread, the composer, the scrolling and
  the disclosures are ours to fix, and an accessibility improvement upstream
  does not arrive by itself.
- **We maintain a Markdown parser.** What remains of its risk is
  algorithmic: a crafted answer that takes long to parse. The parser is
  built from linear scanners, and a performance test holds it to that.
- **A new feature is styling work**, not an upstream component. ADR 0001
  counted that as the cost of alternative B, and it is the cost we take.
- **The dev tooling is unchanged** but for `@tailwindcss/vite`, which goes
  with Tailwind. TypeScript stays at 5.9.3. Moving it to 7.0.2 (published
  2026-07-08, outside the ten-day cooldown) was tried: `tsc` itself builds
  the tree, but the two tools that read the compiler's own API refuse it --
  `typescript-eslint` 8.72.0 declines TypeScript 7 outright, and
  `openapi-typescript` 7.13.0 needs the JavaScript API the 7.x package no
  longer ships. An `overrides` entry pinning a nested 5.9.3 for those two
  does not take, because TypeScript is a peer dependency of theirs. The
  move waits on those two tools.

## Alternatives considered

- **Keep `react-markdown` and `remark-gfm` as the one exception.** We
  rejected it. That one exception is the unified tree: 100 of the 205
  packages, and the part of the tree the measurements above describe. Kept,
  it would leave most of the problem in place.
- **A parser-only package**, such as `marked`'s lexer, with its tokens
  rendered to React elements by our own code. It keeps the no-HTML property
  and saves writing a parser. We rejected it. OSV lists 13 advisories for
  `marked`, 8 for `markdown-it` and 2 for `markdown-to-jsx`, against none for
  the unified family. A parser is where a Markdown library's advisories
  live, so taking one takes on its record, and its releases, for the part
  we can bound with a test.
- **Render Markdown to HTML and sanitise it.** We rejected it. It brings
  back the class of attack the decision removes, and a sanitiser with it:
  OSV lists 33 advisories for `dompurify`. The Content-Security-Policy stays
  as it is either way.
- **Keep assistant-ui's primitives and write only the markup**, ADR 0001's
  alternative B as it was written. We rejected it. The primitives are the
  library, and its 92 packages with it. They would carry its pre-1.0 churn
  into a window whose state they no longer hold.
- **Keep Tailwind, without the copied components.** We rejected it. The
  shell's styling is small, the tokens already carry every value, and
  Tailwind is the reason packages had to be written down by hand as reached
  through a stylesheet.

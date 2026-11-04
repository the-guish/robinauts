# ADR 0001 — Chat UI: assistant-ui styled components on Tailwind, behind an explicit seam

> **Superseded. Skip this ADR unless you need its history.**
> [ADR 0006](0006-chat-ui-on-react-alone.md) replaces the library, the
> vendored components and Tailwind. The seam, the design tokens and the wire
> boundary survive, and ADR 0006 restates them. For the current design, read
> ADR 0006.

- Status: superseded by [ADR 0006](0006-chat-ui-on-react-alone.md)
- Date: 2026-09-20

## Context

The frontend uses the open source
[assistant-ui](https://github.com/assistant-ui/assistant-ui) library for the
chat window (core spec, "Stack"). The rest of the frontend follows the neorc
UI: the collapsible left navigation, the sign-in page and the profile block.

assistant-ui comes in two layers:

- `@assistant-ui/react` is an npm package (MIT). It holds the runtime, the
  state, the streaming and **unstyled** primitives. It does not need
  Tailwind.
- The **styled** components (`Thread`, `Composer`, messages and so on) are
  source files in a shadcn-style registry. A CLI copies them into the
  consuming repository, and that repository's Tailwind build compiles them.

The npm packages of the styled layer are not usable.
`@assistant-ui/react-ui` is stale (0.2.1, October 2025), and
`@assistant-ui/styles` is deprecated. We checked on 2026-09-20.

Two options were left:

- **A.** Adopt Tailwind and copy the styled components.
- **B.** Use only the npm primitives, and write the markup and CSS by hand,
  as the neorc UI does.

Three goals of the core spec bear on the choice:

- *Compose over build* (goal 4). We build only the glue and the missing
  parts.
- *Components are swappable* (goal 4). The project must be able to drop
  assistant-ui entirely.
- *Open source without restrictions* (goal 1). Code copied in from outside
  is a provenance event, and we record it.

## Decision

**We take option A.** The frontend uses Tailwind CSS, with assistant-ui's
styled components copied into the repository. assistant-ui stays behind an
explicit seam, so it can be dropped.

### The seam

Two boundaries enclose assistant-ui. It exists only between them.

**1. The wire.** The backend knows nothing of assistant-ui. The interface and
the backend speak AG-UI, a protocol of no UI library (goal 4). Its details
are in [specs/wire.md](../specs/wire.md). assistant-ui shapes no backend
route, payload or stored record. `@assistant-ui/react` brings
`assistant-cloud` as a dependency, and we never configure it. Conversations
live in our database only (goal 3).

**2. The chat module.** The frontend source looks like this:

```
src/
  shell/                 layout, navigation, sign-in, session, profile
  chat/
    index.ts             the seam: what the rest of the app may import
    assistant-ui/        the ONLY place assistant-ui exists
      vendor/            styled components copied from upstream
      ...                runtime wiring: our API/AG-UI client <-> assistant-ui
```

Rules:

- `src/chat/index.ts` exports a small interface of our own. It is in
  essence a `<Chat>` component that takes a conversation id and callbacks
  (conversation created, title changed). Its types name nothing from
  assistant-ui.
- Only files under `src/chat/assistant-ui/` may import `@assistant-ui/*` or
  anything under `vendor/`. An ESLint `no-restricted-imports` rule enforces
  it, as a blocking check.
- The shell owns everything outside the chat window: the conversation list
  in the sidebar, "new chat", routing and the session. These read our
  backend API directly. They use no thread list of assistant-ui's.
- Design tokens are CSS custom properties, carried over from neorc. The
  Tailwind theme refers to the tokens. The tokens depend on neither Tailwind
  nor assistant-ui.

**The discard test.** Deleting `src/chat/assistant-ui/` and the
`@assistant-ui/*` packages must break exactly one thing: the implementation
behind `src/chat/index.ts`. The shell, the routes, the backend and the stored
data stay as they are. A change that breaks this rule is a change to this
ADR.

### Vendoring rules

- The copied files live only in `src/chat/assistant-ui/vendor/`. A
  `README.md` there names the upstream URL, the exact upstream commit, our
  local changes and how to re-sync. Upstream's MIT licence text sits beside
  them.
- Every copy and every re-sync gets an entry in `docs/legal/ip-clearance.md`.
  `docs/legal/third-party.md` lists the components.
- Edits inside `vendor/` stay minimal, so a re-sync is a small diff. Our own
  code lives outside it.
- The packages the styled components bring (`clsx`, `tailwind-merge`,
  `class-variance-authority`, `lucide-react`, `tw-animate-css`, Radix) pass
  the same bundle licence gate as every other dependency. Icons come from
  the one allowed icon package. No fonts are added.

### Shell

The neorc shell moves to Tailwind, with the neorc tokens as the theme: the
sidebar, the rail collapse kept in `localStorage`, the profile block at the
bottom and the dark mode the system chooses. The shell and the chat share
one look. The frontend has one styling system.

## Consequences

Good:

- A working chat window takes hours rather than days: streaming, editing,
  the branch picker, copying and scrolling. Later features, such as a tool
  call's UI or attachments, arrive as upstream components rather than
  styling work.
- This is upstream's supported path, on a stack most React contributors
  know.
- The seam keeps the cost of leaving assistant-ui known and bounded.

Costs:

- Vendored files behave like a fork. Upstream fixes do not arrive through
  `npm update`. Each re-sync is a manual merge plus its provenance records.
- assistant-ui is before 1.0 and changes often. The wiring under
  `src/chat/assistant-ui/` absorbs that churn.
- The neorc shell cannot be reused as it is. Porting it to Tailwind takes
  about a day.
- The seam rules out some conveniences of assistant-ui, such as its thread
  list and its cloud persistence. That is intended.

## Alternatives considered

- **B. Primitives only, with CSS written by hand.** It needs only
  dependencies, reuses the neorc shell as it is, and copies nothing. We
  rejected it. It rebuilds a styled chat layer that exists upstream, and
  pays again for each later feature, against *compose over build*. It stays
  the fallback, and the seam is what makes that move cheap.
- **The styled layer's npm packages** (`@assistant-ui/react-ui`,
  `@assistant-ui/styles`). They are unmaintained or deprecated.

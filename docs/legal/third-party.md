# Third-party code and assets in the tree

Everything in the repository that was not written for it: vendored code,
copied components, fonts, icons, images, fixtures, sample data. Each line
states what it is, where it lives, where it came from, and its licence. The
full licence texts are in [LICENSES/](../../LICENSES/).

Dependencies installed by a package manager are not listed here; they are
governed by [DEPENDENCIES.md](../../DEPENDENCIES.md).

| what | where | source | licence |
|---|---|---|---|
| lucide's icon path data: the SVG elements of 24 icons, as data, with no code of lucide's | `frontend/src/icons/lucide.ts` | [lucide](https://github.com/lucide-icons/lucide), the npm package `lucide-react` 1.61.0 | ISC, Copyright (c) 2026 Lucide Icons and Contributors; the icons lucide derives from Feather are MIT, Copyright (c) 2013-present Cole Bemis ([text](../../frontend/src/icons/LICENSE.lucide)) |

The data was copied on 2026-11-04 under
[ADR 0006](../adr/0006-chat-ui-on-react-alone.md), and the copy is an entry in
[`ip-clearance.md`](ip-clearance.md), which names the icons and which of them
are Feather's. The components that draw it, `frontend/src/icons/index.tsx`,
are **not** third-party: they are written here, under this project's own
licence. How to add an icon is in
[`frontend/src/icons/README.md`](../../frontend/src/icons/README.md).

The assistant-ui and shadcn/ui components that were vendored under
`frontend/src/chat/assistant-ui/vendor/` from 2026-09-22 were removed on
2026-11-04, with their licence texts, and nothing of theirs remains in the
tree ([`ip-clearance.md`](ip-clearance.md) has both entries).

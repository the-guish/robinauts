# Icons

Inline SVG components, one per icon, over path data copied from
[lucide](https://github.com/lucide-icons/lucide) rather than installed from
it. `index.tsx` is this project's code; `lucide.ts` is lucide's data, under
the licence beside it (`LICENSE.lucide`: ISC, and MIT for the icons lucide
derived from Feather, fifteen of ours among them), and the copy is recorded in
[`docs/legal/ip-clearance.md`](../../../docs/legal/ip-clearance.md). The
`REUSE.toml` at the root of the repository states the copyright of each.

`lucide.ts` is **generated and not edited**: each entry is the `node` of one
icon as `lucide-react` ships it, with the React `key` of each element
dropped. It was written from `lucide-react` 1.61.0 on 2026-11-04, reading
`dist/esm/icons/<name>.mjs` of the installed package. `panel-right` was
appended on 2026-11-08 from the same version, read out of `npm pack` in a
scratch directory. The twenty-three icons in it are the ones the interface
draws; to add one, read it from the same place at a recorded version and
append a new entry to the clearance log.

An icon is decorative by default (`aria-hidden`); the control around it is
what carries the name. Give it `title` when it stands alone.

# @robinauts/ui

Robinauts' interface as a node package, for a host with applications of its
own ([../../docs/specs/frontend.md](../../docs/specs/frontend.md), "The
interface as a package"). Robinauts' own interface is the application in
[..](..), which uses the same source.

The package ships its TypeScript source, and the host's build compiles it.
React, React DOM, `@oneberth/ui` and `@neorc/ui` are its peers: the host
brings one copy of each. It brings no other dependency.

## Using it in a host

```tsx
import { applyRememberedTheme, Shell } from "@oneberth/ui";
import { neorcWithForms, robinautsApp } from "@robinauts/ui";
import "@robinauts/ui/styles.css";

const apps = [robinautsApp(), neorcWithForms(), mine];

applyRememberedTheme();
createRoot(root).render(<Shell apps={apps} title="Mine" />);
```

- `robinautsApp()` is Robinauts' application. Its API is the component
  `robinauts` of the same backend, at `/robinauts/`.
- `neorcWithForms()` is neorc's application with Robinauts' forms to start a
  run. Its API is the component `neorc`, at `/neorc/`.
- `styles.css` holds the page's base and Robinauts' own reset. The host
  imports it before its own stylesheets.
- The backend's side is `robinauts_parts`
  ([../../docs/architecture/web.md](../../docs/architecture/web.md),
  "Robinauts as a part").

## Packing it

No package is published. `scripts/robinauts-ui-pack.sh <directory>` packs
`robinauts-ui-0.0.0.tgz` from this checkout, beside the two tarballs of its
peers, `neorc-ui-0.0.0.tgz` and `oneberth-ui-0.0.0.tgz`.

[package.json](package.json) is the package's manifest. The application's
own, [../package.json](../package.json), stays as it is: its dependencies
are what the licence gate and the audit read.

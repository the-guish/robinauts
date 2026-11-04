# Open source

Goal 1: Apache-2.0, safe inside a company and safe to embed in a
commercial product. The project follows the "foundation-ready" policy,
whose premise is that foundations and corporate legal teams accept
provenance, not code: for any line in the tree, one can show who wrote it,
when, under what licence, and that they had the right to.

The list of things to do is [oss-checklist.md](../oss-checklist.md).

## Identity

- The name is Robinauts. The repository is `open-shipyard/robinauts`, in an
  organisation, not a personal account.

## Licence

- Apache-2.0, unmodified, for everything in the repository, documentation
  included. No dual licence, no added restriction.
  <!-- REUSE-IgnoreStart -->
- Every source file carries `SPDX-License-Identifier: Apache-2.0` and
  `Copyright The Robinauts Authors`. `REUSE.toml` covers files that cannot
  carry a header. Every byte in the tree has a stated licence.
  <!-- REUSE-IgnoreEnd -->
- `NOTICE` holds required attributions only.

## Contributions

- DCO sign-off on every commit, from the first. No CLA.
- AI coding assistants are allowed. By signing off, a contributor states
  that they reviewed every line, that it reproduces no incompatibly
  licensed code, and that substantially machine-generated files or blocks
  are disclosed in the pull request.
- No code from Stack Overflow or blogs. Nothing copied or ported from a
  forbidden or unlicensed source.
- Code that enters the tree other than through an ordinary signed-off pull
  request is recorded in `docs/legal/ip-clearance.md`: where it came from,
  at which commit, under what licence, where it landed, what was changed.
  Known cases: the sign-in design and code, the frontend tooling and the
  design tokens from neorc, the vendored assistant-ui components (since
  removed), and the icon path data copied from lucide.
- Third-party assets and vendored code are listed in
  `docs/legal/third-party.md`. An upstream header is never stripped.
- No squash-imports; `main` is never force-pushed.

## Dependency licences

The ASF category model — the strictest in use. Written for contributors
in `DEPENDENCIES.md`.

- **Allowed:** Apache-2.0, MIT, MIT-0, BSD-2-Clause, BSD-3-Clause, 0BSD, ISC,
  Zlib, PostgreSQL, PSF-2.0, CNRI-Python, CC0-1.0, Unlicense.
- **Restricted:** MPL-2.0 (also EPL-2.0, CDDL), only as an unmodified,
  unbundled dependency, each one listed by name with its reason.
- **Forbidden, including transitively:** GPL, AGPL, LGPL (every version,
  dynamic linking included), SSPL, BSL, Elastic, Commons Clause,
  CC-BY-NC/ND, any "no commercial use" or "do no evil" term, proprietary,
  and **no licence at all**.
- Development-only and test-only dependencies follow the same lists. One
  may be excepted only by name, with a reason.
- One narrow exception to "unknown fails", for any **Python** dependency,
  runtime included: metadata that pastes an allowed licence's *text* where
  its name belongs, and names nothing else. It is excepted by name, pinned to
  the version that was read, and the gate checks the text's title line
  against the licence the row states (`DEPENDENCIES.md`, "Excepted licence
  texts"; the project owner's decision, 2026-09-28). The npm gate has no
  such exception, and bundled code — the JavaScript bundle — is excepted
  nowhere.
- The JavaScript bundle is bundled code: the allowed list only.
- Assets — fonts, icons, images, fixtures, sample data, model files,
  tokenizers — need a stated licence on the allowed list. No model weights
  or datasets are shipped. No third-party logos are shipped.
- Adopting a dependency means: the gates pass at the pinned version; the
  package is what it claims to be; the reason for it is in the pull
  request. A licence is re-checked on every bump.

## Checks

On GitHub Actions, all blocking, all present from day zero:

- Python: a licence gate over the full locked set, failing on unknown
  licences too — save the by-name, by-version licence-text exception above;
  a vulnerability audit.
- JavaScript: the build-time licence allowlist; the committed list of
  bundled packages compared with the build; `npm audit`;
  `npm audit signatures`.
- `reuse lint`; a DCO check; the architecture contracts
  ([architecture/rules.md](../architecture/rules.md)); the lint rule that
  keeps the chat behind its seam. Secret scanning is planned.
- Dependabot for uv, npm and GitHub Actions, with a 10-day cooldown.
  Actions are pinned to commit SHAs.

## Releases

- Built in CI from a signed tag; published by trusted publishing, with no
  long-lived token.
- `LICENSE`, `NOTICE` and the third-party licence texts are inside the
  wheel.
- Each release carries an SBOM.

## Details likely to change

- Tools: `pip-audit`; the project's own `scripts/licence_gate.py` for the
  Python gate;
  `rollup-plugin-license` for the bundle; `gitleaks`; `syft` for the SBOM.
- Known findings:
  - `psycopg` and `psycopg-pool` are LGPL-3.0-only: excluded; `asyncpg` is
    used. Consequently `langgraph-checkpoint-postgres` cannot be adopted
    as it is
    ([ADR 0002](../adr/0002-conversation-persistence.md)).
  - `certifi` is MPL-2.0 and arrives with the HTTP client: a restricted
    case, unmodified and unbundled, like `pathspec`, `orjson` and npm's
    `lightningcss`.
- The two agent frameworks and their provider clients are the largest
  dependency surface, and the likeliest source of a future conflict.

# The October refactor, in two stages

Stage one makes the product workable again on the new layers. Stage two hardens it.
Each block is planned in its own note in this folder when its turn comes, with the
rules of `config-plan.md`: one branch per step, one commit per branch, the happy
path, minimal code.

## Stage one: workable again

1. **Import-linter rules for the new layers.** Done: `docs/architecture/rules.md`.
2. **Configuration reading and the CLI.** Done: `config-plan.md`.
3. **LangChain engine over in-memory storage.** The first real model and MCP tools
   through the contract, proven by a contract suite both engines must pass, with echo
   as the third subject.
4. **Pydantic AI engine over in-memory storage.** Same suite, second implementation,
   which is what proves the contract is not shaped by one framework.
5. **The conversation format's encoding and versioning.** The documents a durable store
   writes and reads back. Nothing durable can be written before this is decided. It
   grew into the data model as a whole: turns with ids, the store port in the shape a
   durable store needs, and the run id on the wire (`docs/architecture/data-model.md`).
   Planned: `data-model-plan.md`.
6. **PostgreSQL.** The store over asyncpg, the schema with `db init` and the start-up
   check, `LISTEN` and `NOTIFY` for watchers in other processes, and both engines'
   PostgreSQL storage. Planned: `postgres-plan.md`.
7. **Auth.** The OIDC flow, sessions and cookies, the allow list, sign-out, API tokens,
   and `ensure_user` called from the real identity.
8. **The plan for stage two.** A detailed plan capturing every learning still held in
   legacy's code, tests and specs: the refusals, the edge cases, the bounds, the
   protections, the operations, so that nothing is lost when legacy goes.
9. **Removal.** Delete `robinauts.legacy` and `docs/specs/legacy`, repoint what still
    references them, and rewrite `layout.md`, `backend.md` and the ADRs for the new
    layers.

## Stage two: hardening

Planned in detail by stage one's step 8, and gathered meanwhile in
`stage-two-plan.md`, which the data model's review added to. The blocks known today:

1. **Controller hardening.** The refusals, one active turn, the timeout, the bounded
   cancel, the lease renewed and the sweep for turns nobody reads, the edit of a first
   question, titles, and fork in the controller and in both engines.
2. **Web protection and the rest of the wire.** CSRF and origin checks, the body bound,
   the security headers and CSP, hashed asset caching, the error mapping with its
   exhaustive test, log redaction, and the wire's "not yet served" list.
3. **Packaging and operations.** The wheel hook, the demo, the deployment rehearsal and
   the CI scripts.
4. **Housekeeping.** Trash expiry and retention, each calling `forget`, on the process's
   own schedule.
5. **Context management in the engines.** Summarisation or trimming within the model's
   window, and the vendor's prompt cache, by each framework's own means.

## Stage three: open to other packages

Not planned until stages one and two are done.

1. **Extension points.** A store, a turn dispatcher and an engine's storage supplied
   by an installed package that this repository does not name, and the store's
   contract suite importable by such a package. It is what an integration for a
   serverless cloud, in a repository of its own, would build on
   (`aws-serverless.md`). This repository ships no vendor's library, in this stage or
   any other.

## Discarded

- SQLite as a local storage kind. PostgreSQL is the one database; in-memory storage is
  for tests and a local start.

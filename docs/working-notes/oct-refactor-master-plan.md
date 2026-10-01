

1. **Import-linter rules for the new layers.** Cheap, and it locks the structure before more code lands on it.
2. **Configuration reading and the CLI.** The TOML and the environment into the contract's `Config`, every problem reported together, and `robinauts start` and `version` over web. Every later block is exercised through a real configuration.
3. **LangChain engine over in-memory storage.** The first real model and MCP tools through the contract, proven by a contract suite both engines must pass, with echo as the third subject.
4. **Pydantic AI engine over in-memory storage.** Same suite, second implementation, which is what proves the contract is not shaped by one framework.
5. **Controller hardening.** The refusals, one active turn, the timeout, the bounded cancel, the sweep at start, the edit of a first question, titles, fork. Done while the store is still dicts, so it is cheap to test.
6. **The conversation format's encoding and versioning.** The documents a durable store writes and reads back. Nothing durable can be written before this is decided.
7. **SQLite as the local kind**, in the controller and in both engines' storage. A complete local mode with persistence and no database server, which is also what a terminal shell needs.
8. **PostgreSQL.** The store over asyncpg, the schema with `db init` and the start-up check, `LISTEN` and `NOTIFY` for watchers in other processes, and both engines' PostgreSQL storage.
9. **Housekeeping.** Trash expiry and retention, each calling `forget`, on the process's own schedule.
10. **Auth.** The OIDC flow, sessions and cookies, the allow list, sign-out, API tokens, and `ensure_user` called from the real identity.
11. **Web protection and the rest of the wire.** CSRF and origin checks, the body bound, the security headers and CSP, hashed asset caching, the error mapping with its exhaustive test, log redaction, and the wire's "not yet served" list.
12. **Packaging and operations.** The wheel hook, the demo, the deployment rehearsal and the CI scripts, all repointed from legacy.
13. **Removal.** Delete `robinauts.legacy` and `docs/specs/legacy`, repoint what still references them, and rewrite `layout.md`, `backend.md` and the ADRs for the new layers.

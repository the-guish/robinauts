# Robinauts — project layout

Goals and design principles are in [specs/core.md](specs/core.md). Decisions
are in [adr/](adr/).

```
robinauts/
  .github/          the CI workflow and Dependabot
  docs/             specs, decisions, architecture, legal records
  scripts/          one script per check; CI runs these
  demo/             one command to run the whole thing on one machine
  docker/           the control plane, a sandbox worker and PostgreSQL, in
                    three containers
  frontend/         the UI (ADR 0006)
  backend/
    src/robinauts/
      agent_engines/    the agent port and its engines: contract/,
                        echo_engine/, langchain_engine/, pydantic_ai_engine/
      controller/       the application: contract/, core/, ports/,
                        application/, adapters/{memory,postgres,http}/,
                        composition/
      web/              the HTTP server, sign-in, the CLI, the worker's process
    tests/              unit/, fast/, api/, adapters_free/, adapters_metered/,
                        e2e/, tooling/, util/
```

- The layers and what each may import:
  [architecture/rules.md](architecture/rules.md). `backend/pyproject.toml`
  enforces them, and `tests/tooling/test_architecture.py` runs them.
- The controller: [architecture/controller.md](architecture/controller.md).
- The web layer: [architecture/web.md](architecture/web.md).
- The stored records: [architecture/data-model.md](architecture/data-model.md).
- Sandboxed agents, whose turns run in a sandbox worker over the worker API:
  [architecture/sandbox-workers.md](architecture/sandbox-workers.md).

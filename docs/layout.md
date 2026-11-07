# Robinauts — project layout

Goals and design principles are in [specs/core.md](specs/core.md). Decisions
are in [adr/](adr/).

```
robinauts/
  .github/          the CI workflow and Dependabot
  docs/             specs, decisions, architecture, legal records
  scripts/          one script per check; CI runs these
  demo/             one command to run the whole thing on one machine
  frontend/         the UI (ADR 0006)
  extra-workers/    workers that run beside the server, each a project of its
                    own composed from backend/ (extra-workers/README.md)
  backend/
    src/robinauts/
      shared_domain/    the records more than one package uses
      agent_engines/    the agent port and its engines: contract/,
                        echo_engine/, langchain_engine/, pydantic_ai_engine/
      controller/       the application: contract/ (and contract/broker/),
                        core/, ports/, application/,
                        adapters/{in_process,postgres}/
      worker/           runs the turns; of the controller it sees
                        contract/broker/ alone
      web/              the HTTP server and sign-in
      cli/              the robinauts command: builds the controller and the worker
                        (composition.py), and starts and stops them and the HTTP server
    tests/              unit/, fast/, api/, adapters_free/, adapters_metered/,
                        e2e/, tooling/, util/
```

- The layers and what each may import:
  [architecture/rules.md](architecture/rules.md). `backend/pyproject.toml`
  enforces them, and `tests/tooling/test_architecture.py` runs them. Nothing
  under `backend/` names an extra worker; `scripts/check-extra-workers.sh`
  checks that, since import-linter cannot see outside the package.
- The controller: [architecture/controller.md](architecture/controller.md).
- The web layer: [architecture/web.md](architecture/web.md).
- The stored records: [architecture/data-model.md](architecture/data-model.md).

# Plan: configuration reading and the CLI

Goal: `robinauts start` reads a TOML file named by `ROBINAUTS_CONFIG`, builds the
controller from it, and serves the UI; `robinauts version` answers. Happy path only.

## Rules

- One branch per step, `feature/config-N`, from the previous step's branch. One commit
  per branch, pushed. No pull requests, no reviews between steps.
- Minimal code for the main flows. No edge cases, no defensive checks beyond what the
  step names. Comments only where the code does not say it; no docstrings unless a line
  cannot be read without one.
- The layer rules of `docs/architecture/rules.md` hold; `tests/unit/test_architecture.py`
  runs them. Adding a field to a contract dataclass is fine. A new class in a contract,
  or a new import direction, stops the work and asks the owner. `ConfigError` is already
  in the controller's contract for this plan.
- Before the commit, from `backend/`:
  `uv run ruff check --config pyproject.toml . ../scripts ../demo ../examples`,
  `uv run black --check --config pyproject.toml . ../scripts ../demo ../examples`,
  `uv run pytest tests/unit -q`, and `uvx reuse lint` from the root, all green. Every
  new file starts with the two licence header lines.
- Tests: what a step needs to prove its main flow. Nothing more.

## Steps

1. A reader under `controller/adapters` that takes a TOML path and returns the raw
   tables: `model_providers`, `models`, `tool_servers`, `agents`, as the demo's TOML
   names them.
2. A function under `controller/application` that turns the raw tables into the
   contract's `Config`, collecting every problem into one `ConfigError` raised at the
   end: an unknown key, a missing required field, a model on a provider not there, an
   agent on a model or a tool server not there, an engine name not in `langchain`,
   `pydantic-ai`, `echo`.
3. `composition.load(path, environ)` reads and parses, and answers the `Config` and the
   `secret_for` lookup. `web/__main__.py` uses it with `ROBINAUTS_CONFIG`; the echo
   configuration becomes `examples/echo.toml`.
4. `robinauts.web.cli`: `start` serves on the host and port given, `version` prints the
   distribution's version; the console script in `pyproject.toml` points at it;
   uvicorn and logging are set up there and nowhere else. `python -m robinauts.web`
   calls the same `start`.
5. Proof: a TOML file becomes the expected `Config`; one with three mistakes names all
   three; `robinauts version` answers; the shell started from `examples/echo.toml`
   shows a turn in the browser, as the echo plan's step 13 did.

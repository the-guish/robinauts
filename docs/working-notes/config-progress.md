# Configuration and CLI: what was verified

- A TOML file becomes the expected `Config`: `test_parse_config.py::test_builds_the_config`, and `test_composition_load.py` over `examples/echo.toml`.
- One with three mistakes names all three: `test_parse_config.py::test_names_every_problem`.
- `uv run robinauts version` prints `0.1.0`; `test_web_cli.py` runs it too.
- `ROBINAUTS_CONFIG=../examples/echo.toml uv run robinauts start` from `backend/` served the built UI on 127.0.0.1:8000.
- In Chromium, "hello config" sent from the composer answered "1 tool call" and "The tool said: hello config", with Echo as the agent and the model.
- No fix was needed.

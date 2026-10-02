# Layer references rules

Anything not allowed is forbidden. A rule names a package and covers everything
under it. `backend/pyproject.toml` enforces these; `tests/unit/test_architecture.py`
runs them.

## Top level

agent_engines -> nothing from robinauts
controller -> agent_engines.contract
web -> controller.contract, controller.composition

## Inside agent_engines

contract -> nothing from robinauts
echo_engine -> contract
langchain_engine -> contract + langchain deps + postgres
pydantic_ai_engine -> contract + pydantic ai deps + postgres
no engine imports another

## Inside controller

contract -> nothing from robinauts
ports -> contract
core -> contract, ports, agent_engines.contract; pure functions, no I/O
adapters -> contract, ports
application -> contract, ports, core, agent_engines.contract
composition -> contract, ports, core, adapters, application; imported by web alone

## Third-party libraries, where each may be imported

langchain, langchain_core, langchain_*, langgraph, langchain_mcp_adapters, langsmith -> langchain_engine
pydantic_ai, pydantic_graph, fastmcp, logfire, opentelemetry -> pydantic_ai_engine
anthropic, openai, mcp -> langchain_engine, pydantic_ai_engine
asyncpg, sqlite3, aiosqlite -> langchain_engine, pydantic_ai_engine, controller.adapters
httpx -> web, langchain_engine, pydantic_ai_engine
fastapi, starlette, uvicorn, ag_ui -> web

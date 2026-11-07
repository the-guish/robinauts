# Layer references rules

Anything not allowed is forbidden. A rule names a package and covers everything
under it. `backend/pyproject.toml` enforces these; `tests/tooling/test_architecture.py`
runs them.

## Top level

shared_domain -> nothing from robinauts; any package but agent_engines and filesystems may import it
agent_engines -> nothing from robinauts
filesystems -> nothing from robinauts
tools -> agent_engines.contract, filesystems.contract
controller -> shared_domain
worker -> controller.contract.broker, web.contract.broker, agent_engines.contract, tools.contract, filesystems.contract
web -> controller.contract
cli -> web, controller, worker, tools, filesystems, agent_engines.contract, all but web and controller.contract by cli.composition alone; imported by nothing

```mermaid
flowchart TD
    web --> controller
    worker -- "web/direct" --> controller
    worker --> tools
    worker --> agent_engines
    worker --> filesystems
    tools --> agent_engines
    tools --> filesystems
```

## Inside agent_engines

contract -> nothing from robinauts
echo_engine -> contract
langchain_engine -> contract + langchain deps + postgres
pydantic_ai_engine -> contract + pydantic ai deps + postgres
no engine imports another

## Inside tools

contract -> agent_engines.contract, filesystems.contract
each tool (sessions, basic_edit) -> contract, agent_engines.contract, filesystems.contract
no tool imports another; only cli.composition imports a tool

## Inside filesystems

contract -> nothing from robinauts
local -> contract
no adapter imports another; only cli.composition imports an adapter

## Inside controller

contract -> nothing from robinauts
contract.broker -> contract; contract never imports contract.broker
ports -> contract
core -> contract, ports; pure functions, no I/O
adapters -> contract, ports
application -> contract, ports, core

## Inside web

contract -> controller.contract; nothing else of web

## Third-party libraries, where each may be imported

langchain, langchain_core, langchain_*, langgraph, langchain_mcp_adapters, langsmith -> langchain_engine
pydantic_ai, pydantic_graph, fastmcp, logfire, logfire_api, opentelemetry -> pydantic_ai_engine
anthropic, openai, mcp -> langchain_engine, pydantic_ai_engine
asyncpg, sqlite3, aiosqlite -> langchain_engine, pydantic_ai_engine, controller.adapters
httpx -> web, worker.http_turn_broker, langchain_engine, pydantic_ai_engine
fastapi -> web, cli
starlette, ag_ui -> web
uvicorn -> cli
qrcodegen -> web

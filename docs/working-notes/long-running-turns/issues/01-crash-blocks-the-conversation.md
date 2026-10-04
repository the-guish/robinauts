# 01 — A crash mid-turn blocks the conversation until the lease runs out

## Summary

If the server process dies during a turn (crash, OOM kill, `kill -9`), the turn
stays `running` after the restart until its lease passes. That is the model's
`timeout_seconds` plus 60 s: 3 minutes by default, and hours when the timeout is
raised for long tasks. Until then:

- the conversation refuses new messages (409);
- Stop is refused (409, "runs in another process");
- re-attaching to the stream hangs, sending no bytes;
- the UI shows the answer as still running.

Once the lease has passed, the turn is shown as interrupted. What it had streamed,
for example the tool call, is no longer in the conversation.

## Setup (used by all the issues in this folder)

A local PostgreSQL, a Haiku agent on the LangChain engine, and a test-only slow
MCP tool.

```toml
# repro.toml (ROBINAUTS_CONFIG)
[model_providers.anthropic]
kind = "anthropic"
api_key_env = "ANTHROPIC_API_KEY"

[models.haiku]
provider = "anthropic"
name = "claude-haiku-4-5"
timeout_seconds = 600

[tool_servers.slow]
url = "http://127.0.0.1:9000/mcp"
auth = "none"
timeout_seconds = 900

[agents.assistant]
title = "Assistant"
system_prompt = "Use slow_echo when asked."
model = "haiku"
engine = "langchain"
tools = ["slow"]
```

```python
# slow_mcp.py: test only (fastmcp is in the backend's environment)
import asyncio
from fastmcp import FastMCP

mcp = FastMCP("slow")

@mcp.tool
async def slow_echo(text: str, seconds: float = 60) -> str:
    """Wait `seconds`, then return `text`."""
    await asyncio.sleep(seconds)
    return text

mcp.run(transport="http", host="127.0.0.1", port=9000)
```

```sh
export ROBINAUTS_CONFIG=repro.toml ROBINAUTS_DATABASE_URL=postgresql://… ANTHROPIC_API_KEY=…
python slow_mcp.py &
robinauts db init
robinauts start --dev-no-sign-in        # the UI needs frontend/dist built; curl works without it
```

## How to reproduce

1. Send: "Call slow_echo with seconds=300 and text=hello". Wait until the tool
   call shows in the answer.
2. `kill -9` the server process, then start it again.
3. Open the conversation, or check by hand:

   ```sh
   psql "$ROBINAUTS_DATABASE_URL" -c "select state, lease_until - now() from turns order by started_at desc limit 1"
   curl -s -X POST localhost:8000/api/conversations/$CONV/runs/$RUN/cancel             # 409
   curl -s -X POST localhost:8000/api/conversations/$CONV/turns \
        -H 'content-type: application/json' -d '{"text":"again","parent_id":"<last id>"}'  # 409
   ```

Observed on `main` (`0a60922`, with a timeout of 600 s):

- the turn is `running`, with 10 min 36 s of lease left;
- the cancel answers 409, `runs in another process`;
- a new message answers 409;
- re-attaching with `Last-Event-ID` hangs with no output.

After the lease passed, the conversation showed `ended_badly: interrupted`, and its
messages held the question alone.

## Why it happens

- **The lease is written once, sized to the whole turn, and never renewed.**
  `lease_until = now + timeout + LEASE_MARGIN` (`controller/application/controller.py:357`,
  margin 60 s at `:61`). Nothing extends or shortens it, so a dead runner looks
  alive until then.
- **Only a lease that has passed is ended.**
  `end_expired_turn` ends a turn only when `lease_until < now`
  (`controller/adapters/postgres/store.py:69-76`).
- **One running turn per conversation.**
  `start_turn` hits `turns_one_running_per_session` and raises `TurnActiveError`
  → 409 (`store.py:256-283`).
- **Stop only reaches tasks in the same process.**
  `InProcessDispatcher.cancel` returns false for a turn whose task is not in this
  process's dict (`controller/adapters/dispatch.py:32-38`). The restarted process
  has no such task, so `cancel_turn` raises "runs in another process"
  (`controller.py:320-321`).
- **The re-attached stream stays silent.**
  `watch_turn` waits for events that never come (`controller.py:409-441`). The web
  layer sends no keep-alive, and awaits the first event before it sends the
  response (`web/app.py:458-475`).
- **The partial answer lives in memory.**
  The runner keeps it in a local list (`controller/application/turns.py`, `parts`)
  and stores it only in `finish_turn`. Ending an expired turn updates the turn row
  alone, so what streamed survives only as `turn_events` rows, which expire after
  24 h (`turns.py:62`).

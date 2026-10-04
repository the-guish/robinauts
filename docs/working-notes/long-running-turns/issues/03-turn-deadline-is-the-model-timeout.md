# 03 — A turn cannot outlive one vendor call's timeout

## Summary

A model's `timeout_seconds` (default 120 s) is three things at once:

- the timeout of each HTTP call to the vendor;
- the deadline of the **whole turn**, tool calls included;
- the turn's lease.

So any turn longer than 120 s fails, even when every single call is quick. The
only lever is to raise `timeout_seconds`. That also lets one hung vendor call hang
for that long, and makes [01](01-crash-blocks-the-conversation.md) last that long:
at 10800 s, a crash blocks the conversation for 3 hours.

Each tool call has a cap of its own, the tool server's `timeout_seconds`
(default 60 s).

## How to reproduce

Setup as in [01](01-crash-blocks-the-conversation.md#setup-used-by-all-the-issues-in-this-folder),
with the setting changed:

1. Remove `timeout_seconds` from `[models.haiku]`, so the default of 120 s applies.
2. Send: "Call slow_echo with seconds=150 and text=hello".
3. At about 120 s the turn ends with `RUN_ERROR {"code": "failed"}`, "the agent
   could not finish this answer". The tool call was fine. The engine raised a
   bare `TimeoutError`, so `turns.error` is empty.
4. Set `timeout_seconds = 3600` and repeat: the turn now succeeds. Kill the server
   mid-tool, though, and the conversation is blocked for 61 minutes (01).

## Why it happens

- **The deadline comes from the model.**
  `run_turn` derives it from the lease and the model's timeout:
  `timeout_seconds=min(model_timeout, remaining)`
  (`controller/application/turns.py:143,162`).
- **Each engine enforces it over the whole run.**
  `asyncio.timeout_at(deadline)` covers every step
  (`agent_engines/langchain_engine/engine.py`, `pydantic_ai_engine/engine.py`).
  Its `TimeoutError` ends the turn as failed.
- **The lease comes from the same number.**
  `lease_until = now + timeout + 60 s` (`controller/application/controller.py:357`).
- **The same number is also each vendor call's timeout.**
  The clients are built with `timeout=model.timeout_seconds`
  (`agent_engines/langchain_engine/clients.py:44,52`,
  `pydantic_ai_engine/clients.py:43-55`).
- **The defaults:** `ModelConfig.timeout_seconds = 120.0` and
  `ToolServerConfig.timeout_seconds = 60.0`
  (`agent_engines/contract/domain.py:62,82`).

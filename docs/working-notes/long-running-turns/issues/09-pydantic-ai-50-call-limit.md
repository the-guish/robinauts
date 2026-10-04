# 09 — Agents on the Pydantic AI engine stop after 50 model calls

## Summary

A turn on the `pydantic-ai` engine fails when it needs more than 50 requests to the
model: every round of tool calls is one more request. A long agentic task, such as
many tool calls one after another, hits this limit and ends as failed. Agents on
the `langchain` engine are not affected: `create_agent` sets LangGraph's recursion
limit to 9 999.

## How to reproduce

Setup as in [01](01-crash-blocks-the-conversation.md#setup-used-by-all-the-issues-in-this-folder),
with `engine = "pydantic-ai"` in `[agents.assistant]`.

1. Send: "Call slow_echo 60 times, one call at a time, waiting for each result
   before the next, with seconds=0 and text set to the call's number. Then say
   done."
2. After the 50th model request, the turn ends with
   `RUN_ERROR {"code": "failed"}`. `turns.error` holds
   `The next request would exceed the request_limit of 50`.

A model may batch several calls into one request. The instruction "one at a time,
waiting for each result" keeps it to one call per request.

## Why it happens

- **Pydantic AI's default limit is 50.** `UsageLimits.request_limit` defaults to
  50, and the run raises `UsageLimitExceeded` at the next request
  (`pydantic_ai/usage.py:457,523-524` in pydantic-ai-slim 2.47).
- **The engine never overrides it.** It runs the agent with
  `runner.iter(prompt, message_history=history)`, without `usage_limits`
  (`agent_engines/pydantic_ai_engine/engine.py:120`), so the default applies to
  every turn.

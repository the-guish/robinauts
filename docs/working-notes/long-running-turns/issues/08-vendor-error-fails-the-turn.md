# 08 — One vendor error fails the whole turn

## Summary

A single transient error from the model vendor ends the turn as failed:

- a dropped connection;
- a 429 (rate limited);
- a 529 or 503 (overloaded).

Nothing retries it. For a chat answer this is a rare annoyance. For a task that
has run for an hour, one blip throws the hour away, and Retry starts it over at
full cost.

## How to reproduce

Setup as in [01](01-crash-blocks-the-conversation.md#setup-used-by-all-the-issues-in-this-folder).

1. Ask for something long, so that Haiku streams for a while, or for a few
   `slow_echo` calls with `seconds=5` each.
2. While it runs, cut this machine's network for 2–3 s (switch Wi-Fi off and on,
   or `sudo ip link set <iface> down; sleep 3; sudo ip link set <iface> up`), so
   that the next or current model call fails.
3. The turn ends with `RUN_ERROR {"code": "failed"}` at once. `turns.error` holds
   the SDK's connection error.

A deterministic variant shows that no retry happens. Point the provider at a port
where nothing listens: `kind = "anthropic-compatible"`,
`base_url = "http://127.0.0.1:9"`. Each turn fails on the first refused connection,
with no backoff.

## Why it happens

- **Both engines build the vendor client with retries off.**
  `max_retries=0` in `agent_engines/langchain_engine/clients.py:45,53` and in
  `agent_engines/pydantic_ai_engine/clients.py:48,55`. The SDKs' own default is 2.
- **No retry anywhere else.** No retry middleware is configured on the LangGraph
  agent (`create_agent` gets no `middleware`), and Pydantic AI has no retry
  wrapper. The first exception propagates to the runner, which ends the turn as
  failed (`controller/application/turns.py`, the `except Exception` branch of
  `run_turn`).

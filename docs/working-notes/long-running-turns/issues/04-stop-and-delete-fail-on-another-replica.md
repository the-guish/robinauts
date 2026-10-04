# 04 — Stop and delete fail when the request reaches another replica

## Summary

With two or more server processes behind a load balancer, a turn runs in the
process that received its POST. If a later request lands on a different process:

- **Stop** is refused with 409 ("runs in another process"). The UI says "The stop
  did not reach the server, so the answer is still arriving".
- **Delete** of that conversation is refused with 409 while the turn runs.

With two replicas, that is about half of all Stops and deletes. Streaming is not
affected: any process can serve a turn's events.

## How to reproduce

Setup as in [01](01-crash-blocks-the-conversation.md#setup-used-by-all-the-issues-in-this-folder),
with two servers on the same database:

```sh
robinauts start --dev-no-sign-in --port 8000 &
robinauts start --dev-no-sign-in --port 8001 &
```

1. In a tab on `http://127.0.0.1:8000`, send "Call slow_echo with seconds=120 and
   text=hello".
2. In a tab on `http://127.0.0.1:8001`, open the same conversation (the local mode
   has one user, so it is listed in both). It shows the turn running.
3. Press Stop on 8001: 409, `{"error":"TurnActiveError","detail":"turn … runs in
   another process"}`.
4. Delete the conversation on 8001: 409, until the turn ends on 8000.

## Why it happens

- **Turns run as asyncio tasks of the process that took the request.**
  `InProcessDispatcher` keeps them in a dict local to that process
  (`controller/adapters/dispatch.py`).
- **Stop only cancels a local task.**
  `cancel` returns false for a turn it does not hold (`dispatch.py:32-38`), and
  `cancel_turn` turns that into `TurnActiveError` → 409
  (`controller/application/controller.py:320-321`).
- **Delete refuses while a turn runs.**
  `delete_session` cancels only a local turn (`controller.py:193-201`), then
  `hide_session` refuses with `TurnActiveError` while any turn is `running`
  (`controller/adapters/postgres/store.py:224-238`).
- **No cancel goes through the database.** `turns.cancel_requested_at` exists in
  the schema, but nothing writes or reads it, and no notification reaches the
  owning process.
- **The UI cannot tell this refusal apart.** It maps every failed cancel to "the
  stop did not reach the server" (`frontend/src/chat/assistant-ui/state.ts:254`).

# 02 — A crashed turn stays `running` until someone opens its conversation

## Summary

After a crash, a turn whose lease has passed is not ended by anything running in
the background. It stays `state = 'running'` in the database, for days if nobody
opens that conversation. Anything that reads the `turns` table sees work that is
not happening: an operator's query, a future "running" badge, a future cap on
running turns.

## How to reproduce

Setup as in [01](01-crash-blocks-the-conversation.md#setup-used-by-all-the-issues-in-this-folder).

1. Start a turn with a slow tool call, then `kill -9` the server and restart it.
2. Do **not** open the conversation, and make no request about it.
3. After the lease has passed (the model's `timeout_seconds` + 60 s), run:

   ```sh
   psql "$ROBINAUTS_DATABASE_URL" -c \
     "select id, state, lease_until < now() as lease_passed from turns where state = 'running'"
   ```

   The turn is still `running`, with `lease_passed = t`. Opening the conversation
   ends it as `interrupted` at that moment.

## Why it happens

- **Only readers end expired turns.** `end_expired_turn` is called from request
  paths alone (`controller/application/controller.py`):
  - opening a conversation (`:170`);
  - deleting it (`:195`);
  - cancelling (`:316`);
  - starting a turn (`:348`);
  - a watcher timing out (`:434`).
- **Nothing runs on a schedule.** `Controller.sweep()` is not implemented
  (`controller.py:443-444`). The index `turns_lease_until_idx` that a sweep would
  use exists, but nothing reads it.

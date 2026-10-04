# 12 — Without a database URL, the server quietly runs on in-memory storage

## Summary

`robinauts start` with a sign-in configuration but no `ROBINAUTS_DATABASE_URL`
starts normally and serves on **in-memory** storage. It logs no warning.
Conversations, users and sessions vanish at the next restart. With several
replicas, one pod missing the variable has a private data set of its own: people
routed to it see different conversations from everyone else. The deployment guide
says start-up refuses in this case.

## How to reproduce

1. Make a configuration with a sign-in provider. Dummy values are enough to start:

   ```toml
   public_url = "http://127.0.0.1:8000"

   [providers.google]
   title = "Google"
   issuer = "https://accounts.google.com"
   client_id = "dummy"
   client_secret_env = "DUMMY_SECRET"

   [[allow]]
   provider = "google"
   hosted_domain = "example.com"

   # plus the model, provider and agent tables, as in issue 01
   ```

2. Start it **without** the database URL:

   ```sh
   unset ROBINAUTS_DATABASE_URL
   ROBINAUTS_CONFIG=signin.toml DUMMY_SECRET=x robinauts start
   ```

3. It logs "Application startup complete" and serves, with nothing said about
   storage. Observed on `main` (`0a60922`).

## Why it happens

- **Storage falls back silently.** `storage_from` chooses PostgreSQL when
  `ROBINAUTS_DATABASE_URL` is set, and in-memory otherwise
  (`controller/composition/__init__.py:49-54`).
- **Start-up never checks.** `serving()` validates the sign-in configuration and
  the controller's tables, but not the storage (`web/cli.py:74-111`). `start` then
  composes the controller from `storage_from(os.environ)` (`web/cli.py:125`).
- **The docs say otherwise.** `docs/deployment.md:541` lists "no database: set
  ROBINAUTS_DATABASE_URL…" among the start-up refusals, which the code does not
  have.

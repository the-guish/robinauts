-- SPDX-License-Identifier: Apache-2.0
-- Copyright The Robinauts Authors
--
-- The controller's store on SQLite, in one file: `robinauts.db` in the data
-- directory. It renders the model of docs/architecture/data-model.md, as
-- `postgres/schema.sql` does, and the comments there explain each table. This
-- file says only where SQLite differs.
--
-- It holds the store's tables alone. Sign-in, sessions, tokens and devices
-- are oneberth's, in `oneberth.db` beside this file.
-- The engines keep their state apart, and nothing here references it.
--
-- The store applies this file itself, to an empty file, in one transaction.
-- The backend's process alone opens the file, and no operator runs a command
-- first. A file of another version, or made from another edit of this file,
-- is refused. Until the first release the version stays 1 and this file is
-- edited in place, like the PostgreSQL schema.
--
-- Conventions:
--
--   * every table is STRICT, so a column holds the type it names.
--   * an id is a uuid as lowercase text, which sorts as the uuid does.
--   * a time is UTC text of one width, `2026-10-02T12:00:00.000000Z`, which
--     sorts as the instant does. The store writes and compares it.
--   * a document, a payload and a list of groups are JSON text, kept whole and
--     never read here.
--   * the foreign keys hold only on a connection with `PRAGMA foreign_keys`
--     on, which the store sets. The store checks each rule itself first, as
--     the port promises; the keys are a second guard.


-- One row, for ever. `schema_sha256` is written by the store right after
-- this file, since a file cannot hold its own hash.
CREATE TABLE schema_version (
    only_row INTEGER NOT NULL
        CONSTRAINT schema_version_pkey PRIMARY KEY
        CONSTRAINT schema_version_only_row CHECK (only_row = 1),
    version INTEGER NOT NULL,
    schema_sha256 TEXT
        CONSTRAINT schema_version_sha256_is_a_hash
            CHECK (length(schema_sha256) = 64 AND schema_sha256 NOT GLOB '*[^0-9a-f]*'),
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
) STRICT;


CREATE TABLE users (
    id TEXT NOT NULL
        CONSTRAINT users_pkey PRIMARY KEY,
    provider TEXT NOT NULL,
    subject TEXT NOT NULL,
    name TEXT,
    email TEXT,
    created_at TEXT NOT NULL,
    hosted_domain TEXT,
    -- A JSON array of text.
    groups TEXT NOT NULL DEFAULT '[]',
    change_version INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT users_provider_subject_key UNIQUE (provider, subject)
) STRICT;


CREATE TABLE projects (
    id TEXT NOT NULL
        CONSTRAINT projects_pkey PRIMARY KEY,
    owner_id TEXT NOT NULL
        CONSTRAINT projects_owner_id_fkey REFERENCES users (id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CONSTRAINT projects_owner_id_name_key UNIQUE (owner_id, name),
    CONSTRAINT projects_owner_id_id_key UNIQUE (owner_id, id)
) STRICT;


CREATE TABLE sessions (
    id TEXT NOT NULL
        CONSTRAINT sessions_pkey PRIMARY KEY,
    owner_id TEXT NOT NULL
        CONSTRAINT sessions_owner_id_fkey REFERENCES users (id) ON DELETE CASCADE,
    project_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    engine TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    archived_at TEXT,
    CONSTRAINT sessions_project_fkey FOREIGN KEY (owner_id, project_id)
        REFERENCES projects (owner_id, id)
) STRICT;

CREATE INDEX sessions_project_idx
    ON sessions (project_id);

CREATE INDEX sessions_listing_idx
    ON sessions (owner_id, updated_at DESC, id DESC);

CREATE INDEX sessions_deleted_at_idx
    ON sessions (deleted_at, id) WHERE deleted_at IS NOT NULL;


CREATE TABLE messages (
    id TEXT NOT NULL
        CONSTRAINT messages_pkey PRIMARY KEY,
    session_id TEXT NOT NULL
        CONSTRAINT messages_session_id_fkey REFERENCES sessions (id) ON DELETE CASCADE,
    parent_id TEXT,
    role TEXT NOT NULL
        CONSTRAINT messages_role_is_a_role CHECK (role IN ('user', 'assistant', 'tool')),
    created_at TEXT NOT NULL,
    document TEXT NOT NULL,
    CONSTRAINT messages_session_id_id_key UNIQUE (session_id, id),
    CONSTRAINT messages_parent_id_fkey FOREIGN KEY (session_id, parent_id)
        REFERENCES messages (session_id, id)
) STRICT;

CREATE INDEX messages_session_id_created_at_idx
    ON messages (session_id, created_at, id);

CREATE INDEX messages_session_id_parent_id_idx
    ON messages (session_id, parent_id);


CREATE TABLE tasks (
    id TEXT NOT NULL
        CONSTRAINT tasks_pkey PRIMARY KEY,
    task_name TEXT NOT NULL,
    payload TEXT NOT NULL,
    state TEXT NOT NULL
        CONSTRAINT tasks_state_is_a_state CHECK (state IN ('queued', 'running', 'done')),
    created_at TEXT NOT NULL,
    lease_until TEXT NOT NULL,
    claimed_at TEXT,
    cancel_requested_at TEXT,
    worker_queue TEXT NOT NULL
) STRICT;

CREATE INDEX tasks_queued_idx
    ON tasks (worker_queue, created_at, id) WHERE state = 'queued';

CREATE INDEX tasks_lease_until_idx
    ON tasks (lease_until) WHERE state <> 'done';


CREATE TABLE turns (
    id TEXT NOT NULL
        CONSTRAINT turns_pkey PRIMARY KEY,
    session_id TEXT NOT NULL
        CONSTRAINT turns_session_id_fkey REFERENCES sessions (id) ON DELETE CASCADE,
    follows TEXT NOT NULL,
    model TEXT NOT NULL,
    state TEXT NOT NULL
        CONSTRAINT turns_state_is_a_state CHECK (
            state IN ('active', 'finished', 'failed', 'cancelled', 'interrupted')
        ),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    error TEXT,
    task_id TEXT NOT NULL
        CONSTRAINT turns_task_id_fkey REFERENCES tasks (id)
        CONSTRAINT turns_task_id_key UNIQUE,
    retries_message_id TEXT,
    CONSTRAINT turns_follows_fkey FOREIGN KEY (session_id, follows)
        REFERENCES messages (session_id, id),
    CONSTRAINT turns_retries_fkey FOREIGN KEY (session_id, retries_message_id)
        REFERENCES messages (session_id, id),
    CONSTRAINT turns_ended_when_not_active CHECK (
        (state = 'active') = (ended_at IS NULL)
    ),
    CONSTRAINT turns_error_only_when_failed CHECK (
        error IS NULL OR state IN ('failed', 'interrupted')
    )
) STRICT;

CREATE UNIQUE INDEX turns_one_active_per_session
    ON turns (session_id) WHERE state = 'active';

CREATE INDEX turns_session_id_started_at_idx
    ON turns (session_id, started_at DESC, id DESC);


CREATE TABLE turn_events (
    turn_id TEXT NOT NULL
        CONSTRAINT turn_events_turn_id_fkey REFERENCES turns (id) ON DELETE CASCADE,
    position INTEGER NOT NULL
        CONSTRAINT turn_events_position_from_one CHECK (position >= 1),
    document TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    CONSTRAINT turn_events_pkey PRIMARY KEY (turn_id, position)
) STRICT;

CREATE INDEX turn_events_expires_at_idx
    ON turn_events (expires_at);


-- The version row, last: a file that stopped half way records no version.
INSERT INTO schema_version (only_row, version) VALUES (1, 1);

-- SPDX-License-Identifier: Apache-2.0
-- Copyright The Robinauts Authors
--
-- The controller's schema on PostgreSQL, in one file.
--
-- This is the controller's alone. The engines keep their memory in tables of
-- their own, made by their own `setup`, and nothing here references them
-- (docs/specs/agent-engines.md). Sign-in's tables (`user_sessions` and the
-- pending logins) are added to this file with sign-in.
--
-- A table comes after every table it references, since the file is applied
-- from the top.
--
-- Until the first release this file is edited in place and there are no
-- migrations: the version stays 1, and a database made from an older edit is
-- dropped and made again. It is applied by a command, never by the server,
-- in one transaction, so that a failure half way leaves nothing behind. By
-- hand, that transaction has to be asked for:
--
--     psql -v ON_ERROR_STOP=1 --single-transaction -f schema.sql
--
-- Conventions, as legacy's schema had them:
--
--   * every id is a uuid the application mints, and every time is a
--     `timestamptz` the application sets. No default reads the clock or makes
--     an id: a store keeps no clock and no id source. The one `now()` below
--     dates the schema itself, which is not a decision any code makes.
--   * a document is the controller's versioned JSON, kept whole and never
--     read here. The columns beside it are what the store orders, filters,
--     joins or decides by.
--   * every constraint is named, and the names are interface: the store
--     translates a violation into its caller's error by the constraint's name.
--   * every column a sweep deletes by is indexed.
--   * no rule depends on a cascade or a trigger alone. The store port carries
--     out what it promises, so that a store without foreign keys can keep the
--     same promises (docs/working-notes/oct-refactor/aws-serverless.md). The
--     keys here are a second guard, not the guarantee.


-- ---------------------------------------------------------------------------
-- The schema's own version.
-- ---------------------------------------------------------------------------

-- One row, for ever: `only_row` is a boolean primary key that must be true,
-- so a second row cannot be inserted and the version cannot become ambiguous.
-- The row itself is inserted at the bottom of this file, after every table,
-- so that a file that stopped half way records no version at all.
--
-- `schema_sha256` is the SHA-256 of this file as the command applied it,
-- written by the command right after the file, since a file cannot hold its
-- own hash. Until the first release the version is 1 whatever edit of this
-- file a database was made from, so the hash is what tells an older edit
-- apart: the server refuses a database whose hash is not the build's, and
-- says to make it again. NULL is a file applied by hand, refused the same way.
CREATE TABLE IF NOT EXISTS schema_version (
    only_row boolean
        CONSTRAINT schema_version_pkey PRIMARY KEY DEFAULT true
        CONSTRAINT schema_version_only_row CHECK (only_row),
    version integer NOT NULL,
    schema_sha256 text
        CONSTRAINT schema_version_sha256_is_a_hash CHECK (schema_sha256 ~ '^[0-9a-f]{64}$'),
    applied_at timestamptz NOT NULL DEFAULT now()
);


-- ---------------------------------------------------------------------------
-- Users.
-- ---------------------------------------------------------------------------

-- A person, as an identity provider names them. Created on first sight by
-- `ensure_user`; a terminal names the operating system's user the same way,
-- with its own provider.
--
-- Keyed by `(provider, subject)`: the same address at two providers is two
-- users, and a provider that reassigns an address does not hand over an
-- account. Two first sign-ins of one person at once both insert, and the
-- unique constraint makes one of them fail; the store reads that, by name, as
-- "already there" and returns the row the other wrote.
--
-- `name` and `email` are what the provider said, and may be null.
--
-- Deleting a user deletes their sessions, by the cascade on
-- `sessions.owner_id`. The cascade does not reach the engines, so whatever
-- deletes a user purges their sessions first, calling `forget` for each.
CREATE TABLE IF NOT EXISTS users (
    id uuid
        CONSTRAINT users_pkey PRIMARY KEY,
    provider text NOT NULL,
    subject text NOT NULL,
    name text,
    email text,
    created_at timestamptz NOT NULL,
    CONSTRAINT users_provider_subject_key UNIQUE (provider, subject)
);


-- ---------------------------------------------------------------------------
-- Sessions.
-- ---------------------------------------------------------------------------

-- A session: one conversation of one owner with one agent
-- (docs/architecture/controller.md).
--
-- `agent` is an id of the configuration, not a foreign key: the operator may
-- remove the agent, and the session keeps its row. `engine` is the engine that
-- holds the session's memory, the agent's when the session was made: a turn
-- runs on it and the purge forgets on it, whatever the configuration names
-- today. There is no `model`: the model travels with each turn and is recorded
-- on the turn and on the messages it produces (docs/specs/wire.md).
--
-- There is no pointer to the running turn. "At most one running turn" is the
-- partial unique index on `turns` below, and what is running is looked up
-- there.
--
-- `deleted_at` hides a session. It is set only while no turn is running, under
-- a lock on the row taken first, so that no runner and no engine writes under
-- a purge. From the moment it is set, every read treats the session as not
-- found and no turn may start on it. The purge then calls `forget` on the
-- engine the row names and deletes the row, and the cascade takes its
-- messages and turns with it. Trash, in stage two, is a delay before the
-- purge, not a change of schema.
--
-- `owner_id` cascades: a user's sessions are private to them, and there is
-- nobody else for them to belong to once the user is gone.
CREATE TABLE IF NOT EXISTS sessions (
    id uuid
        CONSTRAINT sessions_pkey PRIMARY KEY,
    owner_id uuid NOT NULL
        CONSTRAINT sessions_owner_id_fkey REFERENCES users (id) ON DELETE CASCADE,
    agent text NOT NULL,
    engine text NOT NULL,
    title text NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    deleted_at timestamptz
);

-- The listing: one owner's sessions, most recently updated first, ties broken
-- by id so that the order is total and a keyset page, whose cursor is
-- `(updated_at, id)`, neither repeats nor skips one. Not partial on
-- `deleted_at`: a hidden session is rare and short-lived, and the cascade from
-- `users` needs `owner_id` indexed for every row.
CREATE INDEX IF NOT EXISTS sessions_listing_idx
    ON sessions (owner_id, updated_at DESC, id DESC);

-- What the purge reads: the hidden sessions, oldest first.
CREATE INDEX IF NOT EXISTS sessions_deleted_at_idx
    ON sessions (deleted_at, id) WHERE deleted_at IS NOT NULL;


-- ---------------------------------------------------------------------------
-- Messages.
-- ---------------------------------------------------------------------------

-- One message of a session: a node of its tree. A question is stored before
-- its turn starts, and an answer when its turn finishes. A turn that fails
-- leaves no answer.
--
-- `document` is the whole message in the controller's versioned format
-- (docs/architecture/data-model.md): the fields below again, its parts (text,
-- reasoning, tool calls with their arguments, tool results), the agent and
-- the model, the engine's checkpoint id on an answer, and the turn that
-- produced an answer. The columns are what the store orders, joins and checks
-- by, written from the same record in the same statement; the decoder reads
-- the document alone.
--
-- The id is a primary key, so it is unique across the deployment and not
-- merely within a session.
--
-- `parent_id` is a message **of the same session**: the composite key points
-- `(session_id, parent_id)` at `(session_id, id)`, so that a parent from
-- another session's tree cannot be stored. A null `parent_id` is a root, and
-- the key does not apply to it (MATCH SIMPLE). The key needs a unique
-- constraint over exactly those two columns, which `messages_session_id_id_key`
-- is; the primary key implies it, but PostgreSQL wants one declared. `turns`
-- points at it too.
--
-- `parent_id` takes no action on delete: a message is never deleted alone,
-- only with its session, whose cascade removes the whole tree in one
-- statement. Deleting one message under which others hang is refused, which
-- is what an edit, a new message under an earlier parent, never needs.
--
-- `role` is a column because it is what the message is, not what it says.
-- `tool` is reserved: no message has it today, and the spelling is settled
-- so that no column changes the day one does.
CREATE TABLE IF NOT EXISTS messages (
    id uuid
        CONSTRAINT messages_pkey PRIMARY KEY,
    session_id uuid NOT NULL
        CONSTRAINT messages_session_id_fkey REFERENCES sessions (id) ON DELETE CASCADE,
    parent_id uuid,
    role text NOT NULL
        CONSTRAINT messages_role_is_a_role CHECK (role IN ('user', 'assistant', 'tool')),
    created_at timestamptz NOT NULL,
    document jsonb NOT NULL,
    CONSTRAINT messages_session_id_id_key UNIQUE (session_id, id),
    CONSTRAINT messages_parent_id_fkey FOREIGN KEY (session_id, parent_id)
        REFERENCES messages (session_id, id)
);

-- What opening a session reads: every message of it, oldest first, ties
-- broken by id so that two stores hand back one order. It is also what the
-- cascade from `sessions` deletes by.
CREATE INDEX IF NOT EXISTS messages_session_id_created_at_idx
    ON messages (session_id, created_at, id);

-- A message's children: the answers to a question, and the edits under one
-- message. It is also what the parent key checks when a session's tree is
-- deleted, which would otherwise read the whole session once for every
-- message.
CREATE INDEX IF NOT EXISTS messages_session_id_parent_id_idx
    ON messages (session_id, parent_id);


-- ---------------------------------------------------------------------------
-- Turns.
-- ---------------------------------------------------------------------------

-- A turn: one question, and everything done to answer it. Its id is the run
-- id on the wire, and its events are keyed by it, so their positions start at
-- 1 with each turn and never collide.
--
-- `follows` is the question the turn answers. With `session_id`, it points at
-- `messages (session_id, id)`, so that a question of another session cannot be
-- answered here. A question has any number of turns: its first, each
-- regeneration, and those that failed. The answer is not referenced: it is
-- stored when the turn finishes, and it names its turn in its own document. A
-- turn has one answer, or none if it did not finish.
--
-- That the question is a *user* message is the store's rule, since a foreign
-- key cannot say "and its role is user". The question and its turn are
-- written in one transaction, so a question refused a turn is not left behind.
--
-- `model` is the model the turn runs on. The question carries the one it was
-- asked with, but a regeneration answers the same question on another model,
-- so the turn records its own.
--
-- `lease_until` is written with the turn, as its start plus its timeout and a
-- margin. Every write of the runner's is refused past it, and a running turn
-- whose lease has passed was left by a runner that went away: the next reader
-- to find it ends it as `interrupted`, with no event. Renewing the lease for a
-- long turn, and `cancel_requested_at`, which a cancel from another process
-- will set and the runner read back, are stage two. Both are set by the
-- application's clock.
CREATE TABLE IF NOT EXISTS turns (
    id uuid
        CONSTRAINT turns_pkey PRIMARY KEY,
    session_id uuid NOT NULL
        CONSTRAINT turns_session_id_fkey REFERENCES sessions (id) ON DELETE CASCADE,
    follows uuid NOT NULL,
    model text NOT NULL,
    -- The states of robinauts.controller.contract.domain.TurnState. Which may
    -- follow which is a rule above this layer; what a column can say is that
    -- nothing else is a state.
    state text NOT NULL
        CONSTRAINT turns_state_is_a_state CHECK (
            state IN ('running', 'finished', 'failed', 'cancelled', 'interrupted')
        ),
    started_at timestamptz NOT NULL,
    ended_at timestamptz,
    -- For the operator. Never sent to the browser, which is told a fixed
    -- sentence (docs/specs/wire.md).
    error text,
    lease_until timestamptz NOT NULL,
    cancel_requested_at timestamptz,
    CONSTRAINT turns_follows_fkey FOREIGN KEY (session_id, follows)
        REFERENCES messages (session_id, id),
    -- A turn has ended exactly when it is no longer running.
    CONSTRAINT turns_ended_when_not_running CHECK (
        (state = 'running') = (ended_at IS NULL)
    ),
    -- Only a turn that ended badly says why.
    CONSTRAINT turns_error_only_when_failed CHECK (
        error IS NULL OR state IN ('failed', 'interrupted')
    )
);

-- **At most one running turn per session.** Two requests arriving together
-- both insert, and only this index makes one of them fail. The store
-- translates a violation of it, by name, into `TurnActiveError`. It is also
-- what `active_turn` reads. Partial: a session has any number of turns that
-- have ended.
CREATE UNIQUE INDEX IF NOT EXISTS turns_one_running_per_session
    ON turns (session_id) WHERE state = 'running';

-- What opening a session reads to say how its last turn ended (`ended_badly`),
-- and what the cascade from `sessions` deletes by: a session's turns, most
-- recent first, ties broken by id.
CREATE INDEX IF NOT EXISTS turns_session_id_started_at_idx
    ON turns (session_id, started_at DESC, id DESC);

-- What the sweep reads: the running turns whose lease has passed.
CREATE INDEX IF NOT EXISTS turns_lease_until_idx
    ON turns (lease_until) WHERE state = 'running';


-- ---------------------------------------------------------------------------
-- Turn events.
-- ---------------------------------------------------------------------------

-- What a turn published, numbered: the pieces a watcher streams and
-- re-attaches to. `document` is the event in the controller's versioned
-- format, kept whole and never read here.
--
-- Events are a replay log, not the record. Once a turn has ended, its answer
-- is in `messages` and its outcome is in `turns`, and nothing reads its events
-- again but a late watcher. So every event expires: `expires_at` is set by the
-- application when the event is written, as that moment plus the retention
-- (hours, not days), and the sweep deletes what has passed it. Ending a turn
-- touches none of its events. Until they expire, the events are the only copy
-- of what a turn's reasoning said and of what a turn that failed had
-- streamed, which is why the retention is short.
--
-- The primary key is `(turn_id, position)`. The runner numbers its turn's
-- events, starting at 1, and is their only writer. The same document offered
-- again at a position it has is the runner's own write, acknowledged late, and
-- is accepted; another document there means two runners on one turn, and the
-- store translates the key's violation, by name, into `TurnLostError` instead
-- of renumbering. It is also the index `events_after` reads: one turn's
-- events, in order, past a position.
--
-- There is no `kind` column. "A turn ends once" is held by `finish_turn`, which
-- changes only a running turn whose lease has not passed, in the same
-- transaction that writes its last events; a turn ended by its lease
-- (`end_expired_turn`) writes no event at all, and a watcher reads the record.
-- So no index on the event's kind is needed to hold it.
CREATE TABLE IF NOT EXISTS turn_events (
    turn_id uuid NOT NULL
        CONSTRAINT turn_events_turn_id_fkey REFERENCES turns (id) ON DELETE CASCADE,
    position integer NOT NULL
        CONSTRAINT turn_events_position_from_one CHECK (position >= 1),
    document jsonb NOT NULL,
    expires_at timestamptz NOT NULL,
    CONSTRAINT turn_events_pkey PRIMARY KEY (turn_id, position)
);

-- What the sweep deletes by: the events past their expiry, oldest first.
CREATE INDEX IF NOT EXISTS turn_events_expires_at_idx
    ON turn_events (expires_at);


-- ---------------------------------------------------------------------------
-- The version row, last.
-- ---------------------------------------------------------------------------

-- Inserted only if there is none. Overwriting one would stamp this version on
-- an older edit's tables, which `CREATE TABLE IF NOT EXISTS` above left as
-- they were, and every check afterwards would pass. Whether this database may
-- be written to at all is decided by the command before this file runs.
INSERT INTO schema_version (version) VALUES (1)
ON CONFLICT (only_row) DO NOTHING;

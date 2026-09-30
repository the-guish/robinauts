-- SPDX-License-Identifier: Apache-2.0
-- Copyright The Robinauts Authors
--
-- The whole schema of a Robinauts deployment, in one file.
--
-- Until the first release this file is **edited in place** and there are no
-- incremental migrations (docs/specs/backend.md): the version stays 1, and
-- a database made from an older edit is dropped and made again with
-- `robinauts db init`. `SCHEMA_VERSION` in schema.py is what a server
-- compares against the row in `schema_version` before it agrees to start.
--
-- It is applied by a command (`robinauts db init`), never by the server
-- itself, and it is applied in one transaction: the whole file goes in as
-- one simple query, so a failure half way leaves nothing behind. Applying it
-- by hand needs that transaction asked for explicitly:
--
--     psql -v ON_ERROR_STOP=1 --single-transaction -f schema.sql
--
-- Without both flags psql runs the statements one by one and keeps going
-- after an error, which is exactly the half-applied database the version row
-- at the bottom of this file is placed to expose. A database made that way
-- records no `schema_sha256` (below), and the server refuses it: the command
-- is what makes a database the server will run against.
--
-- **Editing this file means updating `SCHEMA_SHA256` in schema.py**, and
-- nothing else: a test pins the SHA-256 of this file, so every edit is
-- deliberate and visible in review, and the version is not bumped. After
-- the first release a change is a migration; freezing the file as released
-- comes with that work.
--
-- A later step adds the usage tables to the bottom of this file, as the
-- conversation and run tables were added. Keep each table's block
-- self-contained -- the table, its comments, then its indexes -- so that
-- adding one is an addition and not an edit.
--
-- Conventions:
--
--   * every point in time is `timestamptz`. A `timestamp` would be a wall
--     clock with no zone, read as one thing by the server and another by
--     the process, and an expiry that means two things is no expiry.
--   * **no default reads the clock for a decision.** Expiry times and
--     creation times are computed by the application from its own `Clock`
--     port and passed in, so that one clock decides what has expired
--     (docs/specs/backend.md, "Where expiry is involved, one clock
--     decides"). The one `now()` below dates the schema itself, which is
--     not a decision any code makes.
--   * anything looked up by a secret is looked up by the SHA-256 of it, in
--     lower-case hex, and the column says so with a CHECK. The secret
--     itself is never stored (ports/credentials.py). The store checks the
--     same shape in Python before it writes, because a constraint only runs
--     when a row is really inserted and there are statements that decline
--     to insert one; this is the backstop under that, for the row nobody
--     went through the store to write.
--   * every column a sweep deletes by is indexed.
--
-- The statements are idempotent where that is honest -- `IF NOT EXISTS` on
-- what can simply already be there -- so that applying the file twice is a
-- no-op rather than an error. It is not a migration tool: it will not
-- reshape a table that exists with the wrong columns, which is what the
-- version check is for.


-- ---------------------------------------------------------------------------
-- The schema's own version.
-- ---------------------------------------------------------------------------

-- One row, for ever: `only_row` is a boolean primary key that must be true,
-- so a second row cannot be inserted and the version cannot become ambiguous.
--
-- `schema_sha256` is the SHA-256 of this file as `robinauts db init` applied
-- it, written by the command right after the file, since a file cannot hold
-- its own hash. Until the first release the version is 1 whatever edit of
-- this file a database was made from, so the hash is what tells an older
-- edit apart from the current one: the server and the command refuse a
-- database whose hash is not the build's, and say to make it again. NULL is
-- a file applied by hand, refused the same way.
CREATE TABLE IF NOT EXISTS schema_version (
    only_row boolean PRIMARY KEY DEFAULT true CHECK (only_row),
    version integer NOT NULL,
    schema_sha256 text,
    applied_at timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Users.
-- ---------------------------------------------------------------------------

-- Keyed by (provider, subject): the same address at two providers is two
-- users, and a provider that reassigns an address does not hand over an
-- account. `name` and `email` are refreshed at every sign-in and may be
-- null; `email` is only ever an address the provider verified.
CREATE TABLE IF NOT EXISTS users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider text NOT NULL,
    subject text NOT NULL,
    name text,
    email text,
    created_at timestamptz NOT NULL,
    UNIQUE (provider, subject)
);


-- ---------------------------------------------------------------------------
-- Sessions.
-- ---------------------------------------------------------------------------

-- A signed-in browser. The cookie carries the secret; this table holds its
-- SHA-256 and nothing that could open the session. A session is never
-- renewed, so `expires_at` is written once.
-- Every constraint here is named, and the names are not decoration: the
-- store translates a violation of one of them into an answer for its caller
-- (`datastore/credentials.py`), and it tells them apart by name. A
-- constraint left to PostgreSQL to name would still be told apart, until the
-- day somebody added a second one of the same kind and every violation
-- started being reported as the first.
CREATE TABLE IF NOT EXISTS sessions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    secret_hash text NOT NULL
        CONSTRAINT sessions_secret_hash_key UNIQUE
        CONSTRAINT sessions_secret_hash_is_a_hash CHECK (secret_hash ~ '^[0-9a-f]{64}$'),
    user_id uuid NOT NULL
        CONSTRAINT sessions_user_id_fkey REFERENCES users (id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL
);

-- Deleting a user ends their sessions with them (ON DELETE CASCADE above),
-- which needs the foreign key's own column indexed or every delete is a scan.
CREATE INDEX IF NOT EXISTS sessions_user_id_idx ON sessions (user_id);

-- What the sweep deletes by.
CREATE INDEX IF NOT EXISTS sessions_expires_at_idx ON sessions (expires_at);


-- ---------------------------------------------------------------------------
-- Sign-ins in progress.
-- ---------------------------------------------------------------------------

-- What a sign-in leaves between the button and the callback: short-lived,
-- single use, found by the SHA-256 of the `state` handed to the provider.
-- The `nonce` and the PKCE verifier are in the clear on purpose -- the
-- callback compares them with what the provider sent, and a hash cannot be
-- compared with something it has not seen (ports/credentials.py says why
-- the row is worth nothing to a reader all the same).
--
-- No foreign key: a sign-in in progress belongs to nobody yet.
CREATE TABLE IF NOT EXISTS pending_logins (
    state_hash text PRIMARY KEY
        CONSTRAINT pending_logins_state_hash_is_a_hash
        CHECK (state_hash ~ '^[0-9a-f]{64}$'),
    provider text NOT NULL,
    nonce text NOT NULL,
    verifier text NOT NULL,
    return_to text,
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL
);

-- What the sweep deletes by, and what the cap counts by.
CREATE INDEX IF NOT EXISTS pending_logins_expires_at_idx ON pending_logins (expires_at);


-- ---------------------------------------------------------------------------
-- Conversations.
-- ---------------------------------------------------------------------------

-- A conversation: one owner, one agent, a model, a title and its times
-- (docs/specs/conversations.md). The id is the application's, never the
-- server's: a turn builds the conversation, its first message and its run
-- together, before any of them is stored, so nothing here may mint one.
--
-- `model` is NOT NULL: the agent's default is copied in when the conversation
-- starts, unless its author picked another, so what a conversation runs on is
-- read off this row alone and never off whatever the agent says today
-- (docs/specs/agents.md). Like `agent`, it is an id of the configuration and
-- not a foreign key: the operator may remove the model, and the conversation
-- then keeps its row and its next turn is refused until it is moved to
-- another.
--
-- `owner_id` cascades. Deleting a user deletes their conversations, with the
-- messages, runs and events under them, by the chain of foreign keys below.
-- The alternative -- refusing to delete a user who has conversations, or
-- leaving them owned by nobody -- would keep the whole of somebody's history
-- after the account that is the only thing entitled to read it has gone. A
-- conversation is private to its owner in this version: there is nobody else
-- for it to belong to.
--
-- No column says where a conversation opens: it opens on the path to its
-- newest message, which the tree of `messages` below decides
-- (docs/specs/conversations.md).
CREATE TABLE IF NOT EXISTS conversations (
    id uuid
        CONSTRAINT conversations_pkey PRIMARY KEY,
    owner_id uuid NOT NULL
        CONSTRAINT conversations_owner_id_fkey REFERENCES users (id) ON DELETE CASCADE,
    agent text NOT NULL,
    model text NOT NULL,
    title text NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);

-- The listing, exactly: one person's conversations, most recently updated
-- first, ties broken by id so that the order is total and a keyset page
-- cannot show one twice or skip one (docs/specs/conversations.md). Leading
-- with `owner_id` is also what the cascade above needs -- a foreign key whose
-- column is unindexed turns every delete of a user into a table scan -- so
-- this one index serves both and `conversations` has no second one.
CREATE INDEX IF NOT EXISTS conversations_listing_idx
    ON conversations (owner_id, updated_at DESC, id DESC);


-- ---------------------------------------------------------------------------
-- Messages.
-- ---------------------------------------------------------------------------

-- One message of a conversation: a node of the tree. `document` is the
-- message in the platform's own format (robinauts.core.message_to_data),
-- kept whole and never read here; the columns beside it are the ones the
-- store indexes by and checks with, and they say nothing the document does
-- not (ports/conversations.py, docs/layout.md).
--
-- The id is a primary key, so it is **unique across the deployment** and not
-- merely within a conversation: the same id offered in another conversation
-- is the same row and is refused.
--
-- `parent_id` must be a message **of the same conversation**, and that is
-- what the composite foreign key says: `(conversation_id, parent_id)` points
-- at `(conversation_id, id)`, so a parent from somebody else's tree cannot be
-- stored whatever a caller believed. A composite key needs a unique index
-- over exactly those two columns, which is what
-- `messages_conversation_id_id_key` is for; it is implied by the primary key
-- and is here because PostgreSQL requires a declared one. A null `parent_id`
-- is a root and the constraint does not apply to it (MATCH SIMPLE), which is
-- what a nullable half of a composite key means.
--
-- Every constraint is named, and the names are interface: the store
-- translates a violation of one of them into an answer for its caller and
-- tells them apart by name (datastore/conversations.py, CONVERSATION_REFUSALS).
CREATE TABLE IF NOT EXISTS messages (
    id uuid
        CONSTRAINT messages_pkey PRIMARY KEY,
    conversation_id uuid NOT NULL
        CONSTRAINT messages_conversation_id_fkey REFERENCES conversations (id) ON DELETE CASCADE,
    parent_id uuid,
    -- The roles of robinauts.domain.Role. `tool` is reserved and refused
    -- above this layer; the spelling is settled here so that the day it is
    -- carried, no column changes.
    role text NOT NULL
        CONSTRAINT messages_role_is_a_role CHECK (role IN ('user', 'assistant', 'tool')),
    created_at timestamptz NOT NULL,
    document jsonb NOT NULL,
    CONSTRAINT messages_conversation_id_id_key UNIQUE (conversation_id, id),
    CONSTRAINT messages_parent_id_fkey FOREIGN KEY (conversation_id, parent_id)
        REFERENCES messages (conversation_id, id) ON DELETE CASCADE
);

-- What reading a conversation does: every message of it, oldest first, ties
-- broken by id so that two stores hand back one order.
CREATE INDEX IF NOT EXISTS messages_conversation_id_created_at_idx
    ON messages (conversation_id, created_at, id);


-- ---------------------------------------------------------------------------
-- Runs.
-- ---------------------------------------------------------------------------

-- One turn's work: what an agent did to answer one user message
-- (docs/specs/runs.md). Unlike a message, a run is columns and no document --
-- every field of it is something this store orders, filters or decides by --
-- and the store builds the record back out of them inside
-- `domain.reading_stored`. The one column that is neither is `engine_state`,
-- below, which is opaque bytes handed back as they were given.
--
-- `(conversation_id, message_id)` points at a message of **that**
-- conversation: a run answers a question of the conversation it is in, and
-- the rest of that rule -- that the message is a *user* message -- is the
-- store's, since SQL cannot express "and its role is user" in a foreign key.
CREATE TABLE IF NOT EXISTS runs (
    id uuid
        CONSTRAINT runs_pkey PRIMARY KEY,
    conversation_id uuid NOT NULL
        CONSTRAINT runs_conversation_id_fkey REFERENCES conversations (id) ON DELETE CASCADE,
    message_id uuid NOT NULL,
    agent text NOT NULL,
    -- The engines of robinauts.domain.Engine.
    engine text NOT NULL
        CONSTRAINT runs_engine_is_an_engine CHECK (engine IN ('langgraph', 'pydantic-ai')),
    model text NOT NULL,
    -- The six states of robinauts.domain.RunState. Which may follow which is
    -- a rule above this layer; what a column can say is that nothing else is
    -- a state at all.
    state text NOT NULL
        CONSTRAINT runs_state_is_a_state CHECK (
            state IN ('running', 'waiting', 'finished', 'failed', 'cancelled', 'interrupted')
        ),
    created_at timestamptz NOT NULL,
    started_at timestamptz,
    finished_at timestamptz,
    error text,
    -- The conversation as the agent framework left it after this run: the
    -- framework's own serialisation of its history, written with the run's
    -- ending and read by the conversation's next turn, and never read by
    -- anything of the platform's (docs/specs/conversations.md, "The model's
    -- memory"). NULL for a run that did not finish, which leaves the memory
    -- where the run before it left it. The transcript is in `messages`.
    engine_state bytea,
    CONSTRAINT runs_message_id_fkey FOREIGN KEY (conversation_id, message_id)
        REFERENCES messages (conversation_id, id) ON DELETE CASCADE
);

-- **At most one active run per conversation**, held by the database and not
-- by a count in a transaction that has not committed yet (docs/specs/runs.md).
-- Two requests arriving together both look, both find none, and both insert:
-- only a unique index makes one of them fail. The store takes the
-- conversation's row with `SELECT ... FOR UPDATE` first, so in practice the
-- second is told "already answering" rather than meeting this; this is the
-- backstop under that, and the store translates a violation of it -- by name
-- -- into `RunAlreadyActiveError`.
--
-- Partial, over the active states alone: a conversation has any number of
-- runs that have ended.
CREATE UNIQUE INDEX IF NOT EXISTS runs_one_active_per_conversation
    ON runs (conversation_id) WHERE state IN ('running', 'waiting');

-- What the start-up sweep reads: the runs of the whole deployment that are
-- still going, oldest first (`runs_in`). Partial for the same reason -- the
-- ended runs are the deployment's whole history and nobody sweeps those.
CREATE INDEX IF NOT EXISTS runs_active_idx
    ON runs (created_at, id) WHERE state IN ('running', 'waiting');

-- What opening a conversation reads: its runs, most recent first, ties
-- broken by id (`runs_of`). A conversation answered a thousand times must
-- not be read a thousand rows at a time to look at the last one.
CREATE INDEX IF NOT EXISTS runs_conversation_id_created_at_idx
    ON runs (conversation_id, created_at DESC, id DESC);


-- ---------------------------------------------------------------------------
-- Run events.
-- ---------------------------------------------------------------------------

-- What a run published, numbered. `document` is the event in the platform's
-- format (robinauts.core.run_event_to_data), kept whole and never read here.
--
-- The primary key is `(run_id, seq)`, which is the whole of "a position is
-- stored once": of several writers offering one position at once exactly one
-- gets it, and the loser is told the position is taken rather than being
-- allowed to renumber. The store holds the run's row with
-- `SELECT ... FOR UPDATE` while it reads the last position and writes the
-- next, so this is the backstop under that rule, translated by name into
-- `PositionTakenError`.
--
-- `kind` is the name of the record's class (robinauts.domain: `RunStarted`,
-- `MessageStarted`, `TextDelta`, `ReasoningDelta`, `MessageCompleted`,
-- `RunEnded`). It is a column and not something read out of the document
-- because the store may not read a document at all, and it exists for the
-- partial unique index below -- which is why the store writes it and reads
-- it back nowhere. There is no CHECK enumerating the kinds, deliberately --
-- a kind added to the format does not move this schema.
CREATE TABLE IF NOT EXISTS run_events (
    run_id uuid NOT NULL
        CONSTRAINT run_events_run_id_fkey REFERENCES runs (id) ON DELETE CASCADE,
    -- Positions run from FIRST_POSITION (1) with no gaps. `bigint` because
    -- the format allows up to 2^53 - 1 of them.
    seq bigint NOT NULL
        CONSTRAINT run_events_seq_is_a_position CHECK (seq >= 1),
    kind text NOT NULL,
    document jsonb NOT NULL,
    CONSTRAINT run_events_pkey PRIMARY KEY (run_id, seq)
);

-- A run ends once. Nothing is written into a run that has ended, which the
-- store decides from the run's own state in the same step as the write --
-- `end_run` writes the ended record and its `RunEnded` in one transaction,
-- so the state is the same answer and costs no query. This says the same
-- thing about the stream itself, so that a second `RunEnded` is impossible
-- rather than merely unwritten, and it is the backstop that lets the write
-- path stay one index lookup however long a run has been streaming. The
-- literal is the name of `robinauts.domain.RunEnded`, which
-- tests/unit/test_datastore_schema.py pins.
CREATE UNIQUE INDEX IF NOT EXISTS run_events_one_end_per_run
    ON run_events (run_id) WHERE kind = 'RunEnded';

-- Deleting a run takes its events with it (ON DELETE CASCADE above), which
-- needs the foreign key's own column indexed or every delete is a scan --
-- and it is the primary key's leading column, so the key is that index and
-- there is no second one. Reading a watcher's slice (`events_of`, `seq >
-- after`) walks the same key.


-- ---------------------------------------------------------------------------
-- The version, written last.
-- ---------------------------------------------------------------------------

-- Last on purpose. This row is the claim "every table above exists, in the
-- shape this version describes", so it must not be written until they do: a
-- file that stopped half way -- an interrupted `psql`, a permission error, a
-- disconnection -- leaves no version, and `create_schema` and `check_schema`
-- both refuse what they find rather than believing it.
--
-- `DO NOTHING`, never `DO UPDATE`. Relabelling an older schema as this one
-- would be the worst thing this file could do: the `CREATE TABLE IF NOT
-- EXISTS` statements above leave an existing table exactly as it is, so an
-- `UPDATE` here would stamp this version on an older one's tables and every
-- check afterwards would pass. Deciding whether this database may be
-- written to at all is `create_schema`'s job, before any of this runs.
--
-- The version this file defines must equal `SCHEMA_VERSION` in schema.py;
-- tests/unit/test_datastore_schema.py fails if the two drift apart.
--
-- Version 1 is the schema until the first release: this file, as it stands,
-- edited in place. A database made from an older edit is made again, and
-- `schema_sha256`, written by the command after this row, is how it is told.
INSERT INTO schema_version (version) VALUES (1)
ON CONFLICT (only_row) DO NOTHING;

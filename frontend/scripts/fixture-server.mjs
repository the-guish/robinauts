// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * A built `dist/` served with fixture answers, for looking at the shell.
 *
 * A development tool. It is not in the bundle, not in the wheel and not part
 * of any check: it exists so that a person -- or a browser driven by one --
 * can see the interface in a state the backend would take a database and a
 * sign-in to produce. It answers exactly the routes the code here calls, and
 * refuses everything else.
 *
 *     npm run build
 *     node scripts/fixture-server.mjs signed-in 5173
 *
 * The scene is the first argument: `signed-in`, `one-agent`, `no-agents`,
 * `local`, `signed-out`, `history`, `conversation`, `streaming`, `reattach`
 * or `error`. It binds loopback only.
 *
 * A failed sign-in is the `signed-out` scene at the hash the backend
 * redirects to: `http://127.0.0.1:5173/#/sign-in?error=not_allowed`.
 *
 * **Which conversation is open is the hash**, so `history` and `conversation`
 * differ in what they list rather than in what they serve: `history` has
 * enough conversations to page through, `conversation` the three a
 * conversation can be in --
 * `#/c/c0000000-0000-4000-8000-000000000001` (a branch),
 * `…002` (a run in flight) and `…003` (a run that ended badly). In
 * `conversation`, `…003` is also on a model these fixtures no longer offer,
 * so its next turn is refused until another is picked.
 *
 * Renaming, deleting, cancelling and changing a model really change what
 * this serves, so the states after them can be looked at too. Nothing is
 * written to disk: a restart is a fresh set of fixtures.
 *
 * **The four scenes with a stream in them** answer the streaming routes of
 * `docs/specs/wire.md` for real -- server-sent events, a position on the last
 * wire event derived from each of the run's own, and the two headers -- so
 * that the chat can be looked at doing what it is for:
 *
 * - `streaming`: sending a message streams a stretch of thinking and then an
 *   answer, a few words at a time;
 * - `reattach`: conversation `…002` has a run going, so opening it attaches
 *   to that run at the `resume.after` the conversation answered with, and the
 *   rest of the answer arrives;
 * - `drop`: the connection goes in the middle of the answer, and the run
 *   does not;
 * - `error`: the same turn, ending in `RUN_ERROR`.
 *
 * A turn really writes its messages into the conversation, so the reread that
 * follows one shows what the store would.
 */
import { randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import { createServer } from "node:http";
import { extname, join, normalize, resolve, sep } from "node:path";
import process from "node:process";

const DIST = resolve(import.meta.dirname, "..", "dist");

const USER = {
  id: "11111111-1111-1111-1111-111111111111",
  provider: "google",
  name: "Ada Lovelace",
  email: "ada@example.test",
};

const PROVIDERS = [
  { id: "google", title: "Google" },
  { id: "okta", title: "Okta" },
];

/**
 * The models on offer. An agent's `model` is its default, and the two
 * agents' defaults differ, so switching agent on the empty chat can be seen
 * to switch the model with it until one is picked.
 */
const MODELS = [
  { id: "claude-sonnet", title: "Claude Sonnet" },
  { id: "gpt-5-5", title: "GPT 5.5" },
  { id: "gemini-2-5-pro", title: "Gemini 2.5 Pro" },
];

/** A model the operator has removed, which a scene's `retired` is still on. */
const RETIRED = "claude-opus-3";

const AGENTS = [
  {
    id: "helper",
    title: "Helper",
    engine: "langgraph",
    model: "claude-sonnet",
  },
  {
    id: "researcher",
    title: "Researcher",
    engine: "pydantic-ai",
    model: "gpt-5-5",
  },
];

/** @param {unknown} id */
const offered = (id) => MODELS.some((model) => model.id === id);

/** What the backend says about a model a request named and it does not offer. */
const NOT_OFFERED = "body.model_id: is not a model this deployment offers";

/** And about a turn in a conversation on a model it no longer offers. */
const NO_LONGER_OFFERED =
  "this conversation's model is no longer offered here; move the conversation to another";

const SIGNED_IN = {
  sign_in: true,
  local_development: false,
  providers: PROVIDERS,
  user: USER,
};

const RUN = "99999999-9999-4999-8999-999999999999";

/**
 * What an answer records (`ProvenanceView`).
 *
 * @typedef {{agent: string, engine: string, model: string, run_id: string}} Provenance
 */

/**
 * One message of a conversation's tree (`MessageView`).
 *
 * @typedef {{
 *   id: string,
 *   parent_id: string | null,
 *   role: string,
 *   channel: string,
 *   created_at: string,
 *   parts: {kind: string, text: string}[],
 *   provenance: Provenance | null,
 * }} Message
 */

/**
 * A conversation opened, without the summary (`OpenedConversationResponse`).
 *
 * @typedef {{
 *   messages: Message[],
 *   leaf_id: string | null,
 *   run_id: string | null,
 *   resume: {after: number, follows: string | null} | null,
 *   ended_badly: {run_id: string, state: string, ended_at: string} | null,
 * }} Tree
 */

/**
 * A conversation's id, in the shape the router insists on.
 *
 * @param {number} n
 */
const conversationId = (n) =>
  `c0000000-0000-4000-8000-${String(n).padStart(12, "0")}`;

/**
 * The agent and model a conversation is bound to unless a fixture says otherwise.
 */
const DEFAULT_BOUND = { agent: "helper", model: "claude-sonnet" };

/**
 * One conversation as the panel lists it.
 *
 * `updated_at` counts backwards from the first, because a listing is most
 * recently updated first and a panel where every row says the same time
 * would hide that.
 *
 * @param {number} n
 * @param {string} title
 * @param {{agent: string, model: string}} [bound]
 * @returns {{
 *   id: string,
 *   title: string,
 *   agent: string,
 *   model: string,
 *   created_at: string,
 *   updated_at: string,
 *   active_leaf_id: string | null,
 * }}
 */
const summary = (n, title, bound = DEFAULT_BOUND) => ({
  id: conversationId(n),
  title,
  ...bound,
  created_at: "2026-09-18T09:00:00Z",
  updated_at: new Date(Date.parse("2026-09-21T16:00:00Z") - n * 3_600_000)
    .toISOString()
    .replace(".000", ""),
  active_leaf_id: null,
});

/**
 * @param {string} id
 * @param {string | null} parent
 * @param {string} role
 * @param {string} text
 * @param {Provenance | null} provenance
 * @returns {Message}
 */
const message = (id, parent, role, text, provenance = null) => ({
  id,
  parent_id: parent,
  role,
  channel: "web",
  created_at: "2026-09-21T15:40:00Z",
  parts: [{ kind: "text", text }],
  provenance,
});

const MADE_BY = {
  agent: "helper",
  engine: "langgraph",
  model: "claude-sonnet",
  run_id: RUN,
};

/** The titles, in the order the listing hands them out. */
const TITLES = [
  "Why do robins sing before dawn?",
  "Reading a wheel's metadata",
  "An answer that stopped halfway",
  "Migrating a schema without migrations",
  "Server-sent events behind a proxy",
  "Choosing between two engines",
  "What a licence gate can and cannot see",
  "Notes on the panel's focus trap",
];

/**
 * A conversation's messages and the state of its last run, by id.
 *
 * The first three are the states a conversation can be in; the rest are one
 * exchange each, so that the panel has something to page through.
 *
 * @param {number} n
 * @returns {Tree}
 */
function tree(n) {
  const first = message(
    `${conversationId(n)}-1`,
    null,
    "user",
    TITLES[n - 1] ?? "A question",
  );
  const answer = message(
    `${conversationId(n)}-2`,
    first.id,
    "assistant",
    "Short answer: yes. The longer one is that it depends on what the\nquestion is really about, and this is a fixture rather than a model.",
    MADE_BY,
  );
  if (n === 1) {
    const follow = message(
      `${conversationId(n)}-3`,
      answer.id,
      "user",
      "And what about the ones that sing at night?",
    );
    const last = message(
      `${conversationId(n)}-4`,
      follow.id,
      "assistant",
      "Street lighting, mostly. A robin will sing under a lamp post at two\nin the morning and be perfectly sincere about it.",
      { ...MADE_BY, engine: "pydantic-ai" },
    );
    // A regeneration of the first answer, so the tree really has a branch
    // beside the one that is shown.
    const beside = message(
      `${conversationId(n)}-2b`,
      first.id,
      "assistant",
      "Another answer to the same question, on the branch beside this one.",
      MADE_BY,
    );
    return {
      messages: [first, answer, beside, follow, last],
      leaf_id: last.id,
      run_id: null,
      resume: null,
      ended_badly: null,
    };
  }
  if (n === 2) {
    return {
      messages: [first],
      leaf_id: first.id,
      run_id: RUN,
      resume: { after: 4, follows: first.id },
      ended_badly: null,
    };
  }
  if (n === 3) {
    return {
      messages: [first],
      leaf_id: first.id,
      run_id: null,
      resume: null,
      ended_badly: {
        run_id: RUN,
        state: "interrupted",
        ended_at: "2026-09-21T15:41:00Z",
      },
    };
  }
  return {
    messages: [first, answer],
    leaf_id: answer.id,
    run_id: null,
    resume: null,
    ended_badly: null,
  };
}

/**
 * @type {Record<string, {
 *   session: unknown,
 *   agents: typeof AGENTS,
 *   conversations?: number,
 *   retired?: number,
 *   stream?: "finishes" | "fails",
 *   inFlight?: number,
 *   dropAt?: number,
 * }>}
 */
const SCENES = {
  "signed-in": {
    session: SIGNED_IN,
    agents: AGENTS,
  },
  history: {
    session: SIGNED_IN,
    agents: AGENTS,
    conversations: TITLES.length,
  },
  conversation: {
    session: SIGNED_IN,
    agents: AGENTS,
    conversations: 3,
    /** `…003` is on a model no longer offered. */
    retired: 3,
  },
  /** A turn that really streams: a stretch of thinking, then an answer. */
  streaming: {
    session: SIGNED_IN,
    agents: AGENTS,
    conversations: 3,
    stream: "finishes",
  },
  /** `…002` has a run going: opening it attaches at `resume.after`. */
  reattach: {
    session: SIGNED_IN,
    agents: AGENTS,
    conversations: 3,
    stream: "finishes",
    inFlight: 2,
  },
  /**
   * The connection goes in the middle of the answer.
   *
   * The stream simply stops -- no event in it says the run is over -- which
   * is what a proxy closing a connection looks like. The run is still in
   * flight, so the chat reads the conversation and watches it again
   * (`docs/specs/wire.md`; `src/chat/runtime.tsx`).
   */
  drop: {
    session: SIGNED_IN,
    agents: AGENTS,
    conversations: 3,
    stream: "finishes",
    dropAt: 7,
  },
  /** The same turn, ending in a `RUN_ERROR`. */
  error: {
    session: SIGNED_IN,
    agents: AGENTS,
    conversations: 3,
    stream: "fails",
  },
  "one-agent": {
    session: SIGNED_IN,
    agents: AGENTS.slice(0, 1),
  },
  "no-agents": {
    session: SIGNED_IN,
    agents: [],
  },
  local: {
    session: {
      sign_in: false,
      local_development: true,
      providers: [],
      user: {
        id: "00000000-0000-0000-0000-000000000000",
        provider: "!local",
        name: "Local developer",
        email: null,
      },
    },
    agents: AGENTS,
  },
  "signed-out": {
    session: {
      sign_in: true,
      local_development: false,
      providers: PROVIDERS,
      user: null,
    },
    agents: [],
  },
};

/**
 * What a built file is served as. Nothing else is servable.
 *
 * @type {Record<string, string>}
 */
const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".svg": "image/svg+xml",
  ".txt": "text/plain; charset=utf-8",
  ".ico": "image/x-icon",
};

const scene = process.argv[2] ?? "signed-in";
const port = Number(process.argv[3] ?? "5173");
const fixture = SCENES[scene];
if (fixture === undefined) {
  console.error(`no such scene: ${scene}. One of ${Object.keys(SCENES)}`);
  process.exit(2);
}

/**
 * The conversations this run is serving, and what they hold.
 *
 * Mutable: a rename, a delete and a cancel really change them, so what the
 * interface looks like after one can be looked at. It lives for as long as
 * the process does and is written nowhere.
 */
const listed = Array.from({ length: fixture.conversations ?? 0 }, (_, index) =>
  summary(index + 1, TITLES[index] ?? `Conversation ${index + 1}`, {
    agent: "helper",
    model: fixture.retired === index + 1 ? RETIRED : "claude-sonnet",
  }),
);
/** @type {Map<string, Tree>} */
const trees = new Map(listed.map((one, index) => [one.id, tree(index + 1)]));

/**
 * How many a page holds, whatever was asked for.
 *
 * The interface asks for thirty. Answering with five is what makes "Load
 * more" something to look at without inventing thirty-one conversations.
 */
const PAGE = 5;

/**
 * @param {import("node:http").ServerResponse} response
 * @param {number} status
 * @param {unknown} body
 */
function json(response, status, body) {
  const text = JSON.stringify(body);
  response.writeHead(status, {
    "content-type": "application/json",
    "cache-control": "no-store",
  });
  response.end(text);
}

/**
 * The API's one shape for a refusal (`api/errors.py`).
 *
 * @param {import("node:http").ServerResponse} response
 * @param {number} status
 * @param {string} error
 * @param {string} detail
 */
function refuse(response, status, error, detail) {
  json(response, status, { error, detail });
}

/**
 * The body of a write, as JSON; `null` if there was none or it was not.
 *
 * @param {import("node:http").IncomingMessage} request
 * @returns {Promise<Record<string, unknown> | null>}
 */
function body(request) {
  return new Promise((done) => {
    /** @type {Buffer[]} */
    const chunks = [];
    request.on("data", (/** @type {Buffer} */ chunk) =>
      chunks.push(Buffer.from(chunk)),
    );
    request.on("end", () => {
      try {
        done(JSON.parse(Buffer.concat(chunks).toString("utf8")));
      } catch {
        done(null);
      }
    });
  });
}

const UUID = "[0-9a-fA-F-]{36}";
const ONE = new RegExp(`^/api/conversations/(${UUID})$`);
const CANCEL = new RegExp(
  `^/api/conversations/(${UUID})/runs/(${UUID})/cancel$`,
);
const LEAF = new RegExp(`^/api/conversations/(${UUID})/leaf$`);
const MODEL = new RegExp(`^/api/conversations/(${UUID})/model$`);
const TURNS = new RegExp(`^/api/conversations/(${UUID})/turns$`);
const EVENTS = new RegExp(`^/api/runs/(${UUID})/events$`);

/**
 * The conversation routes, as far as the interface uses them.
 *
 * Returns whether it answered, so that the file server below is only reached
 * by what is really a file.
 *
 * @param {import("node:http").IncomingMessage} request
 * @param {import("node:http").ServerResponse} response
 * @param {string} path
 * @param {URLSearchParams} query
 * @returns {Promise<boolean>}
 */
async function conversations(request, response, path, query) {
  const method = request.method ?? "GET";
  if (path === "/api/conversations" && method === "GET") {
    const cursor = query.get("cursor");
    const from = cursor === null ? 0 : Number(cursor);
    const items = listed.slice(from, from + PAGE);
    const next = from + PAGE;
    json(response, 200, {
      items,
      next_cursor: next < listed.length ? String(next) : null,
    });
    return true;
  }
  const one = ONE.exec(path);
  if (one !== null) {
    const id = one[1] ?? "";
    const index = listed.findIndex((each) => each.id === id);
    const held = trees.get(id);
    if (index === -1 || held === undefined) {
      // One that is not there and one that is somebody else's answer alike.
      refuse(
        response,
        404,
        "NotFoundError",
        "there is nothing here of that id",
      );
      return true;
    }
    const found = listed[index];
    if (found === undefined) return true;
    if (method === "GET") {
      json(response, 200, { conversation: found, ...held });
      return true;
    }
    if (method === "PATCH") {
      const asked = await body(request);
      const title = typeof asked?.title === "string" ? asked.title.trim() : "";
      if (title === "") {
        refuse(
          response,
          422,
          "InvalidValueError",
          "a title is one line with something on it",
        );
        return true;
      }
      const renamed = { ...found, title };
      listed[index] = renamed;
      json(response, 200, renamed);
      return true;
    }
    if (method === "DELETE") {
      if (held.run_id !== null) {
        // A conversation with a run going is not deleted
        // (docs/specs/conversations.md, "Deletion").
        refuse(
          response,
          409,
          "RunAlreadyActiveError",
          `run ${held.run_id} is running in conversation ${id}; cancel it or wait for it`,
        );
        return true;
      }
      listed.splice(index, 1);
      trees.delete(id);
      response.writeHead(204).end();
      return true;
    }
  }
  const leaf = LEAF.exec(path);
  if (leaf !== null && method === "PUT") {
    // Where its author is reading. It deliberately does not date the
    // conversation (`docs/specs/conversations.md`).
    const id = leaf[1] ?? "";
    const index = listed.findIndex((each) => each.id === id);
    const held = trees.get(id);
    const found = listed[index];
    const asked = await body(request);
    const messageId =
      typeof asked?.message_id === "string" ? asked.message_id : "";
    if (found === undefined || held === undefined) {
      refuse(
        response,
        404,
        "NotFoundError",
        "there is nothing here of that id",
      );
      return true;
    }
    trees.set(id, { ...held, leaf_id: messageId });
    const moved = { ...found, active_leaf_id: messageId };
    listed[index] = moved;
    json(response, 200, moved);
    return true;
  }
  const model = MODEL.exec(path);
  if (model !== null && method === "PUT") {
    // Checked before the conversation, as the backend does.
    const asked = await body(request);
    if (!offered(asked?.model_id)) {
      refuse(response, 422, "UnknownModelError", NOT_OFFERED);
      return true;
    }
    const id = model[1] ?? "";
    const index = listed.findIndex((each) => each.id === id);
    const found = listed[index];
    if (found === undefined) {
      refuse(
        response,
        404,
        "NotFoundError",
        "there is nothing here of that id",
      );
      return true;
    }
    // A change of model dates the conversation, as a rename does.
    const moved = {
      ...found,
      model: String(asked?.model_id),
      updated_at: new Date().toISOString(),
    };
    listed.splice(index, 1);
    listed.unshift(moved);
    json(response, 200, moved);
    return true;
  }
  const turns = TURNS.exec(path);
  if (turns !== null && method === "POST") {
    const id = turns[1] ?? "";
    const held = trees.get(id);
    const found = listed.find((each) => each.id === id);
    if (held === undefined) {
      refuse(
        response,
        404,
        "NotFoundError",
        "there is nothing here of that id",
      );
      return true;
    }
    // Once the conversation is known, and before the run already going: a
    // model no longer offered refuses the turn by name (`docs/specs/wire.md`).
    if (!offered(found?.model)) {
      refuse(response, 409, "ModelNotOfferedError", NO_LONGER_OFFERED);
      return true;
    }
    if (held.run_id !== null) {
      refuse(
        response,
        409,
        "RunAlreadyActiveError",
        `run ${held.run_id} is running in conversation ${id}`,
      );
      return true;
    }
    const asked = await body(request);
    if (typeof asked?.regenerate === "string") {
      // A regeneration answers the question that turn already had: it hangs
      // where the answer it replaces hung.
      const replaced = held.messages.find(
        (each) => each.id === asked.regenerate,
      );
      await follow(response, begin(id, replaced?.parent_id ?? null), 0);
      return true;
    }
    const text = typeof asked?.text === "string" ? asked.text : "";
    const parentId =
      typeof asked?.parent_id === "string" ? asked.parent_id : null;
    const question = message(newId(), parentId, "user", text);
    trees.set(id, {
      ...held,
      messages: [...held.messages, question],
      leaf_id: question.id,
    });
    await follow(response, begin(id, question.id), 0);
    return true;
  }
  const cancel = CANCEL.exec(path);
  if (cancel !== null && method === "POST") {
    const id = cancel[1] ?? "";
    const runId = cancel[2] ?? "";
    const going = runs.get(runId);
    if (going?.conversationId === id) {
      // A run this process is streaming: stopping it is what the stream's
      // own ending then says, as the backend's cancel does.
      going.cancelled = true;
      json(response, 200, {
        id: runId,
        state: "cancelled",
        started_at: "2026-09-21T16:00:00Z",
        ended_at: new Date().toISOString(),
      });
      return true;
    }
    const held = trees.get(id);
    if (held?.run_id !== runId) {
      refuse(
        response,
        404,
        "NotFoundError",
        "there is nothing here of that id",
      );
      return true;
    }
    const run = held.run_id;
    trees.set(id, {
      ...held,
      run_id: null,
      resume: null,
      ended_badly: {
        run_id: run,
        state: "cancelled",
        ended_at: "2026-09-21T16:05:00Z",
      },
    });
    json(response, 200, {
      id: run,
      state: "cancelled",
      started_at: "2026-09-21T16:00:00Z",
      ended_at: "2026-09-21T16:05:00Z",
    });
    return true;
  }
  return false;
}

/* ------------------------------------------------------------------ streams */

/**
 * One AG-UI event as a server-sent event (`api/agui.py`, `sse`).
 *
 * `id:` is the platform's own numbering of the run's events, and it goes on
 * the **last** wire event derived from each of them; an event this layer made
 * up carries none, so a client re-attaching after it asks from the last real
 * position (`docs/specs/wire.md`).
 *
 * @param {string} type
 * @param {Record<string, unknown>} body
 * @param {number | null} position
 */
const sse = (type, body, position) => {
  const id = position === null ? "" : `id: ${position}\n`;
  return (
    `${id}event: ${type}\n` + `data: ${JSON.stringify({ type, ...body })}\n\n`
  );
};

/**
 * A run's events: one step per event the platform numbered.
 *
 * Written out the way the backend really derives them. The brackets around
 * thinking are derived from the sequence and are **not** numbered, so each of
 * them travels with the step that produced it; the first text delta closes
 * the stretch of thinking that came before it, and the error ending of a run
 * that failed carries no position at all.
 *
 * @param {string} threadId
 * @param {string} runId
 * @param {string} messageId
 * @param {"finishes" | "fails"} how
 * @returns {{
 *   steps: {position: number | null, events: string[]}[],
 *   said: string,
 * }}
 */
function script(threadId, runId, messageId, how) {
  const thought = `${messageId}:reasoning:3`;
  const said = [
    "Because it is quiet then. ",
    "A robin singing before dawn is heard further than one singing at noon, ",
    "and a song that carries is a territory that is already claimed. ",
    "Street lighting does the rest: a lamp post is as good as a sunrise ",
    "to a bird that has never read an almanac.",
  ];
  /** @type {{position: number | null, events: string[]}[]} */
  const steps = [
    { position: 1, events: [sse("RUN_STARTED", { threadId, runId }, 1)] },
    {
      position: 2,
      events: [sse("TEXT_MESSAGE_START", { messageId, role: "assistant" }, 2)],
    },
    {
      position: 3,
      events: [
        sse("REASONING_MESSAGE_START", { messageId: thought }, null),
        sse(
          "REASONING_MESSAGE_CONTENT",
          { messageId: thought, delta: "The question is about robins, " },
          3,
        ),
      ],
    },
    {
      position: 4,
      events: [
        sse(
          "REASONING_MESSAGE_CONTENT",
          {
            messageId: thought,
            delta: "and about what dawn is good for. Sound carries further ",
          },
          4,
        ),
      ],
    },
    {
      position: 5,
      events: [
        sse(
          "REASONING_MESSAGE_CONTENT",
          { messageId: thought, delta: "in cold, still air." },
          5,
        ),
      ],
    },
  ];
  said.forEach((delta, index) => {
    const position = 6 + index;
    steps.push({
      position,
      events: [
        // The first text delta is what closes the thinking.
        ...(index === 0
          ? [sse("REASONING_MESSAGE_END", { messageId: thought }, null)]
          : []),
        sse("TEXT_MESSAGE_CONTENT", { messageId, delta }, position),
      ],
    });
  });
  const ending = 6 + said.length;
  if (how === "fails") {
    // A run that ended in an error: the event this layer made up carries no
    // position, and the answer it was producing is in no conversation.
    steps.push({
      position: null,
      events: [
        sse(
          "RUN_ERROR",
          { message: "the agent could not finish this answer", code: "failed" },
          null,
        ),
      ],
    });
    return { steps, said: "" };
  }
  steps.push(
    {
      position: ending,
      events: [sse("TEXT_MESSAGE_END", { messageId }, ending)],
    },
    {
      position: ending + 1,
      events: [sse("RUN_FINISHED", { threadId, runId }, ending + 1)],
    },
  );
  return { steps, said: said.join("") };
}

/**
 * The runs this process has started, by id.
 *
 * @type {Map<string, {
 *   conversationId: string,
 *   messageId: string,
 *   parentId: string | null,
 *   steps: {position: number | null, events: string[]}[],
 *   said: string,
 *   how: "finishes" | "fails",
 *   cancelled: boolean,
 *   dropped: boolean,
 * }>}
 */
const runs = new Map();

/** A uuid, because a run and a message need one and nothing else does. */
const newId = () => randomUUID();

/**
 * Begin a run in that conversation, under that message.
 *
 * @param {string} conversationId
 * @param {string | null} parentId
 */
function begin(conversationId, parentId) {
  const how = fixture?.stream === "fails" ? "fails" : "finishes";
  const runId = newId();
  const messageId = newId();
  const { steps, said } = script(conversationId, runId, messageId, how);
  runs.set(runId, {
    conversationId,
    messageId,
    parentId,
    steps,
    said,
    how,
    cancelled: false,
    dropped: false,
  });
  const held = trees.get(conversationId);
  if (held !== undefined) {
    trees.set(conversationId, {
      ...held,
      run_id: runId,
      resume: { after: 0, follows: parentId },
      ended_badly: null,
    });
  }
  return runId;
}

/** @param {number} ms */
const delay = (ms) => new Promise((done) => setTimeout(done, ms));

/**
 * Write that run's events from `after`, a step at a time.
 *
 * @param {import("node:http").ServerResponse} response
 * @param {string} runId
 * @param {number} after
 */
async function follow(response, runId, after) {
  const run = runs.get(runId);
  if (run === undefined) {
    refuse(response, 404, "NotFoundError", "there is nothing here of that id");
    return;
  }
  response.writeHead(200, {
    "content-type": "text/event-stream",
    "cache-control": "no-store",
    "x-accel-buffering": "no",
    "x-robinauts-run-id": runId,
    "x-robinauts-conversation-id": run.conversationId,
  });
  let stopped = false;
  const dropAt = fixture?.dropAt;
  for (const step of run.steps) {
    if (step.position !== null && step.position <= after) continue;
    await delay(220);
    if (response.writableEnded) return;
    if (
      dropAt !== undefined &&
      !run.dropped &&
      step.position !== null &&
      step.position > dropAt
    ) {
      // The connection goes, and the run does not: the stream stops with
      // nothing in it saying the run is over, and the conversation still
      // names it as in flight.
      run.dropped = true;
      const before = trees.get(run.conversationId);
      if (before !== undefined) {
        trees.set(run.conversationId, {
          ...before,
          run_id: runId,
          resume: { after: dropAt, follows: run.parentId },
        });
      }
      response.end();
      return;
    }
    if (run.cancelled) {
      // **A cancellation is not a failure**: `RUN_FINISHED` with AG-UI's
      // `cancelled` outcome, and no position -- this layer made it up
      // (`docs/specs/wire.md`).
      response.write(
        sse(
          "RUN_FINISHED",
          {
            threadId: run.conversationId,
            runId,
            outcome: { type: "cancelled" },
          },
          null,
        ),
      );
      stopped = true;
      break;
    }
    for (const written of step.events) response.write(written);
  }
  response.end();
  // A turn writes its messages, so the reread that follows one shows them.
  // **The message in flight is not one of them**: a message enters the
  // conversation when it is complete (`docs/specs/runs.md`), so a run that
  // was stopped or that failed leaves the conversation as it was.
  const held = trees.get(run.conversationId);
  if (held === undefined) return;
  const finished = !stopped && run.how === "finishes";
  /** @type {"cancelled" | "failed" | null} */
  let badly = null;
  if (stopped) badly = "cancelled";
  else if (run.how === "fails") badly = "failed";
  trees.set(run.conversationId, {
    ...held,
    messages: finished
      ? [
          ...held.messages,
          message(run.messageId, run.parentId, "assistant", run.said, MADE_BY),
        ]
      : held.messages,
    leaf_id: finished ? run.messageId : held.leaf_id,
    run_id: null,
    resume: null,
    ended_badly:
      badly === null
        ? null
        : { run_id: runId, state: badly, ended_at: new Date().toISOString() },
  });
}

/**
 * The `reattach` scene: a run already going when the page is first opened.
 *
 * So that opening that conversation does what step 21 is really about --
 * reads it, sees a run in flight, and attaches to that run's stream at the
 * position the conversation answered with (`docs/specs/runs.md`).
 */
if (fixture.inFlight !== undefined) {
  const waiting = conversationId(fixture.inFlight);
  const held = trees.get(waiting);
  if (held !== undefined) {
    const runId = begin(waiting, held.leaf_id);
    const now = trees.get(waiting);
    if (now !== undefined) {
      // `resume.after` is the position of the run's last completed message,
      // or of the event that started it where it has completed none
      // (`docs/specs/runs.md`) -- so the answer still being produced is
      // replayed **from its announcement**, and nothing already on the
      // screen is sent twice.
      trees.set(waiting, {
        ...now,
        run_id: runId,
        resume: { after: 1, follows: held.leaf_id },
      });
    }
  }
}

const server = createServer((request, response) => {
  const asked = new URL(request.url ?? "/", "http://127.0.0.1");
  const path = asked.pathname;
  if (path === "/auth/session") {
    json(response, 200, fixture.session);
    return;
  }
  if (path === "/api/agents") {
    json(response, 200, { items: fixture.agents });
    return;
  }
  if (path === "/api/models") {
    json(response, 200, { items: MODELS });
    return;
  }
  if (path === "/auth/logout") {
    response.writeHead(204).end();
    return;
  }
  if (path === "/api/turns" && request.method === "POST") {
    // A first message creates the conversation, and the response's headers
    // are how the interface learns which one (`docs/specs/wire.md`).
    void body(request).then((sent) => {
      const text = typeof sent?.text === "string" ? sent.text : "";
      // The scene's agents, which are what its picker offered.
      const agent = fixture.agents.find((each) => each.id === sent?.agent_id);
      if (agent === undefined) {
        refuse(
          response,
          404,
          "NotFoundError",
          "there is nothing here of that id",
        );
        return;
      }
      // Absent or null is the agent's default; anything else must be offered.
      const model = sent?.model_id ?? agent.model;
      if (!offered(model)) {
        refuse(response, 422, "UnknownModelError", NOT_OFFERED);
        return;
      }
      const created = summary(listed.length + 1, text.slice(0, 60), {
        agent: agent.id,
        model: String(model),
      });
      const question = message(newId(), null, "user", text);
      listed.unshift(created);
      trees.set(created.id, {
        messages: [question],
        leaf_id: question.id,
        run_id: null,
        resume: null,
        ended_badly: null,
      });
      void follow(response, begin(created.id, question.id), 0);
    });
    return;
  }
  const events = EVENTS.exec(path);
  if (events !== null && request.method === "GET") {
    const after = Number(
      request.headers["last-event-id"] ?? asked.searchParams.get("after") ?? 0,
    );
    void follow(response, events[1] ?? "", Number.isFinite(after) ? after : 0);
    return;
  }
  if (path.startsWith("/api/conversations")) {
    void conversations(request, response, path, asked.searchParams).then(
      (answered) => {
        if (!answered) {
          refuse(response, 404, "NotFoundError", "no such route here");
        }
      },
    );
    return;
  }
  // Anything else is a built file, and `normalize` is what keeps `..` from
  // reaching outside dist/. A tool this small still does not get to serve
  // the whole disk.
  const wanted = normalize(join(DIST, path === "/" ? "/index.html" : path));
  // The separator matters: `dist-secrets/` starts with `dist` and is not
  // inside it.
  if (wanted !== DIST && !wanted.startsWith(DIST + sep)) {
    json(response, 403, { error: "outside", detail: path });
    return;
  }
  const type = TYPES[extname(wanted)];
  readFile(wanted).then(
    (bytes) => {
      response.writeHead(200, {
        "content-type": type ?? "application/octet-stream",
      });
      response.end(bytes);
    },
    () => {
      json(response, 404, { error: "not_found", detail: path });
    },
  );
});

server.listen(port, "127.0.0.1", () => {
  console.log(`${scene} on http://127.0.0.1:${port}/ from ${DIST}`);
});

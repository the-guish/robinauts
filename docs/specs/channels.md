# Channels: chat platforms as clients

A **channel** is a chat platform people reach an agent from: Telegram and Slack
so far. The web interface is not one; a channel is a client of the deployment
that speaks for many people at once.

Status: **proof of concept**, in `channels/` at the root of the repository and
`backend/src/robinauts/web/channels.py`. What is settled and what is not is
at the end.

## Compose over build

The platforms are not ours to integrate one by one. CopilotKit's
[Channels SDK](https://github.com/CopilotKit/channels-sdk) (MIT) already
receives a platform's messages, renders an agent's streamed answer the way the
platform shows one (Telegram HTML edited in place, Slack's native streams and
Block Kit), keeps one turn at a time per platform thread, and drives **any
AG-UI agent**. Robinauts already speaks AG-UI (`wire.md`). So a channel is:

- a small service, `channels/`, a container of its own, which runs the SDK's
  platform adapters on this deployment's side and points their agent at
- one endpoint of the backend that takes AG-UI's **stock** run input.

Adding a platform the SDK has (Teams, Discord, WhatsApp) is an adapter in that
service and no change here.

The SDK runs on its own, without CopilotKit Intelligence, the hosted service
that otherwise holds the platforms' credentials and relays their traffic; and
its anonymous telemetry is switched off. Nothing leaves the deployment's side
but the platforms' own traffic, which is goal 3 of `core.md`.

## The endpoint

`POST /api/channels/agents/{agent_id}/agui`, served only when the backend is
started with `ROBINAUTS_CHANNELS_SECRET` (32 characters at least; a shorter one
stops the start). Outside the OpenAPI document, like every stream (`wire.md`).

- **The caller** proves itself with that secret as a bearer, compared in
  constant time, before the body is read: 401 otherwise.
- **The body** is AG-UI's `RunAgentInput`, of which three things are read:
  - `threadId`: the id of a conversation, to continue it. Anything that is not
    a conversation's id starts a new one; the answer's
    `X-Robinauts-Conversation-Id` names it, and the caller sends it as the
    thread id from then on.
  - `messages`: the text of the **last user message**, its text parts joined.
    The history before it is the store's, as on the web's wire; a client's
    history is never taken.
  - `forwardedProps.robinauts`: `user` — `{"id", "name"}`, the platform person
    the bridge speaks for — and optionally `model_id`.

  The client's `tools`, `context` and `state` are accepted and not read.
- **The user** is `(provider "!channel", subject <user.id>)`, created on first
  sight. `!channel` is not a provider id a sign-in configuration can name, so
  the holder of the secret can speak for platform users and for nobody who
  signs in through an identity provider, nor for the local development mode's
  user. A conversation of somebody else is 404, as on the wire.
- **A new message** goes at the end of the conversation's visible thread: under
  its last answer, or, when the last message is a question nobody answered, in
  its place — what the interface does. A conversation of another agent is 404,
  so that a bridge pointed at another agent starts over. A turn going is 409.
- **The answer** is the same AG-UI stream as the web's, with two differences
  for a stock client written against AG-UI before 1.0, which is what the SDK
  pins: `RUN_STARTED` and `RUN_FINISHED` carry the client's own `runId` (the
  turn's is in `X-Robinauts-Run-Id`), and a cancelled turn ends in `RUN_ERROR`
  coded `cancelled`, since that AG-UI has no cancelled outcome.

The order of the refusals is part of the endpoint: an unknown agent (404) and
a question with no text (422) are refused before anybody is created or anything
is started, which is what the bridge's start-up probe relies on.

## The bridge

`channels/README.md` is how to run it. What it decides:

- **Who may use it.** A Telegram bot can be found by anyone, so Telegram needs
  an allow list of user ids or usernames (`*` for everyone, said explicitly).
  Slack is limited to its workspace, and may be narrowed. Somebody left out is
  told their id.
- **Who a person is.** `telegram:<user id>` — the same person in every chat —
  and `slack:<team>:<member id>`.
- **Which conversation.** Each person's platform conversation (a private chat
  or forum topic; a Slack thread or direct message) is linked to the Robinauts
  conversation it started, in a JSON file on the bridge's volume. Per person,
  because a conversation is private to its owner: in a thread several people
  write in, each has their own.

## Not settled

- **Credentials.** The shared secret is the proof of concept's. What replaces
  it is tokens minted by an operator for one bridge each, naming the platform
  identities it may assert — `slack:T0123` and nothing else — with the allow
  list of `sign-in.md` applying to them as to any provider. `sign-in.md` lists
  "tokens minted by an operator for somebody else" as not there yet.
- **Where thread links live.** In the bridge today; the store is the better
  home, so that two bridges, or a lost volume, do not split a thread across
  conversations.
- **Shared threads.** A Slack thread several people write in is one
  conversation per person today. Whether it should be one conversation, and
  whose, waits for shared projects (`privacy.md`).
- **Account linking**, so that a person's Slack conversations are in their web
  history too.
- **Attachments**, **stopping a turn** from the platform, and tools that ask
  the person first, which the SDK can render as buttons.

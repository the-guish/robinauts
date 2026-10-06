# Channels

A bridge from chat platforms — **Telegram** and **Slack** today — to a Robinauts
deployment. People talk to a Robinauts agent from the platform; the agent, its
tools and the conversations stay in Robinauts.

**This is a proof of concept.** It authenticates to the deployment with one
shared secret, which is to be replaced by tokens minted per bridge (below,
"What is not done").

The platform side is not ours: it is CopilotKit's
[Channels SDK](https://github.com/CopilotKit/channels-sdk) (MIT), which
receives the platform's messages and streams an AG-UI agent's answer back as
the platform shows it. The agent it runs is the deployment's channels endpoint,
through a stock AG-UI client. What this directory adds is the glue:

| file | what it does |
|---|---|
| `src/main.ts` | reads the environment, checks the deployment answers, starts the platforms |
| `src/channel.ts` | one Channel: who may talk to the agent, and a run of it for every message |
| `src/robinauts.ts` | the agent: the deployment's endpoint, with the thread, question and user it wants |
| `src/threads.ts` | which platform thread is which Robinauts conversation, in one JSON file |
| `src/config.ts` | the environment, every problem with it named at once |

It runs on CopilotKit's open-source SDK alone, on this machine: not on
CopilotKit Intelligence, their hosted service, and the SDK's anonymous
telemetry is switched off. Nothing is sent anywhere but to the platforms and
to the deployment.

## How it fits

    Telegram / Slack ──▶ channels (this) ──AG-UI──▶ backend: POST /api/channels/agents/{agent}/agui
                     ◀── streamed answer ◀──SSE───  the controller, the engines, the database

- Each platform user is a Robinauts user of their own (provider `!channel`,
  subject `telegram:<id>` or `slack:<team>:<id>`), created on their first
  message. The bridge names the user; the deployment trusts it because it
  holds the secret, and a bridge can never speak for anybody who signs in
  through the web.
- Each person's platform conversation is one Robinauts conversation: a
  Telegram private chat or forum topic, a Slack thread or direct message. In a
  thread several people write in, each has their own, since a conversation is
  private to its owner. The bridge sends only the newest message; the
  deployment has the history.
- The deployment's side is `backend/src/robinauts/web/channels.py`.

## Try it on a laptop, with Telegram

You need Docker Desktop, a Telegram account, and Robinauts running on the
laptop (the [demo](../demo/README.md) is the quickest way).

1. **A bot.** In Telegram, talk to [@BotFather](https://t.me/BotFather),
   send `/newbot`, and keep the token it gives you.

2. **A secret**, shared by the backend and the bridge:

       openssl rand -hex 32

3. **The backend, with the secret.** The demo passes its environment on to the
   server:

       export ROBINAUTS_CHANNELS_SECRET=<the secret>
       demo/start.sh

   Its log says `the channels endpoint is served`. Without the variable the
   endpoint does not exist, and the bridge says so when it starts.

4. **The bridge's settings.** In `channels/`:

       cp .env.example .env

   and set `ROBINAUTS_CHANNELS_SECRET` (the same secret),
   `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_ALLOWED_USERS` — your Telegram username
   is enough. `ROBINAUTS_AGENT_ID` is already the demo's LangGraph assistant.

5. **The bridge:**

       docker compose up --build

   It checks the deployment first and stops with a sentence if the URL, the
   secret or the agent is wrong. Then it logs
   `robinauts-channels: telegram to agent … at …`.

6. **Talk to your bot** on Telegram. The answer appears as a message that
   fills in while the agent writes. Somebody who is not on the allow list is
   told their id, so that you can add them.

The conversations belong to the Telegram users, not to the demo's local user,
so the web interface does not list them (linking a platform account to a person
who signs in is not done yet). The backend's log names each channel turn and
its conversation.

### On Linux

Docker on Linux does not reach the laptop's `127.0.0.1` through
`host.docker.internal`, and the demo listens on `127.0.0.1` only. Share the
host's network instead:

    docker build -t robinauts-channels .
    docker run --rm --network host --env-file .env \
        -e ROBINAUTS_API_URL=http://127.0.0.1:8000 \
        -v robinauts-channels:/data robinauts-channels

### Without Docker

With Node.js at the version in `.nvmrc`, which runs the TypeScript as it is:

    npm ci
    set -a; . ./.env; set +a
    ROBINAUTS_API_URL=http://127.0.0.1:8000 npm start

The thread file is then `state/threads.json`.

## Slack

Slack runs in Socket Mode: the bridge connects out to Slack, and needs no
public URL. Create an app at <https://api.slack.com/apps> "from a manifest":

```yaml
display_information:
  name: Robinauts
features:
  bot_user:
    display_name: Robinauts
    always_online: true
oauth_config:
  scopes:
    bot:
      - app_mentions:read
      - chat:write
      - assistant:write
      - im:history
      - im:read
      - im:write
      - channels:history
      - groups:history
      - users:read
settings:
  event_subscriptions:
    bot_events:
      - app_mention
      - message.im
  socket_mode_enabled: true
```

Install it to the workspace and set `SLACK_BOT_TOKEN` to its bot token
(`xoxb-…`). Under "Basic Information", generate an app-level token with the
`connections:write` scope, and set `SLACK_APP_TOKEN` to it (`xapp-…`). The bot
answers direct messages, and mentions in channels, in a thread. Everyone in the
workspace may use it unless `SLACK_ALLOWED_USERS` names member ids.

Both platforms can run at once, from the same bridge.

**Slack has not been tried against a real workspace yet**; Telegram has been
tried through its adapter with the Bot API stubbed, against a real backend.

## Configuration

Everything is an environment variable; `.env.example` lists them all with what
each is for. The bridge refuses to start, naming every problem at once, without
`ROBINAUTS_API_URL`, `ROBINAUTS_CHANNELS_SECRET` (at least 32 characters),
`ROBINAUTS_AGENT_ID` and at least one platform — and without an allow list for
Telegram, because anybody can find a Telegram bot and spend the deployment's
model budget through it.

One process at a time per Telegram bot: Telegram refuses a second one polling
the same token.

## What is not done

- **The shared secret.** Whoever holds it can speak for any platform user, but
  only for those: the users it creates are under a provider no sign-in
  configuration can name. It is to be replaced by tokens minted by an operator
  for one bridge each, naming the platform identities that bridge may assert
  (`docs/specs/channels.md`).
- **Thread links live in the bridge**, in `/data/threads.json`. Losing the file
  loses the links, not the conversations: a thread's next message starts a new
  one. The deployment is the better home for them.
- **Text only.** Images and files sent to the bot are dropped; the deployment
  reads the text of a message.
- **No stop button**, no account linking, no answers to people the allow list
  leaves out beyond telling them their id.

## Development

    npm ci
    npm run check     # prettier, tsc, and the tests: the SDK's fake platform, a fake deployment

`npm run format` rewrites what `prettier` would complain about. Dependencies
follow `../docs/contributing/js-dependencies.md`: exact versions, no install
scripts, and versions at least ten days old (`npm install --before=<date>`).

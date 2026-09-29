# Robinauts

An open source AI assistant platform that a company runs on its own
servers, leveraging the best capabilities from any vendor, on its own terms.

## Why Robinauts

AI assistants are becoming part of how work gets done. Robinauts lets a team
of any size adopt them without handing its long-term strategy to a single
vendor.

**Switch AI vendors at no cost, at any time.** Conversations are stored in
a vendor-agnostic format, so switching vendors takes one click, even in
the middle of a conversation. A company keeps its leverage in
contract talks, and can move all its work quickly for price, tax reasons
or an outage.

**Your knowledge stays yours.** Instructions, tools and conversations live
in the company's own configuration and database. Nothing is sent anywhere
except to the AI vendors and tools the company chose, on its own contracts.

**Little to run.** One application and one PostgreSQL database. No extra
services, no hosted dependencies. A platform team can own it without a new
team to look after it.

**Open standards.** Tools connect through MCP, and the interface talks to
the server through AG-UI, both open protocols. Tools built for other AI
products work here, and the parts of Robinauts can be replaced without
starting over.

**Safe to use, now and later.** Robinauts is Apache-2.0, with no added
restrictions, and every dependency is checked to keep it that way. A
company can run it as an internal platform, or build a SaaS or any other
commercial product on it, white-labelled under its own brand, at any time,
with no licence to renegotiate.

## What it does today

- Assistants defined by the company: a name, instructions, a default
  model, and the tools it may use.
- Sign-in with the company's Google or Okta account; an allow list decides
  who gets in.
- Conversations are private to the person who started them.
- A model picker: people choose from the models the company offers, and
  can change it at any point in a conversation.
- AI vendors: Anthropic and OpenAI directly. OpenRouter, which gives access to OpenAI, Google and many others. AWS Bedrock planned.
- Tools over MCP: assistants can call remote tool servers such as GitHub,
  Atlassian or Microsoft Learn. Each call is shown in the chat.
- UI for managing conversation history. Message editing and replaying.
- Built on the Pydantic AI agent framework, behind a boundary that keeps
  it replaceable; conversations are stored in Robinauts's own format
  beside the framework's transcript.


![Robinauts chat, with the model picker and the history panel](docs/images/robinauts-chat.png)

## Built for security and platform teams

**Licence safety.** Everything is Apache-2.0, safe to run internally and
safe to embed in a commercial product. Every dependency, including the
dependencies of dependencies, is checked against a strict allow list
modelled on the Apache Software Foundation's rules. GPL, AGPL, LGPL, SSPL,
"no commercial use" licences and packages with no licence at all break the
build. The same check covers the JavaScript that ships to the browser.
Every file in the repository states its licence, and every commit is
signed off by its author under the Developer Certificate of Origin.

**Supply chain.** Known vulnerabilities are checked on every change, for
both Python and JavaScript, and npm package signatures are verified. All
dependencies are locked, and the release package is installed with hash
checks. Automated updates wait ten days before they are proposed, to avoid
freshly compromised releases. CI actions are pinned to exact versions and
run with read-only permissions.

**Sign-in and access.** Sign-in uses OpenID Connect with the protections
current practice asks for (PKCE, state and nonce checks). Sessions are
kept on the server, cookies are locked to the site, and every change
request is checked for its origin. Every route checks that the person owns
what they ask for.

**Secrets.** API keys are read from environment variables. They are never
stored in the database, never written to a log, and never sent to the
browser. A missing key stops the server at start-up with a clear message.

**Nothing phones home.** No telemetry, no external CDN, and the tracing
services of the AI frameworks are switched off. The only outbound traffic
is to the identity provider at sign-in, the AI vendors, and the tool
servers the company configured.

**Tools under control.** Every tool call and its result is recorded in the
conversation as it happens, under the timeout the company set. Only tool
servers the company configured are reachable, over HTTPS, and the company's
credentials never leave the server.

**Fails early and clearly.** The configuration is checked when the server
starts: a misspelt setting, a missing key or a database that does not
match the application stops it with a message saying what to fix, instead
of running half-configured.

**Built to be changed.** Each outside component sits behind a boundary
that tests enforce, so replacing one breaks one known place and nothing
else.

## Deployment

One application and one PostgreSQL database. PostgreSQL is the only
dependency, and holds everything: conversations, users, sessions. Backing
up Robinauts means backing up one database.

Today the application ships as one Python package that includes the web
interface, placed behind the company's usual HTTPS reverse proxy.
[docs/deployment.md](docs/deployment.md) walks through it, step by step.

To try it on a laptop, one command starts a throwaway database and the
application, with no sign-in; it needs `uv`, Node.js and an Anthropic or
OpenRouter key ([demo/README.md](demo/README.md)):

    demo/start.sh          # demo/stop.sh takes it down

## Planned

- Prepare and stress test performance behind a load balancer.
- More AI vendors reached directly: Google Gemini and
  AWS Bedrock.
- Tools execution approval.
- Memories, stored in the company's database like conversations.
- Usage and cost reporting per model and per conversation, with an export.
- Administrators, projects shared by a team, share links, search and
  export.
- Retention periods, purge, and an audit log for the company's security
  tools.
- API tokens, and more channels: Slack and mobile.

## Licence

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
Contributing: [CONTRIBUTING.md](CONTRIBUTING.md).

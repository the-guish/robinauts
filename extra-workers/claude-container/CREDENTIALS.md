# Credentials: who makes them, where they go, and what Robinauts never does

The worker runs Claude Code, which authenticates to Anthropic with a credential.
**Robinauts does not make that credential, ask for it, refresh it, store it,
show it, copy it, or share it.** The person or the operator makes it with
Anthropic's own tools and puts it in an environment file the worker's process
inherits. Robinauts' configuration holds variable *names*; its database holds
nothing of it; its logs print nothing of it; and nothing of it crosses the
broker API to the server.

The same sentence governs a future Robinauts desktop: it may explain the steps
below and point at the file; it may not run `claude setup-token`, open or embed
the browser authorization, read `~/.claude/.credentials.json` or the macOS
keychain, mount a person's `~/.claude` into the container, copy a credential
anywhere, or use one person's credential for another person's turns.

There are two modes, and the worker refuses to start until the environment
says which one: `ROBINAUTS_CLAUDE_AUTH=personal` or
`ROBINAUTS_CLAUDE_AUTH=deployed`.

## What Anthropic says

Quoted so that the rule is Anthropic's, not ours. Verify the reading against the
current [Consumer Terms](https://www.anthropic.com/legal/consumer-terms) and
[Commercial Terms](https://www.anthropic.com/legal/commercial-terms) before a
release; this file was last checked against the documentation on 2026-11-07.

- Agent SDK quickstart: *"Unless previously approved, Anthropic does not allow
  third party developers to offer claude.ai login or rate limits for their
  products, including agents built on the Claude Agent SDK. Use the API key
  authentication methods described in this document instead."*
- Agent SDK, session storage: *"if you run the agent yourself under your own
  Claude subscription, set `CLAUDE_CODE_OAUTH_TOKEN` in `env` to a token from
  `claude setup-token`."*
- Claude Code, authentication: `claude setup-token` *"opens the same browser
  authorization flow as `/login`, and the token prints to the terminal after
  you approve access in the browser. It does not save the token anywhere"*; the
  token is a one-year token that *"authenticates with your Claude subscription
  and requires a Pro, Max, Ultra, Team, or Enterprise plan"* and *"can only
  make model requests"*. When several credentials are present, Claude Code
  picks `ANTHROPIC_AUTH_TOKEN`, then `ANTHROPIC_API_KEY`, then an
  `apiKeyHelper`, then `CLAUDE_CODE_OAUTH_TOKEN`.

Our reading: a person who runs their own Robinauts, for themselves, under their
own subscription, is the second quote. A Robinauts that offers a "sign in with
Claude" button, pools a token, or runs one person's token for other people's
turns is the first. The two modes keep the product on the right side of that
line. **You are responsible for your own plan's terms**, in either mode.

## The two modes

| | `personal` | `deployed` |
|---|---|---|
| Who | One person, on their own machine ("Robinauts desktop") | An instance with sign-in, several people |
| Server | `robinauts start --dev-no-sign-in`, on the same machine | A deployment ([docs/deployment.md](../../docs/deployment.md)) |
| Credential | A subscription token the person made with `claude setup-token` | A Claude Platform API key the operator made in the [Claude Console](https://platform.claude.com/) |
| Variable | `CLAUDE_CODE_OAUTH_TOKEN` | The one the provider's `api_key_env` names, usually `ANTHROPIC_API_KEY` |
| Where it lives | `personal.env`, the person's, mode `0600` | `/etc/robinauts/claude-container.env`, root's, mode `0600`, or an orchestrator secret |
| Whose spend | The person's plan, with its rate limits | The deployment's key |
| Model providers | Kind `anthropic`, no `base_url` | Kind `anthropic`, or `anthropic-compatible` with a gateway's `base_url` |
| Refused | Any API key beside the token; a server that is not on this machine; any endpoint but Anthropic's | A subscription token in the environment; the cloud-provider variables (not in this version) |

### Personal

1. Have a Claude Pro, Max, Ultra, Team or Enterprise subscription, and
   [Claude Code installed](https://code.claude.com/docs/en/setup) on your
   machine, logged in as yourself.
2. In **your own terminal**, run

       claude setup-token

   approve access in the browser, and copy the token it prints. Robinauts
   never runs this command for you, and the token is saved nowhere by it.
3. Put it in `personal.env` beside the compose file, mode `0600`, as
   `CLAUDE_CODE_OAUTH_TOKEN=...`, with the worker's secret
   (`personal.env.example` shows both lines).
4. Set nothing else that authenticates: no `ANTHROPIC_API_KEY`, no
   `ANTHROPIC_AUTH_TOKEN`, no `ANTHROPIC_BASE_URL`, none of the provider keys
   your `robinauts.toml` names. Each would outrank the token, so the worker
   refuses to start while one is set.
5. The worker also refuses a server that is not on this machine: loopback,
   `host.docker.internal`, a private address or a unix socket are fine, a
   public name is not. A personal token serves the person's own instance alone.

The token lasts a year. When Anthropic refuses it, a turn fails with an error
the server's operator (you) reads, which names `CLAUDE_CODE_OAUTH_TOKEN` and
never its value: run `claude setup-token` again and replace the line. Revoke it
in your claude.ai account whenever you like; Robinauts cannot.

### Deployed

1. The operator creates an API key in the
   [Claude Console](https://platform.claude.com/), as they would for the
   server's own agents.
2. They write it to `/etc/robinauts/claude-container.env`, owned by root, mode
   `0600`, under the variable name the provider's `api_key_env` gives in
   `robinauts.toml`, beside the worker's secret (`deployed.env.example`).
3. No `CLAUDE_CODE_OAUTH_TOKEN` anywhere near it: a subscription token is one
   person's, and Anthropic does not allow a product to serve others on it. The
   worker refuses to start with one.

One identity per deployment, as for the server's tool servers
([docs/specs/operations.md](../../docs/specs/operations.md)): every person's
turns spend from that key, and Anthropic's logs name the deployment, not the
person. Bedrock, Vertex and Foundry through Claude Code's own variables are the
same principle, the operator's cloud credentials, and not in this version: the
worker refuses `CLAUDE_CODE_USE_*` so that nothing runs on a credential it did
not check.

## What the worker checks, and what it never does

At its start, before it claims a turn, the worker reads which of the variables
above are **set**, never their values beyond presence, and refuses to start
with every problem named at once (exit code 2). While it runs, the Claude Code
subprocess inherits the environment; in deployed mode the worker adds the
provider's key to that subprocess's environment and nothing else; in personal
mode it adds nothing. The credential appears in no log line, no turn event, no
error text and no request to the server.

## Troubleshooting

| what you see | why | what to do |
|---|---|---|
| `ROBINAUTS_CLAUDE_AUTH must be 'personal' or 'deployed'` | the mode is not chosen | choose it; there is no default on purpose |
| `personal mode needs CLAUDE_CODE_OAUTH_TOKEN` | the token is not in the environment | run `claude setup-token` yourself and put the token in `personal.env` |
| `personal mode refuses ANTHROPIC_API_KEY` (or another key) | a key beside the token would outrank it | remove the key from the worker's environment |
| `personal mode refuses provider '…'` | the provider is not plain Anthropic | a subscription token goes to Anthropic alone: kind `anthropic`, no `base_url` |
| `personal mode serves the person's own instance alone, and … is not on this machine` | the broker URL is a public name | run the server on this machine, or use deployed mode with an API key |
| `deployed mode refuses CLAUDE_CODE_OAUTH_TOKEN` | a personal token in a deployment's environment | remove it; create an API key in the Console |
| `deployed mode reads the API key of provider '…' from X, which is unset or empty` | the key variable is not in the environment file | set it, under the name `api_key_env` gives |
| a turn fails with `Anthropic refused the subscription token …` | the token expired (a year) or was revoked | `claude setup-token` again; replace the value |
| a turn fails with `Anthropic refused the API key in X` | the key was revoked, or is another organization's | check it in the Console; replace the value |

# Credentials

This worker runs on one kind of credential: the operator's API key of each model
provider. Robinauts does not make, request, refresh, store, print or share it.
You make it, with the provider's own tools.

## The API key

| | |
|---|---|
| What it is | A key from the provider: Anthropic, OpenAI, or a gateway that speaks their APIs |
| Where it is | The variable the provider's `api_key_env` names, as for the server's own agents |
| Who makes it | The operator, or the person running their own instance |

## What this image does

- The worker reads each key with the server's own lookup, and puts it in
  OpenCode's configuration. The configuration is in OpenCode's environment
  alone, never in a file or a log.
- The worker refuses to start when a provider's variable is unset or empty, or
  when an `openai-compatible` provider names no `base_url`.
- OpenCode's own logins are not used: `opencode auth login`, its `auth.json` and
  its login plugins. The worker refuses to start with `OPENCODE_AUTH_CONTENT`
  set. A subscription login is one person's.
- OpenCode's server listens on loopback, behind a password the worker makes at
  every start. That password is the process's own, and dies with it.

## Your responsibility

You make the keys, and you keep them where the worker's command reads them.
Read each provider's terms for the keys you use.

# Credentials

Codex accepts two credentials, and this image supports both. Which one may be
used depends on who it serves. Robinauts does not make, request, refresh,
store, print or share either of them. You make it, with OpenAI's own tools.

## The two credentials

| | API key | ChatGPT login |
|---|---|---|
| Mode | `deployed` | `personal` |
| What it is | A key from the provider, OpenAI's platform or a gateway | A sign-in to a ChatGPT plan, made by `codex login` |
| Where it is | The variable the provider's `api_key_env` names | `$CODEX_HOME/auth.json`, in the home volume |
| Whose spend | The key's account, per token | The plan of the person who signed in |

## A credential that serves more than one user is an API key

A ChatGPT login is one person's, and its plan is for that person's own use. An
instance that serves several people runs in deployed mode, on an API key. In
doubt, use an API key.

## What this image does

### Deployed

- Each provider of the worker's agents is a model provider of Codex's own
  making, with `env_key` set to the provider's `api_key_env`. Codex reads the
  key from that variable on every request.
- The key is in Codex's environment, never on its command line.
- The worker refuses to start when a provider's variable is unset or empty,
  when a provider is not of kind `openai` or `openai-compatible`, or when a
  ChatGPT login is in `CODEX_HOME`.

### Personal

- Codex runs on its own `openai` provider, with `forced_login_method` set to
  `chatgpt`.
- You sign in yourself, into the home volume, with
  `codex login --device-auth` (README.md, "Personal use"). Codex refreshes the
  login in that file, so the volume is writable.
- The worker refuses to start without the login file, with a provider other
  than plain OpenAI, or with a server that is not on this machine.

Both modes refuse `CODEX_ACCESS_TOKEN`, which this version does not support.

## Your responsibility

You choose the mode, and you make the credential. Read OpenAI's terms for the
plan or the key you use, and keep a personal login on your own machine.

# Credentials

Claude Code accepts two credentials, and this image ships both. Which one may
be used depends on who it serves. Robinauts does not make, request, refresh,
store, print, or share either of them. You make it, with Anthropic's own
tools, and the process inherits it.

The sentences quoted here were read on 7 November 2026. The pages move. The
last section is the part that stays in force when they do.

## The two credentials

| | API key | Subscription token |
|---|---|---|
| Variable | `ANTHROPIC_API_KEY` | `CLAUDE_CODE_OAUTH_TOKEN` |
| What it is | A key from the [Claude Console](https://platform.claude.com/) | A token printed by `claude setup-token` |
| Whose spend | The key's account, per token | The plan of the person who created the token |
| Plans | A Console account | Pro, Max, Ultra, Team, or Enterprise |
| In the GitHub Action | input `anthropic_api_key` | input `claude_code_oauth_token` |

When both are present, Claude Code uses the API key and ignores the token.
The order is in [Authentication](https://code.claude.com/docs/en/authentication):
cloud credentials, then `ANTHROPIC_AUTH_TOKEN`, then `ANTHROPIC_API_KEY`, then
an `apiKeyHelper`, then `CLAUDE_CODE_OAUTH_TOKEN`, then a `/login` session.

## A credential that serves more than one user is an API key

When the same credential is shared across more than one user, the
authentication is an `ANTHROPIC_API_KEY` from the Claude Console. In doubt,
that is the one to use.

Anthropic's own [GitHub Actions](https://code.claude.com/docs/en/github-actions)
page draws the line at a secret that outlives one person:

> For a secret shared across repositories, authenticate with an API key from
> the Claude Console rather than an OAuth token, since an OAuth token is tied
> to the subscription of the person who ran `claude setup-token`.

The [Consumer Terms](https://www.anthropic.com/legal/consumer-terms) (8 October
2025) say you may not share account login information or account credentials
with anyone else. [Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance)
tells developers building products, including on the Agent SDK, to use an API
key, and does not permit them to route requests through Free, Pro, Max, or
Ultra credentials on behalf of their users, or to collect, store, or
intermediate Claude.ai credentials. The [Agent SDK
quickstart](https://code.claude.com/docs/en/agent-sdk/quickstart) and the
[overview](https://code.claude.com/docs/en/agent-sdk/overview) repeat the
product rule: unless previously approved, a third party may not offer
claude.ai login or rate limits for its product.

Companies that run the official `claude` binary for other people follow that
rule, and they say so.

[NVIDIA OpenShell](https://docs.nvidia.com/openshell/v0.0.116/get-started/quickstart)
launches `claude` inside a sandbox and injects `ANTHROPIC_API_KEY`. The
quickstart, and the [provider
docs](https://docs.nvidia.com/openshell/how-it-works/providers/overview), say:

> `ANTHROPIC_API_KEY` is an API key from console.anthropic.com, not a
> subscription token. Subscription users must generate a separate API key.

The shipped
[profile](https://github.com/NVIDIA/OpenShell/blob/main/providers/claude-code.yaml)
sends that key as `x-api-key` and names `/usr/bin/claude` as the binary
allowed to carry it. Its smoke test is `claude -p`.

[Claude Code on Amazon Bedrock](https://code.claude.com/docs/en/amazon-bedrock)
documents the unattended case as cloud credentials, not a subscription token:
set the variables "instead of the wizard, for example in CI or a scripted
enterprise rollout", with `CLAUDE_CODE_USE_BEDROCK=1` and the AWS credential
chain. AWS's [Guidance for Claude Code with Amazon
Bedrock](https://github.com/aws-solutions-library-samples/guidance-for-claude-code-with-amazon-bedrock)
distributes the official CLI plus a `credential-process` that turns each
developer's own IdP login into temporary credentials for Bedrock. Anthropic's
[GitHub Action on cloud
providers](https://code.claude.com/docs/en/github-actions-cloud-providers)
does the same for a workflow: `use_bedrock: "true"`, and the job assumes an
IAM role with the workflow's own OIDC token. No Claude credential is stored.

## One person, driving their own instance

The exception is one seat and the same user. One person runs a Claude Code
instance for their own tasks, and they are the only one who can drive it.
Then `CLAUDE_CODE_OAUTH_TOKEN` is the credential the current documentation
describes.

[Authentication](https://code.claude.com/docs/en/authentication), under
"Generate a long-lived token":

> For CI pipelines, scripts, or other environments where interactive browser
> login isn't available, generate a one-year OAuth token with `claude
> setup-token`.
>
> This token authenticates with your Claude subscription and requires a Pro,
> Max, Ultra, Team, or Enterprise plan. It can only make model requests.

[Session storage](https://code.claude.com/docs/en/agent-sdk/session-storage),
for a run whose config directory is not the one you logged in to:

> if you run the agent yourself under your own Claude subscription, set
> `CLAUDE_CODE_OAUTH_TOKEN` in `env` to a token from `claude setup-token`.

[Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance)
keeps that use with the person who bought the plan. Usage limits for Pro,
Max, and Ultra "assume ordinary, individual usage of Claude Code and the
Agent SDK", and the credential section says the restrictions on third-party
products do not prevent

> an end user from signing in to the unmodified Claude Code binary with their
> own Claude subscription, including where a platform hosts Claude Code.

The help article [Use the Claude Agent SDK with your Claude
plan](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)
says that, signed in with the plan, the Agent SDK and `claude -p` still draw
from the subscription limits (7 October 2026). A paused change, noted there
on 15 June 2026, would have moved the Agent SDK, `claude -p`, and third-party
app usage off those limits. The article records the pause. It is a billing
note, and it is one of the pages that moves.

You control the login, and you keep the credential inside your own activity.
You run `claude setup-token` or `claude auth login` yourself. You do not put
the token where another person can spend it, and you do not point it at an
instance someone else can drive.

That is how [T3 Code](https://github.com/pingdotgg/t3code) does it. Its
[install](https://github.com/pingdotgg/t3code/blob/main/docs/user/install.md)
says to install Claude Code and run `claude auth login` on the machine that
runs the agent. The [provider
page](https://github.com/pingdotgg/t3code/blob/main/docs/user/providers-claude.md)
uses that login and that config directory; a second account is a second
`claude auth login` into another directory. T3 does not mint the token and
does not keep a copy. The
[FAQ](https://github.com/pingdotgg/t3code/blob/main/docs/user/faq.md) answers
"Can I use T3 Code with my Claude Code subscription?" with "Yes."

## Automated work may use either, and the Action says when

The [Claude Code GitHub Action](https://github.com/anthropics/claude-code-action)
runs Claude Code, on the Agent SDK, with nobody at the keyboard: a mention of
`@claude`, or a `prompt` on any event, including a cron schedule. Anthropic's
[GitHub Actions](https://code.claude.com/docs/en/github-actions) page allows
both credentials for that.

Quick setup, when Claude Code has no API key:

> choose between creating a long-lived token with your Claude subscription
> and pasting in an API key.

Manual setup names the two secrets. `ANTHROPIC_API_KEY` is "a Claude API key
from the Claude Console". `CLAUDE_CODE_OAUTH_TOKEN` is

> an OAuth token that authenticates with your Claude subscription, available
> on Pro, Max, Ultra, Team, and Enterprise plans. Generate one by running
> `claude setup-token` locally.

The examples pass `anthropic_api_key`. For a subscription, the page says to
replace that line with `claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}`.
The action's own
[setup](https://github.com/anthropics/claude-code-action/blob/main/docs/setup.md)
documents the same secret. The token is the action input
`claude_code_oauth_token`, under `with:`.

The page does not split the two credentials by interactive against automated.
It splits them by whose subscription pays, and it says what the token does to
the bill:

> If you authenticate with an OAuth token, runs use your Claude subscription
> instead of API billing.

The shared-repository sentence quoted above is the Action's rule for when the
token is the wrong one. A third path, workload identity federation, stores
neither secret: the workflow's OIDC token is exchanged for Claude API access
through a Console service account.

## What this image does

The image makes the choice explicit. `ROBINAUTS_CLAUDE_AUTH` is `personal` or
`deployed`, and it has no default: a guess would hide the decision. When the
scenario is in doubt, set `deployed` and use the API key.

The worker reads which variables are set and refuses to start, with every
problem named, until the environment matches the mode. That is a best effort.
It can see the environment and the address of the server. It cannot see
whether a second person can reach the chat. A mode that starts is not a
finding that the deployment satisfies the terms.

### Deployed

An instance with sign-in, several people, or any case that is not the
single-user exception above. The credential is the API key named by the model
provider's `api_key_env`, usually `ANTHROPIC_API_KEY`, created in the Claude
Console. A `CLAUDE_CODE_OAUTH_TOKEN` in that environment stops the start: one
person's subscription token does not serve a deployment. `ANTHROPIC_AUTH_TOKEN`
and the `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`, and
`CLAUDE_CODE_USE_FOUNDRY` switches are refused as well. This version does not
claim to check them.

The provider is kind `anthropic`, or `anthropic-compatible` with a gateway's
`base_url`. The key is the operator's, mode `0600`, in
`/etc/robinauts/claude-container.env` or an orchestrator secret
(`deployed.env.example`).

### Personal

The exception. One person, their own machine, their own tasks, and the only
one who can drive this instance. The credential is `CLAUDE_CODE_OAUTH_TOKEN`,
from `claude setup-token` run in their own terminal. The worker adds nothing
to the environment the Claude Code subprocess inherits: the token is already
there.

It refuses to start if any of these is true:

- the token is missing
- an API key is set beside it (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, a
  provider's `api_key_env`, or a `CLAUDE_CODE_USE_*` switch), because that
  credential would outrank the token
- a provider is not plain Anthropic, kind `anthropic` with no `base_url`, or
  `ANTHROPIC_BASE_URL` is set
- the server is not on this machine: loopback, a private or link-local
  address, `host.docker.internal`, or a unix socket

The token lasts a year and can only make model requests. When Anthropic
refuses it, the turn fails naming `CLAUDE_CODE_OAUTH_TOKEN` and never its
value. Run `claude setup-token` again and replace the line. The file is
`personal.env`, mode `0600` (`personal.env.example`).

## Your responsibility

These checks are a sane default. They are not the terms.

You review the current pages and you decide whether your setup matches the
terms that apply to you. Start with [Legal and
compliance](https://code.claude.com/docs/en/legal-and-compliance),
[Authentication](https://code.claude.com/docs/en/authentication), the [Agent
SDK quickstart](https://code.claude.com/docs/en/agent-sdk/quickstart), [Claude
Code GitHub Actions](https://code.claude.com/docs/en/github-actions), and the
[Consumer Terms](https://www.anthropic.com/legal/consumer-terms). A newer
version of any of them supersedes a quotation on this page.
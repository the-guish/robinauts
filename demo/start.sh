#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
#
# The demo: one command that brings the whole thing up on this machine.
# demo/stop.sh takes it down again, and demo/README.md is the whole of what it
# does and what it reads.
#
# It is the **local development mode** -- no sign-in at all, one fixed local
# user, the loopback interface only -- and it is not a way to deploy anything
# (docs/deployment.md).
#
# It needs one model provider key, and the only three variables it will look
# at are OPENROUTER_API_KEY, ANTHROPIC_API_KEY and OPENAI_API_KEY, from the
# environment or from demo/.env. A GitHub token exported as ROBINAUTS_GITHUB_TOKEN is optional,
# and turns on GitHub's MCP server for both agents.
# **No key or token is ever printed or written to a file**: the configuration
# names the *variable*, and the secret travels in the environment of the
# server this starts (docs/specs/agents.md).
#
# Every failure is one line and a non-zero exit: 2 for something to put right
# before running it again, 1 for something that went wrong while starting.
set -eu

# The keys and the token, taken **out of the environment before anything else
# runs**. Everything this script starts before the server -- nvm and the node
# it may run, uv, pgserver, npm and everything npm runs -- is a program with no
# business holding the operator's key, and an exported variable is inherited
# by every one of them. So they are copied into shell variables, which no child
# process sees, and unset here, as the first thing the script does; whether one
# is there at all, and demo/.env, are looked at later, where they always were
# (below). The server is handed the one key it needs on its own invocation and
# nowhere else, which is what makes the promise at the top of this file true
# rather than nearly true.
openrouter_env=${OPENROUTER_API_KEY:-}
anthropic_env=${ANTHROPIC_API_KEY:-}
openai_env=${OPENAI_API_KEY:-}
github_token=${ROBINAUTS_GITHUB_TOKEN:-}
unset OPENROUTER_API_KEY ANTHROPIC_API_KEY OPENAI_API_KEY ROBINAUTS_GITHUB_TOKEN

demo=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# shellcheck source=demo/common.sh
. "$demo/common.sh"

ENV_FILE="$demo/.env"
ENV_MODE=600
# demo/.env may hold a key, so it is read only when nobody else can read it.

GITHUB_VARIABLE=ROBINAUTS_GITHUB_TOKEN
# The variable the platform reads GitHub's token from, as [tool_servers.github]
# names it. Without it in the environment, the agents have no tools.

OPENROUTER_VARIABLE=OPENROUTER_API_KEY
ANTHROPIC_VARIABLE=ANTHROPIC_API_KEY
OPENAI_VARIABLE=OPENAI_API_KEY

OPENROUTER_BASE_URL="https://openrouter.ai/api"
# The prefix the Anthropic client appends /v1/messages to, which is why it stops
# at /api. OpenRouter serves Anthropic's Messages API, so this build reaches it
# as an `anthropic-compatible` provider (docs/specs/agents.md).

# The three models the picker offers: the id each has in the configuration, the
# vendor's name for it, which ROBINAUTS_DEMO_MODEL, ROBINAUTS_DEMO_MODEL_2 and
# ROBINAUTS_DEMO_MODEL_3 override, and the title the picker shows. OpenRouter's
# names are `<vendor>/<model>` and reach every vendor, so it offers three;
# Anthropic's are its own, and it serves Claude alone, so its three are all
# Claude; OpenAI's likewise, and its three are all GPT. **An id names one model,
# whichever key reaches it**: Claude Sonnet 5 is `claude-sonnet-5` through
# OpenRouter and through Anthropic, and GPT-5.5 is `gpt-5-5` through
# OpenRouter and through OpenAI, so that a conversation carries on across a
# restart on another key when that key offers its model too; every other
# model has an id of its own, so that a conversation on a model the other key
# does not offer is refused as not offered rather than answered by another
# (docs/specs/agents.md). An override keeps the id. A model that has been
# retired is a turn on it that fails saying `not found`, and README.md says
# which variable to set then.
DEFAULT_ID=claude-sonnet-5
DEFAULT_TITLE="Claude Sonnet 5"
DEFAULT_OPENROUTER_MODEL="anthropic/claude-sonnet-5"
DEFAULT_ANTHROPIC_MODEL="claude-sonnet-5"
OPENROUTER_ID_2=gpt-5-5
OPENROUTER_MODEL_2="openai/gpt-5.5"
OPENROUTER_TITLE_2="GPT-5.5"
OPENROUTER_ID_3=gemini-3-8-flash
OPENROUTER_MODEL_3="google/gemini-3.8-flash"
OPENROUTER_TITLE_3="Gemini 3.8 Flash"
ANTHROPIC_ID_2=claude-opus-5-5
ANTHROPIC_MODEL_2="claude-opus-5-5"
ANTHROPIC_TITLE_2="Claude Opus 5.5"
ANTHROPIC_ID_3=claude-haiku-4-5
ANTHROPIC_MODEL_3="claude-haiku-4-5"
ANTHROPIC_TITLE_3="Claude Haiku 4.5"
# OpenAI's own names, over Chat Completions: its flagship first, which is
# also OpenRouter's second model and so has that id, then the smaller and the
# smallest of the GPT-5.4 family, which do not reason unless asked. Not GPT-6
# and not GPT-5.6: OpenAI documents function calling over Chat Completions on
# GPT-6 Sol and Luna only with reasoning turned off, OpenAI Support reported
# GPT-5.6 Sol refusing function tools with reasoning on (2026-09-07), and
# GPT-5.6 Luna and Terra reason by default and are untried. The engines do not
# turn reasoning off, so the GitHub tools could fail on any of them
# (docs/specs/agents.md, "Known findings").
OPENAI_ID=$OPENROUTER_ID_2
OPENAI_MODEL="gpt-5.5"
OPENAI_TITLE=$OPENROUTER_TITLE_2
OPENAI_ID_2=gpt-5-4-mini
OPENAI_MODEL_2="gpt-5.4-mini"
OPENAI_TITLE_2="GPT-5.4 Mini"
OPENAI_ID_3=gpt-5-4-nano
OPENAI_MODEL_3="gpt-5.4-nano"
OPENAI_TITLE_3="GPT-5.4 Nano"

HEALTH_SECONDS=120
# Generous on purpose: a first start has just built an interface, and the server
# still has a schema to check before it answers.

HEALTH_TRIES_PER_SECOND=4

# --- who may run this, and with what -----------------------------------------

[ "$(id -u)" -ne 0 ] ||
    fail "demo/start.sh is not run as root: run it as yourself." 2

command -v uv >/dev/null 2>&1 ||
    fail "uv is not on the PATH: see https://docs.astral.sh/uv/getting-started/" 2

# Node is needed because the interface is built from source here. nvm, if this
# machine has it, is asked for the version frontend/.nvmrc names, exactly as
# frontend/README.md tells a developer to. `set -eu` is lifted around it: nvm's
# own script is not written to be read under either.
: "${NVM_DIR:=$HOME/.nvm}"
if ! command -v node >/dev/null 2>&1 && [ -s "$NVM_DIR/nvm.sh" ]; then
    set +eu
    # shellcheck source=/dev/null
    . "$NVM_DIR/nvm.sh" >/dev/null 2>&1
    nvm use "$(cat "$root/frontend/.nvmrc")" >/dev/null 2>&1 || nvm use >/dev/null 2>&1
    set -eu
fi

wanted_node=$(cat "$root/frontend/.nvmrc")
command -v node >/dev/null 2>&1 ||
    fail "node is not on the PATH: install Node.js $wanted_node, or 'nvm use' in frontend/." 2

# --- is it already up? -------------------------------------------------------
#
# Asked before anything is read or started, so that running this twice is a
# sentence rather than a second server. It needs no key and no database.

running=$(server_pid)
if [ -n "$running" ]; then
    say "Already running on http://$HOST:$PORT/ (pid $running); demo/stop.sh stops it."
    exit 0
fi

# --- the key ------------------------------------------------------------------

from_env_file() {
    # The last assignment of $1 in demo/.env, or nothing. The file is **read,
    # not sourced**: a .env that could run commands would be this script
    # executing whatever somebody pasted into it. `export NAME=` is accepted,
    # because that is how people write these files, and one pair of surrounding
    # quotes is removed with any trailing carriage return.
    [ -f "$ENV_FILE" ] || return 0
    sed -n "s/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}$1=//p" "$ENV_FILE" |
        tail -n 1 |
        sed -e 's/\r$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}

if [ -f "$ENV_FILE" ]; then
    mode=$(file_mode "$ENV_FILE")
    [ "$mode" = "$ENV_MODE" ] ||
        fail "$ENV_FILE is mode ${mode:-unreadable} and may hold a key: chmod $ENV_MODE it first." 2
fi

case "${ROBINAUTS_DEMO_PROVIDER:-}" in
"" | openrouter | anthropic | openai) ;;
*)
    fail "ROBINAUTS_DEMO_PROVIDER is 'openrouter', 'anthropic', 'openai', or not set at all." 2
    ;;
esac

# The environment's value first, as it was read at the top of the script
# (where the variables were also unset), and demo/.env's when it has none.
openrouter_key=${openrouter_env:-$(from_env_file "$OPENROUTER_VARIABLE")}
anthropic_key=${anthropic_env:-$(from_env_file "$ANTHROPIC_VARIABLE")}
openai_key=${openai_env:-$(from_env_file "$OPENAI_VARIABLE")}
unset openrouter_env anthropic_env openai_env

if [ -z "$openrouter_key" ] && [ -z "$anthropic_key" ] && [ -z "$openai_key" ]; then
    # The one message this script prints over several lines, because what to do
    # about it is a command and a choice.
    cat >&2 <<MISSING
The demo needs one model provider key. Set one of these and run it again:

    export $OPENROUTER_VARIABLE=...      # https://openrouter.ai/keys
    export $ANTHROPIC_VARIABLE=...       # https://console.anthropic.com/
    export $OPENAI_VARIABLE=...          # https://platform.openai.com/api-keys

or put the line in $ENV_FILE (chmod $ENV_MODE; it is never committed).
With more than one set the demo uses OpenRouter, then Anthropic, then
OpenAI; ROBINAUTS_DEMO_PROVIDER=anthropic or =openai chooses another.
MISSING
    exit 2
fi

if [ "${ROBINAUTS_DEMO_PROVIDER:-}" = anthropic ] && [ -z "$anthropic_key" ]; then
    fail "ROBINAUTS_DEMO_PROVIDER=anthropic, but $ANTHROPIC_VARIABLE is not set." 2
fi
if [ "${ROBINAUTS_DEMO_PROVIDER:-}" = openrouter ] && [ -z "$openrouter_key" ]; then
    fail "ROBINAUTS_DEMO_PROVIDER=openrouter, but $OPENROUTER_VARIABLE is not set." 2
fi
if [ "${ROBINAUTS_DEMO_PROVIDER:-}" = openai ] && [ -z "$openai_key" ]; then
    fail "ROBINAUTS_DEMO_PROVIDER=openai, but $OPENAI_VARIABLE is not set." 2
fi

title_of() {
    # The title the picker shows for model $1: $3 when it is the default $2, and
    # nothing for a model the operator chose, since a title written here for
    # another model would name the wrong one. demo/config.py calls that one by
    # the vendor's name, cut to the length a title may have.
    if [ "$1" = "$2" ]; then
        printf '%s\n' "$3"
    fi
}

# Which key, when more than one is set and none is chosen: OpenRouter first,
# because it is the one key a reader of README.md is most likely to have and
# it reaches every vendor's models; then Anthropic, then OpenAI, each of which
# reaches its own vendor alone. The order is the order the demo grew in and
# ranks nothing else. ROBINAUTS_DEMO_PROVIDER chooses outright, and was
# checked above to name a key that is set.
chosen=${ROBINAUTS_DEMO_PROVIDER:-}
if [ -z "$chosen" ]; then
    if [ -n "$openrouter_key" ]; then
        chosen=openrouter
    elif [ -n "$anthropic_key" ]; then
        chosen=anthropic
    else
        chosen=openai
    fi
fi

if [ "$chosen" = openrouter ]; then
    provider_id=openrouter
    provider_kind=anthropic-compatible
    key_variable=$OPENROUTER_VARIABLE
    base_url=$OPENROUTER_BASE_URL
    id=$DEFAULT_ID
    model=${ROBINAUTS_DEMO_MODEL:-$DEFAULT_OPENROUTER_MODEL}
    title=$(title_of "$model" "$DEFAULT_OPENROUTER_MODEL" "$DEFAULT_TITLE")
    id_2=$OPENROUTER_ID_2
    model_2=${ROBINAUTS_DEMO_MODEL_2:-$OPENROUTER_MODEL_2}
    title_2=$(title_of "$model_2" "$OPENROUTER_MODEL_2" "$OPENROUTER_TITLE_2")
    id_3=$OPENROUTER_ID_3
    model_3=${ROBINAUTS_DEMO_MODEL_3:-$OPENROUTER_MODEL_3}
    title_3=$(title_of "$model_3" "$OPENROUTER_MODEL_3" "$OPENROUTER_TITLE_3")
    key_value=$openrouter_key
elif [ "$chosen" = anthropic ]; then
    provider_id=anthropic
    provider_kind=anthropic
    key_variable=$ANTHROPIC_VARIABLE
    # `anthropic` has one endpoint and both engines pin it; a base_url there is
    # a start-up refusal, so there is none to write.
    base_url=
    id=$DEFAULT_ID
    model=${ROBINAUTS_DEMO_MODEL:-$DEFAULT_ANTHROPIC_MODEL}
    title=$(title_of "$model" "$DEFAULT_ANTHROPIC_MODEL" "$DEFAULT_TITLE")
    id_2=$ANTHROPIC_ID_2
    model_2=${ROBINAUTS_DEMO_MODEL_2:-$ANTHROPIC_MODEL_2}
    title_2=$(title_of "$model_2" "$ANTHROPIC_MODEL_2" "$ANTHROPIC_TITLE_2")
    id_3=$ANTHROPIC_ID_3
    model_3=${ROBINAUTS_DEMO_MODEL_3:-$ANTHROPIC_MODEL_3}
    title_3=$(title_of "$model_3" "$ANTHROPIC_MODEL_3" "$ANTHROPIC_TITLE_3")
    key_value=$anthropic_key
else
    provider_id=openai
    provider_kind=openai
    key_variable=$OPENAI_VARIABLE
    # `openai` has one endpoint too, https://api.openai.com/v1, pinned by both
    # engines, so there is no base_url to write either.
    base_url=
    id=$OPENAI_ID
    model=${ROBINAUTS_DEMO_MODEL:-$OPENAI_MODEL}
    title=$(title_of "$model" "$OPENAI_MODEL" "$OPENAI_TITLE")
    id_2=$OPENAI_ID_2
    model_2=${ROBINAUTS_DEMO_MODEL_2:-$OPENAI_MODEL_2}
    title_2=$(title_of "$model_2" "$OPENAI_MODEL_2" "$OPENAI_TITLE_2")
    id_3=$OPENAI_ID_3
    model_3=${ROBINAUTS_DEMO_MODEL_3:-$OPENAI_MODEL_3}
    title_3=$(title_of "$model_3" "$OPENAI_MODEL_3" "$OPENAI_TITLE_3")
    key_value=$openai_key
fi
say "Provider: $provider_kind ($provider_id); models $model, $model_2, $model_3; key from $key_variable."
if [ -n "$github_token" ]; then
    github_env=$GITHUB_VARIABLE
    say "Tools: GitHub's MCP server, for both agents."
else
    github_env=
    say "Tools: none (no GitHub token found)."
fi

# --- the database ------------------------------------------------------------

mkdir -p "$state"
say "Starting the demo's own PostgreSQL under $PGDATA ..."
database_url=$(pg start) ||
    fail "the demo's PostgreSQL would not start; its own log is $PGDATA/log." 1
export ROBINAUTS_DATABASE_URL="$database_url"
say "Database: $database_url"

# --- the configuration -------------------------------------------------------

# By demo/config.py rather than by `sed`: the model names are the values here a
# person types, `sed` would read a `&`, a `|` or a backslash in one as part of
# its own language, and a value that broke out of the string it was written
# into would be a configuration that means something nobody asked for.
# config.py substitutes literally, refuses what TOML cannot hold, and reads the
# file back to prove that each value arrived whole.
uv run --no-project --python "$PYTHON" python "$demo/config.py" \
    --template "$demo/robinauts.toml.in" --out "$CONFIG" \
    --provider-id "$provider_id" --kind "$provider_kind" \
    --key-variable "$key_variable" \
    --model "$id" "$model" "$title" \
    --model "$id_2" "$model_2" "$title_2" \
    --model "$id_3" "$model_3" "$title_3" \
    --base-url "$base_url" --github-secret-env "$github_env" ||
    fail "$CONFIG could not be written." 1
export ROBINAUTS_CONFIG="$CONFIG"

# --- the interface, built from source ----------------------------------------

if [ ! -f "$root/frontend/dist/index.html" ]; then
    if [ ! -d "$root/frontend/node_modules" ]; then
        say "Installing the interface's locked dependencies (npm ci) ..."
        (cd "$root/frontend" && npm ci) || fail "npm ci failed in $root/frontend." 1
    fi
    say "Building the interface (npm run build) ..."
    (cd "$root/frontend" && npm run build) ||
        fail "the interface would not build in $root/frontend." 1
fi

# --- the backend, installed from its lock ------------------------------------
#
# Into the demo's own environment, non-editably, so that the interface just
# built is inside the package the server imports (`ROBINAUTS`, in common.sh).
# The project itself is reinstalled every time: `uv sync` alone would leave a
# copy of whatever the source said when the environment was made, and a demo
# that showed last week's code would be worse than no demo.

if [ ! -x "$ROBINAUTS" ]; then
    say "Installing the platform and its locked dependencies into $VENV ..."
fi
(cd "$root/backend" && UV_PROJECT_ENVIRONMENT="$VENV" uv sync --locked \
    --no-dev --python "$PYTHON" --no-editable --reinstall-package robinauts --quiet) ||
    fail "the platform could not be installed into $VENV." 1
[ -x "$ROBINAUTS" ] || fail "$ROBINAUTS is not there even after uv sync." 1

# `db init` is idempotent: it applies this build's schema to an empty database,
# does nothing to one that already has it, and refuses everything else -- a
# demo database made from an older schema.sql included, which it says so
# about: `demo/stop.sh --reset` deletes it, and the next start makes it again.
say "Creating the schema if it is not there (robinauts db init) ..."
"$ROBINAUTS" db init ||
    fail "robinauts db init failed; nothing was started, and it said why above." 1

# --- the server --------------------------------------------------------------
#
# The console script and not `uv run`, so that the pid in the pid file is the
# server's own and demo/stop.sh signals the process that holds the socket.

say "Starting the server on http://$HOST:$PORT/ ..."
: >"$LOG_FILE"
# The secrets, at last, and **only here**: they are exported inside a subshell
# that then becomes the server (`exec`), so they are in the server's
# environment and in nothing else's, and `$!` is the server's own pid. The
# branches are written out rather than built from `$key_variable`, because the
# shell way of setting a variable whose name is itself in a variable is `env`
# or `eval`, and `env KEY=...` would put the key in an argument list that
# anybody on this machine can read out of `ps`.
(
    case "$provider_id" in
    openrouter)
        OPENROUTER_API_KEY=$key_value
        export OPENROUTER_API_KEY
        ;;
    openai)
        OPENAI_API_KEY=$key_value
        export OPENAI_API_KEY
        ;;
    *)
        ANTHROPIC_API_KEY=$key_value
        export ANTHROPIC_API_KEY
        ;;
    esac
    if [ -n "$github_token" ]; then
        ROBINAUTS_GITHUB_TOKEN=$github_token
        export ROBINAUTS_GITHUB_TOKEN
    fi
    exec "$ROBINAUTS" start --port "$PORT"
) >>"$LOG_FILE" 2>&1 &
server=$!
printf '%s\n' "$server" >"$PID_FILE"

# Waiting is done by the interpreter that is already installed, so the demo asks
# for no second tool. It gives up at once on a server that has already stopped,
# so a refusal to start is one line and its log rather than two minutes of
# silence.
if ! "$VENV/bin/python" - \
    "http://$HOST:$PORT/health" "$HEALTH_SECONDS" "$server" "$HEALTH_TRIES_PER_SECOND" \
    <<'PY'
import json
import os
import sys
import time
import urllib.error
import urllib.request

url, seconds, pid, per_second = sys.argv[1], float(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
deadline = time.monotonic() + seconds


def answered() -> bool:
    """Whether the server is answering /health with the one thing it says."""
    try:
        with urllib.request.urlopen(url, timeout=2.0) as answer:
            return answer.status == 200 and json.load(answer).get("status") == "ok"
    except (urllib.error.URLError, OSError, ValueError):
        return False


while not answered():
    try:
        os.kill(pid, 0)
    except OSError:
        raise SystemExit(f"the server stopped before it answered {url}") from None
    if time.monotonic() > deadline:
        raise SystemExit(f"{url} did not answer within {seconds:g} seconds")
    time.sleep(1.0 / per_second)
PY
then
    say "The last lines of $LOG_FILE:"
    tail -n 25 "$LOG_FILE" >&2 || true
    kill "$server" 2>/dev/null || true
    rm -f "$PID_FILE"
    fail "the server did not come up; the log above says why." 1
fi

url="http://$HOST:$PORT/"
say ""
say "The demo is up: $url"
say "  sign-in is off (the local development mode), loopback only, one user"
say "  two agents in the picker, one per engine: start a chat with each"
say "  three models beside it: $model, $model_2, $model_3"
if [ -n "$github_token" ]; then
    say "  tools: both agents may call GitHub's MCP server, as the token's owner"
fi
say "  log:  $LOG_FILE"
say "  stop: demo/stop.sh   ('demo/stop.sh --reset' also deletes $state)"

# `xdg-open` on a machine with no display opens nothing and says so at length,
# or worse, hangs waiting for one: over ssh and in a container the URL printed
# above is the whole of what is wanted.
if [ "${ROBINAUTS_DEMO_OPEN:-1}" != 0 ] &&
    [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] &&
    command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url" >/dev/null 2>&1 &
fi

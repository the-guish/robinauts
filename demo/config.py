#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The demo's configuration file, written from its template and then read back.

``demo/start.sh`` knows what the deployment is -- which provider, which key
variable, which three models -- and ``demo/robinauts.toml.in`` is the shape of
it. This puts the one into the other.

**Why this is not a few lines of ``sed``.** The model names are the values
here that a person types (``ROBINAUTS_DEMO_MODEL`` and its two siblings, which
become the titles too when they are set), and ``sed`` would read a ``&``, a
``|`` or a backslash in one as part of its own replacement language: a
value that broke out of the string it was being written into would be a
configuration that says something nobody asked for -- another provider, another
endpoint -- and the platform would start on it without complaint, since it
would be perfectly valid TOML. So the substitution is literal
(``str.replace``), what cannot be written into a TOML string is refused before
anything is written, and the finished file is **parsed back** and every
substituted value compared with what was asked for. A file that does not say
what this was told to say is not left on disk.

It uses the standard library alone (``tomllib``), so it runs under any
interpreter the demo has and needs no environment of its own.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

MODELS = (
    ("@MODEL_ID@", "@MODEL_NAME@", "@MODEL_TITLE@"),
    ("@MODEL_2_ID@", "@MODEL_2_NAME@", "@MODEL_2_TITLE@"),
    ("@MODEL_3_ID@", "@MODEL_3_NAME@", "@MODEL_3_TITLE@"),
)
"""The template's models, in order, each as the placeholders for its id, the
vendor's name for it and its title. The first is the agents' default, which is
why they name it by ``@MODEL_ID@`` too. Every one is looked for when reading
back, and a file that declares any other model has drifted from this."""

PLACEHOLDERS = (
    "@PROVIDER_ID@",
    "@PROVIDER_KIND@",
    "@KEY_VARIABLE@",
    *(placeholder for model in MODELS for placeholder in model),
)
"""Every name the template holds besides ``@BASE_URL@``, which is a whole line."""

UNFILLED = re.compile(r"@[A-Z][A-Z0-9_]*@")
"""What any placeholder looks like, filled here or not.

Looked for in the **template**, before anything is substituted, so that one
that grew a placeholder nobody fills is caught here, where it is one message
that blames the template, rather than reaching the platform as a literal ``@``
in a model's name or title -- which the parser would accept, and the picker
would show. A value may not look like one either (``check``): it would be
blamed on the template, or be replaced by the value of another.
"""

BASE_URL = "@BASE_URL@"
"""The template's line for it. A provider kind that has no ``base_url`` is not
a provider with an empty one: the line goes altogether, because ``base_url =
""`` is a start-up refusal and ``base_url`` at all is one for a vendor's own
kind, ``anthropic`` or ``openai``, whose endpoint the engines pin
(``docs/specs/agents.md``)."""

TOOLS = "@TOOLS@"
GITHUB_SERVER = "@GITHUB_SERVER@"
"""The template's lines for the GitHub tool server: each agent's ``tools`` line
and the server's own table. Whole lines, like ``@BASE_URL@``: they are written
when ``demo/start.sh`` found a GitHub token, and go altogether when it did not,
so that an agent never names a server the platform would have no secret for."""

GITHUB_URL = "https://api.githubcopilot.com/mcp/"
"""GitHub's remote MCP server, which takes the token as a bearer secret."""

WHOLE_LINES = (BASE_URL, TOOLS, GITHUB_SERVER)

FORBIDDEN = '"\\'
"""What a value may not hold, because a TOML basic string would not survive it.

Refused rather than escaped: everything written here is an id, a variable name,
a URL, a vendor's name for a model or the title the picker shows for it, and
not one of them has any business holding a quote or a backslash. Refusing says
which value was wrong; escaping would quietly accept a model name that cannot
be one.
"""

MODEL_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
"""What a model's id may be spelt with: the platform's rule for an id in the
configuration (``robinauts.domain.agents``, ``_CONFIG_ID``), which also keeps
it a bare key in the ``[models.<id>]`` it is written into."""

MAX_NAME_CHARS = 200
MAX_TITLE_CHARS = 120
"""The platform's bounds on a vendor's name for a model and on its title
(``MAX_MODEL_NAME_CHARS`` and ``MAX_MODEL_TITLE_CHARS`` in
``robinauts.domain.agents``). Written out rather than imported, since this runs
on the standard library alone, before the platform is installed; checked here so
that a name too long is refused before the demo starts anything, as the name
the operator set rather than as a title they never wrote."""

ELLIPSIS = "…"

ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*")
"""What the name of the variable GitHub's token is read from may be."""


def check(value: str, what: str, longest: int | None = None) -> str:
    """``value`` if it can be written into a TOML string as it stands."""
    if not value.strip():
        raise SystemExit(f"{what} is empty or blank")
    if any(character in FORBIDDEN for character in value):
        raise SystemExit(f"{what} may not hold a quote or a backslash: {value!r}")
    if any(character in value for character in "\n\r\t") or not value.isprintable():
        raise SystemExit(f"{what} is one line of printable text: {value!r}")
    if UNFILLED.search(value):
        raise SystemExit(f"{what} may not hold what looks like a template placeholder: {value!r}")
    if longest is not None and len(value) > longest:
        raise SystemExit(f"{what} is at most {longest} characters, not {len(value)}: {value!r}")
    return value


def title_for(name: str) -> str:
    """The title of a model the operator named: the name, cut to fit."""
    if len(name) <= MAX_TITLE_CHARS:
        return name
    return name[: MAX_TITLE_CHARS - len(ELLIPSIS)] + ELLIPSIS


def filled(template: str, values: dict[str, str], base_url: str, github_env: str) -> str:
    """The template with every placeholder replaced, literally.

    ``WHOLE_LINES`` are the exceptions: ``@BASE_URL@`` becomes the assignment
    when there is an endpoint to write, the two GitHub lines become the tools
    line and the server's table when there is a token's variable to name, and
    each line is dropped when there is not.
    """
    unknown = sorted(set(UNFILLED.findall(template)) - {*PLACEHOLDERS, *WHOLE_LINES})
    if unknown:
        raise SystemExit(f"the template holds {', '.join(unknown)}, which nothing fills")
    lines = []
    for line in template.splitlines(keepends=True):
        if line.strip() == BASE_URL:
            if not base_url:
                continue
            line = line.replace(BASE_URL, f'base_url = "{base_url}"')
        elif line.strip() == TOOLS:
            if not github_env:
                continue
            line = line.replace(TOOLS, 'tools = ["github"]')
        elif line.strip() == GITHUB_SERVER:
            if not github_env:
                continue
            line = line.replace(
                GITHUB_SERVER,
                f'[tool_servers.github]\nurl = "{GITHUB_URL}"\nsecret_env = "{github_env}"',
            )
        lines.append(line)
    text = "".join(lines)
    for placeholder in PLACEHOLDERS:
        text = text.replace(placeholder, values[placeholder])
    # Only the template can have left one, since no value may look like one:
    # a whole-line placeholder anywhere but on a line of its own.
    left = sorted(set(UNFILLED.findall(text)))
    if left:
        raise SystemExit(f"{', '.join(left)} left in the finished configuration")
    return text


def written(text: str, values: dict[str, str], base_url: str, github_env: str) -> None:
    """Refuse the file unless, read back, it says what it was told to say."""
    tables = tomllib.loads(text)
    provider_id = values["@PROVIDER_ID@"]
    provider = tables["model_providers"][provider_id]
    models = tables["models"]
    ids = [values[model_id] for model_id, _, _ in MODELS]
    if sorted(models) != sorted(ids):
        raise SystemExit(f"the configuration declares the models {sorted(models)}, not {ids}")
    said = {
        "kind": (provider["kind"], values["@PROVIDER_KIND@"]),
        "api_key_env": (provider["api_key_env"], values["@KEY_VARIABLE@"]),
        "base_url": (provider.get("base_url", ""), base_url),
    }
    for model_id, name, title in MODELS:
        model = models[values[model_id]]
        said[f"{values[model_id]}'s name"] = (model["name"], values[name])
        said[f"{values[model_id]}'s title"] = (model.get("title", ""), values[title])
        said[f"{values[model_id]}'s provider"] = (model["provider"], provider_id)
    agents = tables.get("agents")
    if not agents:
        raise SystemExit("the configuration declares no agents")
    tools = ["github"] if github_env else []
    for agent_id, agent in agents.items():
        said[f"{agent_id}'s model"] = (agent.get("model", ""), ids[0])
        said[f"{agent_id}'s tools"] = (agent.get("tools", []), tools)
    servers = tables.get("tool_servers", {})
    if github_env:
        github = servers.get("github", {})
        said["the GitHub server's url"] = (github.get("url", ""), GITHUB_URL)
        said["the GitHub server's secret_env"] = (github.get("secret_env", ""), github_env)
    said["the tool servers"] = (sorted(servers), ["github"] if github_env else [])
    wrong = [
        f"{what}: {found!r}, not {wanted!r}"
        for what, (found, wanted) in said.items()
        if found != wanted
    ]
    if wrong:
        raise SystemExit(
            "the configuration does not say what it was told to say: " + "; ".join(wrong)
        )


def main(argv: list[str] | None = None) -> int:
    """Write the configuration. Exits 2 for anything it was asked wrongly."""
    parser = argparse.ArgumentParser(
        prog="demo/config.py", description="the demo's configuration, from its template"
    )
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--provider-id", required=True)
    parser.add_argument("--kind", required=True)
    parser.add_argument("--key-variable", required=True)
    # Once per model, in the order of MODELS: the first is the agents' default.
    # An empty title is a model the operator named, called by that name.
    parser.add_argument(
        "--model",
        action="append",
        nargs=3,
        metavar=("ID", "NAME", "TITLE"),
        required=True,
        help=f"each model's id, the vendor's name for it and its title, {len(MODELS)} times",
    )
    parser.add_argument(
        "--base-url", default="", help="the endpoint, for a kind that names a protocol"
    )
    parser.add_argument(
        "--github-secret-env",
        default="",
        help="the variable GitHub's token is read from; the GitHub tools are off without it",
    )
    arguments = parser.parse_args(argv)

    if len(arguments.model) != len(MODELS):
        raise SystemExit(
            f"{len(arguments.model)} models given, and the template declares {len(MODELS)}"
        )
    values = {
        "@PROVIDER_ID@": check(arguments.provider_id, "the provider's id"),
        "@PROVIDER_KIND@": check(arguments.kind, "the provider's kind"),
        "@KEY_VARIABLE@": check(arguments.key_variable, "the key's variable"),
    }
    for (model_id, name, title), (given_id, given_name, given_title) in zip(
        MODELS, arguments.model, strict=True
    ):
        if MODEL_ID.fullmatch(given_id) is None:
            raise SystemExit(
                f"a model's id is lower-case letters, digits, '-' and '_', not {given_id!r}"
            )
        values[model_id] = given_id
        values[name] = check(given_name, f"the name of model {given_id}", MAX_NAME_CHARS)
        values[title] = check(
            given_title or title_for(given_name),
            f"the title of model {given_id}",
            MAX_TITLE_CHARS,
        )
    ids = [values[model_id] for model_id, _, _ in MODELS]
    if len(set(ids)) != len(ids):
        raise SystemExit(f"two models have one id: {ids}")
    base_url = check(arguments.base_url, "the base_url") if arguments.base_url else ""
    github_env = arguments.github_secret_env
    if github_env and ENV_NAME.fullmatch(github_env) is None:
        raise SystemExit(f"the GitHub token's variable is not a variable name: {github_env!r}")

    text = filled(arguments.template.read_text(encoding="utf-8"), values, base_url, github_env)
    written(text, values, base_url, github_env)
    arguments.out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SystemExit, OSError, tomllib.TOMLDecodeError) as refused:
        # Exit 2 with one line, as demo/start.sh's own refusals do; a code of
        # its own, and never a traceback, for something an operator set.
        if isinstance(refused, SystemExit) and refused.code in (0, None):
            raise
        print(refused, file=sys.stderr)
        raise SystemExit(2) from None

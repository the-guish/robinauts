# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The model configuration: what is refused, and that it is all refused at once.

Written like ``test_signin_config.py``, because it is the other half of the
same file and makes the same two promises: an unknown key is a mistake rather
than something to ignore, and a deployment with several mistakes is told about
every one of them in one go.

The two sets that are passed in -- the engines this deployment wired and the
provider kinds it has a client for -- are the ones ``core`` cannot know, so
they have their own tests: an agent on an engine nobody built, and a provider
of a kind this build cannot reach, are start-up refusals naming what to do.
"""

from __future__ import annotations

from typing import Any

import pytest

from robinauts.core import parse_models_config
from robinauts.domain import (
    DEFAULT_MODEL_TIMEOUT_SECONDS,
    DEFAULT_TOOL_TIMEOUT_SECONDS,
    KINDS_WITH_BASE_URL,
    LOOPBACK_HOSTS,
    MAX_AGENT_TITLE_CHARS,
    MAX_ENV_NAME_CHARS,
    MAX_MODEL_NAME_CHARS,
    MAX_MODEL_TIMEOUT_SECONDS,
    MAX_MODEL_TITLE_CHARS,
    MAX_OUTPUT_TOKENS,
    MAX_SYSTEM_PROMPT_CHARS,
    MAX_TOOL_PREFIX_CHARS,
    MAX_TOOL_TIMEOUT_SECONDS,
    AgentDefinition,
    ConfigError,
    Engine,
    InvalidValueError,
    ModelConfig,
    ModelProviderConfig,
    ModelsConfig,
    ProviderKind,
    ToolServerAuth,
    ToolServerConfig,
    UnknownModelError,
    is_endpoint_url,
)

ANTHROPIC = {"kind": "anthropic", "api_key_env": "ROBINAUTS_ANTHROPIC_KEY"}
SONNET = {"provider": "anthropic", "name": "claude-sonnet-5"}
ASSISTANT = {"title": "Assistant", "model": "sonnet", "engine": "langgraph"}
GITHUB = {"url": "https://api.githubcopilot.com/mcp/", "secret_env": "ROBINAUTS_GITHUB_TOKEN"}

LANGGRAPH_ONLY = frozenset({Engine.LANGGRAPH})
ANTHROPIC_ONLY = frozenset({ProviderKind.ANTHROPIC})
ANTHROPIC_KINDS = frozenset({ProviderKind.ANTHROPIC, ProviderKind.ANTHROPIC_COMPATIBLE})
"""The two kinds one Anthropic client reaches, which is what this build has."""


def data(**changes: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "model_providers": {"anthropic": dict(ANTHROPIC)},
        "models": {"sonnet": dict(SONNET)},
        "agents": {"assistant": dict(ASSISTANT)},
    }
    raw.update(changes)
    return {key: value for key, value in raw.items() if value is not ...}


def problems(**changes: Any) -> list[str]:
    with pytest.raises(ConfigError) as raised:
        parse_models_config(data(**changes))
    return list(raised.value.problems)


def one_problem(**changes: Any) -> str:
    found = problems(**changes)
    assert len(found) == 1, found
    return found[0]


def provider(**changes: Any) -> list[str]:
    return problems(model_providers={"anthropic": {**ANTHROPIC, **changes}})


def model(**changes: Any) -> list[str]:
    return problems(models={"sonnet": {**SONNET, **changes}})


def agent(**changes: Any) -> list[str]:
    return problems(agents={"assistant": {**ASSISTANT, **changes}})


def server(**changes: Any) -> list[str]:
    return problems(mcp_servers={"github": {**GITHUB, **changes}})


def only(found: list[str]) -> str:
    assert len(found) == 1, found
    return found[0]


# --- what a good configuration becomes --------------------------------------


def test_the_three_tables_become_the_records_they_describe() -> None:
    config = parse_models_config(data())

    assert config.providers == {
        "anthropic": ModelProviderConfig(
            id="anthropic",
            kind=ProviderKind.ANTHROPIC,
            api_key_env="ROBINAUTS_ANTHROPIC_KEY",
        )
    }
    assert config.models == {
        "sonnet": ModelConfig(id="sonnet", provider="anthropic", name="claude-sonnet-5")
    }
    assert config.agents == {
        "assistant": AgentDefinition(
            id="assistant",
            title="Assistant",
            system_prompt="",
            model="sonnet",
            engine=Engine.LANGGRAPH,
        )
    }


def test_what_a_model_does_not_say_is_left_to_the_defaults() -> None:
    config = parse_models_config(data())

    sonnet = config.models["sonnet"]
    assert sonnet.timeout_seconds == DEFAULT_MODEL_TIMEOUT_SECONDS
    assert sonnet.max_output_tokens is None


def test_a_model_may_say_its_own_timeout_and_ceiling() -> None:
    config = parse_models_config(
        data(models={"sonnet": {**SONNET, "timeout_seconds": 30, "max_output_tokens": 4096}})
    )

    assert config.models["sonnet"].timeout_seconds == 30.0
    assert config.models["sonnet"].max_output_tokens == 4096


def test_an_agent_carries_its_system_prompt_and_can_go_without_one() -> None:
    config = parse_models_config(data(agents={"assistant": {**ASSISTANT, "system_prompt": "Hi."}}))

    assert config.agents["assistant"].system_prompt == "Hi."
    assert parse_models_config(data()).agents["assistant"].system_prompt == ""


def test_an_agent_with_no_title_is_named_after_its_id() -> None:
    table = {key: value for key, value in ASSISTANT.items() if key != "title"}

    assert parse_models_config(data(agents={"assistant": table})).agents["assistant"].title == (
        "assistant"
    )


def test_the_lookups_are_whole_by_construction() -> None:
    config = parse_models_config(data())

    definition = config.agents["assistant"]
    assert config.model_by_id(definition.model).name == "claude-sonnet-5"
    assert config.provider_for(config.model_by_id(definition.model)).kind is ProviderKind.ANTHROPIC


def test_an_openai_compatible_provider_carries_its_base_url() -> None:
    table = {
        "kind": "openai-compatible",
        "api_key_env": "ROBINAUTS_OPENROUTER_KEY",
        "base_url": "https://openrouter.example/api/v1",
    }

    config = parse_models_config(
        data(model_providers={"openrouter": table}, models=..., agents=...)
    )

    assert config.providers["openrouter"].base_url == "https://openrouter.example/api/v1"


# --- a file with no models in it --------------------------------------------


def test_a_file_with_no_model_tables_is_a_deployment_with_no_agents() -> None:
    # Allowed, and not a mistake: the picker has nothing in it and /api/agents
    # is empty, which is what the local development mode has.
    config = parse_models_config({})

    assert (config.providers, config.models, config.agents) == ({}, {}, {})


def test_the_sign_in_tables_of_the_same_file_are_not_unknown_keys() -> None:
    # One file, two parsers, each handed the whole of it.
    parse_models_config(
        data(
            public_url="https://robinauts.example.com",
            session_hours=12,
            providers={"google": {"title": "Google"}},
            allow=[{"provider": "google", "hosted_domain": "example.com"}],
        )
    )


def test_admin_is_left_to_the_sign_in_parser_to_refuse() -> None:
    # It refuses it by name; reporting it here as well would turn one mistake
    # into two problems.
    parse_models_config(data(admin=[{"provider": "google"}]))


# --- the top level ----------------------------------------------------------


def test_an_unknown_top_level_key_is_a_mistake() -> None:
    assert one_problem(modles={}) == "unknown key 'modles'"


@pytest.mark.parametrize(
    ("table", "dependent"),
    [
        ("model_providers", ("models", "agents")),
        ("models", ("agents",)),
        ("agents", ()),
    ],
)
def test_each_table_is_a_table_of_entries_by_id(table: str, dependent: tuple[str, ...]) -> None:
    changes: dict[str, Any] = {name: ... for name in dependent}
    changes[table] = ["not", "a", "table"]

    assert one_problem(**changes) == f"{table}: a table of entries by id"


# --- a provider -------------------------------------------------------------


def test_a_provider_id_is_spelt_the_way_every_configured_id_is() -> None:
    assert "lower-case letters" in one_problem(
        model_providers={"Anthropic!": dict(ANTHROPIC)},
        models=...,
        agents=...,
    )


def test_a_provider_is_a_table() -> None:
    assert (
        one_problem(model_providers={"anthropic": "anthropic"}, models=..., agents=...)
        == "model_providers.anthropic: a table"
    )


def test_a_provider_refuses_an_unknown_key() -> None:
    assert (
        only(provider(organisation="us")) == "model_providers.anthropic: unknown key 'organisation'"
    )


@pytest.mark.parametrize("kind", [..., "", 1])
def test_a_provider_says_what_kind_it_is(kind: Any) -> None:
    table = dict(ANTHROPIC)
    if kind is ...:
        del table["kind"]
    else:
        table["kind"] = kind

    assert (
        only(problems(model_providers={"anthropic": table}))
        == "model_providers.anthropic.kind: missing, or not a non-empty string"
    )


def test_a_kind_is_one_of_the_kinds_there_are() -> None:
    assert only(provider(kind="antropic")) == (
        "model_providers.anthropic.kind: one of anthropic, anthropic-compatible, openai,"
        " openai-compatible,"
        " not 'antropic'"
    )


def test_a_kind_this_build_cannot_reach_is_refused_at_start_up() -> None:
    # A client whose dependency tree fails the licence policy is a provider
    # this build does not offer (DEPENDENCIES.md), and an operator is told so
    # when they start the server rather than by a person waiting for an answer.
    # The model and the agent are left pointing at it, and say nothing: one
    # mistake, one problem.
    with pytest.raises(ConfigError) as raised:
        parse_models_config(
            data(
                model_providers={"openai": {"kind": "openai", "api_key_env": "K"}},
                models={"sonnet": {"provider": "openai", "name": "a-model"}},
            ),
            kinds=ANTHROPIC_ONLY,
        )

    assert list(raised.value.problems) == [
        "model_providers.openai.kind: this build cannot reach 'openai' providers;"
        " it was built with anthropic"
    ]


def test_a_provider_names_the_variable_its_key_is_read_from() -> None:
    table = {key: value for key, value in ANTHROPIC.items() if key != "api_key_env"}
    assert (
        only(problems(model_providers={"anthropic": table}))
        == "model_providers.anthropic.api_key_env: missing, or not a non-empty string"
    )


@pytest.mark.parametrize("given", ["sk-ant-0123456789", "ROBINAUTS KEY", "9KEY"])
def test_a_key_pasted_where_its_variable_s_name_belongs_is_refused(given: str) -> None:
    # The mistake that matters: it would put the key in the file.
    assert only(provider(api_key_env=given)).startswith(
        "model_providers.anthropic.api_key_env: the NAME of an environment variable"
    )


def test_a_variable_name_is_bounded() -> None:
    assert only(provider(api_key_env="A" * (MAX_ENV_NAME_CHARS + 1))) == (
        f"model_providers.anthropic.api_key_env: at most {MAX_ENV_NAME_CHARS} characters"
    )


def test_only_a_kind_that_names_a_protocol_has_a_base_url() -> None:
    # A vendor's endpoint is the engine's own constant, and a second answer to
    # "where is it" would be a way to send the operator's key somewhere else.
    assert only(provider(base_url="https://example.invalid/v1")) == (
        "model_providers.anthropic.base_url: only these kinds have one:"
        " anthropic-compatible, openai-compatible; anthropic has one endpoint of its own"
    )


@pytest.mark.parametrize("kind", sorted(kind.value for kind in KINDS_WITH_BASE_URL))
def test_a_kind_that_names_a_protocol_needs_a_base_url(kind: str) -> None:
    assert only(
        problems(
            model_providers={"local": {"kind": kind, "api_key_env": "K"}},
            models=...,
            agents=...,
        )
    ) == (
        f"model_providers.local.base_url: a provider of kind {kind} needs one; there is"
        f" no endpoint to guess"
    )


def test_an_anthropic_compatible_provider_carries_the_endpoint_it_is_reached_at() -> None:
    # How OpenRouter is reached in this build: the vendor's Messages API at an
    # address the operator gives (docs/specs/agents.md). The base URL is a
    # prefix the client appends `/v1/messages` to, so it stops at `/api`.
    table = {
        "kind": "anthropic-compatible",
        "api_key_env": "ROBINAUTS_OPENROUTER_KEY",
        "base_url": "https://openrouter.ai/api",
    }

    config = parse_models_config(
        data(model_providers={"openrouter": table}, models=..., agents=...)
    )

    assert config.providers["openrouter"] == ModelProviderConfig(
        id="openrouter",
        kind=ProviderKind.ANTHROPIC_COMPATIBLE,
        api_key_env="ROBINAUTS_OPENROUTER_KEY",
        base_url="https://openrouter.ai/api",
    )


def test_a_refusal_names_every_kind_that_could_be_used_instead() -> None:
    # What an operator who wrote `openai` is told to use instead. The set is
    # the deployment's own answer; what this build's really is, and that the
    # sentence reads as the guide prints it, is asserted where `app` may be
    # imported (tests/integration/test_config_file.py).
    with pytest.raises(ConfigError) as raised:
        parse_models_config(
            data(
                model_providers={"openai": {"kind": "openai", "api_key_env": "K"}},
                models={"sonnet": {"provider": "openai", "name": "a-model"}},
            ),
            kinds=ANTHROPIC_KINDS,
        )

    assert list(raised.value.problems) == [
        "model_providers.openai.kind: this build cannot reach 'openai' providers;"
        " it was built with anthropic, anthropic-compatible"
    ]


@pytest.mark.parametrize(
    "base_url",
    [
        "http://openrouter.example/api/v1",  # a key in the clear, to another machine
        "ftp://openrouter.example/v1",
        "openrouter.example/v1",
        "https://openrouter.example/v1?key=leaked",
        "https://openrouter.example/v1#fragment",
        "https://openrouter .example/v1",
    ],
)
def test_a_base_url_is_an_endpoint_a_key_may_be_sent_to(base_url: str) -> None:
    table = {"kind": "openai-compatible", "api_key_env": "K", "base_url": base_url}

    assert only(problems(model_providers={"local": table}, models=..., agents=...)).startswith(
        "model_providers.local.base_url: an https:// endpoint"
    )


def test_a_plain_endpoint_on_this_machine_is_allowed() -> None:
    table = {
        "kind": "openai-compatible",
        "api_key_env": "K",
        "base_url": "http://127.0.0.1:8000/v1",
    }

    config = parse_models_config(data(model_providers={"local": table}, models=..., agents=...))

    assert config.providers["local"].base_url == "http://127.0.0.1:8000/v1"


# --- a model ----------------------------------------------------------------


def test_a_model_id_is_spelt_the_way_every_configured_id_is() -> None:
    assert "lower-case letters" in one_problem(models={"Sonnet!": dict(SONNET)}, agents=...)


def test_a_model_is_a_table() -> None:
    assert one_problem(models={"sonnet": 5}, agents=...) == "models.sonnet: a table"


def test_a_model_refuses_an_unknown_key() -> None:
    assert only(model(temperature=0.5)) == "models.sonnet: unknown key 'temperature'"


def test_a_model_names_a_provider_that_is_configured() -> None:
    assert only(model(provider="openai")) == (
        "models.sonnet.provider: 'openai' is not one of [model_providers]"
    )


def test_a_provider_with_a_mistake_in_it_does_not_bury_it_under_a_cascade() -> None:
    # A reference is checked against what the operator **declared**, not
    # against what was built: one mistake in the provider is one problem, not
    # that plus every model naming it and every agent naming those.
    found = problems(model_providers={"anthropic": {**ANTHROPIC, "kind": "antropic"}})

    assert found == [
        "model_providers.anthropic.kind: one of anthropic, anthropic-compatible, openai,"
        " openai-compatible,"
        " not 'antropic'"
    ]


def test_a_model_says_what_the_vendor_calls_it() -> None:
    table = {key: value for key, value in SONNET.items() if key != "name"}

    assert only(problems(models={"sonnet": table}, agents=...)) == (
        "models.sonnet.name: missing, or not a non-empty string"
    )


def test_a_model_may_have_a_title_to_be_picked_by() -> None:
    config = parse_models_config(data(models={"sonnet": {**SONNET, "title": "Claude Sonnet 5"}}))

    assert config.models["sonnet"].title == "Claude Sonnet 5"


def test_a_model_with_no_title_is_named_after_its_id() -> None:
    assert parse_models_config(data()).models["sonnet"].title == "sonnet"


@pytest.mark.parametrize("title", ["", 5, None])
def test_a_model_s_title_is_text_when_it_is_given(title: Any) -> None:
    assert only(model(title=title)) == "models.sonnet.title: missing, or not a non-empty string"


def test_a_model_s_title_is_bounded() -> None:
    assert only(model(title="C" * (MAX_MODEL_TITLE_CHARS + 1))) == (
        f"models.sonnet.title: at most {MAX_MODEL_TITLE_CHARS} characters"
    )


def test_a_model_title_the_record_refuses_is_reported_as_a_problem_like_any_other() -> None:
    assert only(model(title="Claude\nSonnet")).startswith("models.sonnet: ")
    assert only(model(title="   ")).startswith("models.sonnet: ")


def test_a_vendor_s_name_for_a_model_is_bounded() -> None:
    assert only(model(name="c" * (MAX_MODEL_NAME_CHARS + 1))) == (
        f"models.sonnet.name: at most {MAX_MODEL_NAME_CHARS} characters"
    )


@pytest.mark.parametrize(
    "seconds", [0, -1, MAX_MODEL_TIMEOUT_SECONDS + 1, "30", True, float("inf")]
)
def test_a_model_call_s_timeout_is_a_number_of_seconds_inside_its_bounds(seconds: Any) -> None:
    assert only(model(timeout_seconds=seconds)).startswith(
        "models.sonnet.timeout_seconds: a number of seconds over 0 and at most"
    )


@pytest.mark.parametrize("tokens", [0, -1, MAX_OUTPUT_TOKENS + 1, 1.5, "4096", True])
def test_a_ceiling_on_an_answer_is_a_whole_number_inside_its_bounds(tokens: Any) -> None:
    assert only(model(max_output_tokens=tokens)).startswith(
        "models.sonnet.max_output_tokens: a whole number of tokens over 0 and at most"
    )


def test_a_model_is_found_by_the_id_a_conversation_carries() -> None:
    config = parse_models_config(data())

    assert config.model_by_id("sonnet") is config.models["sonnet"]


def test_a_model_the_deployment_does_not_offer_is_named_as_such() -> None:
    # What a conversation whose model the operator removed meets at its next
    # turn: refused by name, never answered by some other model.
    with pytest.raises(UnknownModelError) as raised:
        parse_models_config(data()).model_by_id("gpt-5-5")

    assert str(raised.value) == "no model 'gpt-5-5' is configured in this deployment"


def test_a_model_id_that_is_not_spelt_like_one_is_refused_before_it_is_looked_up() -> None:
    with pytest.raises(InvalidValueError):
        ModelsConfig().model_by_id("Claude Sonnet!")


# --- an agent ---------------------------------------------------------------


def test_an_agent_id_is_spelt_the_way_every_configured_id_is() -> None:
    assert "lower-case letters" in one_problem(agents={"Assistant!": dict(ASSISTANT)})


def test_an_agent_is_a_table() -> None:
    assert one_problem(agents={"assistant": ["Assistant"]}) == "agents.assistant: a table"


def test_an_agent_refuses_an_unknown_key() -> None:
    assert only(agent(skills=[])) == "agents.assistant: unknown key 'skills'"


def test_an_agent_s_title_is_bounded() -> None:
    assert only(agent(title="A" * (MAX_AGENT_TITLE_CHARS + 1))) == (
        f"agents.assistant.title: at most {MAX_AGENT_TITLE_CHARS} characters"
    )


def test_a_title_the_record_refuses_is_reported_as_a_problem_like_any_other() -> None:
    # The record is the rule, not the parser: what it refuses and this did not
    # belongs in the same list rather than escaping start-up as another error.
    assert only(agent(title="Assistant\nand friends")).startswith("agents.assistant: ")


def test_an_agent_names_a_model_that_is_configured() -> None:
    assert only(agent(model="opus")) == "agents.assistant.model: 'opus' is not one of [models]"


def test_an_agent_says_which_model_it_runs_on() -> None:
    table = {key: value for key, value in ASSISTANT.items() if key != "model"}

    assert only(problems(agents={"assistant": table})) == (
        "agents.assistant.model: missing, or not a non-empty string"
    )


def test_an_engine_is_one_of_the_engines_there_are() -> None:
    assert only(agent(engine="langchain")) == (
        "agents.assistant.engine: one of langgraph, pydantic-ai, not 'langchain'"
    )


def test_an_agent_on_an_engine_nobody_wired_is_refused_at_start_up() -> None:
    with pytest.raises(ConfigError) as raised:
        parse_models_config(
            data(agents={"assistant": {**ASSISTANT, "engine": "pydantic-ai"}}),
            engines=LANGGRAPH_ONLY,
        )

    assert list(raised.value.problems) == [
        "agents.assistant.engine: the pydantic-ai engine is not wired in this deployment;"
        " set engine to one of langgraph"
    ]


def test_a_system_prompt_is_text() -> None:
    assert only(agent(system_prompt=["Play", "fair."])) == (
        "agents.assistant.system_prompt: text, or no system_prompt at all"
    )


def test_a_system_prompt_is_bounded() -> None:
    assert only(agent(system_prompt="x" * (MAX_SYSTEM_PROMPT_CHARS + 1))) == (
        f"agents.assistant.system_prompt: at most {MAX_SYSTEM_PROMPT_CHARS} characters"
    )


# --- all at once ------------------------------------------------------------


def test_every_problem_in_the_file_is_reported_together() -> None:
    found = problems(
        modles={},
        model_providers={
            "anthropic": {**ANTHROPIC, "api_key_env": "sk-ant-secret", "region": "eu"}
        },
        models={"sonnet": {**SONNET, "timeout_seconds": 0}},
        agents={"assistant": {**ASSISTANT, "engine": "langchain", "skills": []}},
    )

    assert found == [
        "unknown key 'modles'",
        "model_providers.anthropic: unknown key 'region'",
        "model_providers.anthropic.api_key_env: the NAME of an environment variable"
        " holding the key, not the key itself",
        "models.sonnet.timeout_seconds: a number of seconds over 0 and at most 3600," " not 0",
        "agents.assistant: unknown key 'skills'",
        "agents.assistant.engine: one of langgraph, pydantic-ai, not 'langchain'",
    ]
    # The key itself is in none of it: only the variable's name is ever named,
    # and here there was no name to give.
    assert not [problem for problem in found if "sk-ant-secret" in problem]


# --- the endpoint rule itself -----------------------------------------------
#
# `domain.is_endpoint_url` is what the parser and the record both ask, and it
# is the rule that decides where an operator's key is sent. It is worth
# checking on its own, in the words it is written in, rather than only through
# the message a configuration refusal happens to print.


@pytest.mark.parametrize(
    "url",
    [
        "https://gateway.example/v1",
        "https://gateway.example:8443/v1",
        # This machine, in every spelling `urlsplit` reports a host in.
        "http://127.0.0.1:8000/v1",
        "http://localhost/v1",
        "http://LOCALHOST/v1",
        "http://[::1]:8080/v1",
        "http://[::1]/v1",
    ],
)
def test_an_endpoint_a_key_may_be_sent_to(url: str) -> None:
    assert is_endpoint_url(url)


@pytest.mark.parametrize(
    "url",
    [
        # A credential in the file, and from there in every log line and copy
        # of it that names the endpoint.
        "https://user:sk-live@gateway.example/v1",
        "https://user@gateway.example/v1",
        # And the trick that rule closes: the loopback host is the userinfo,
        # and the machine reached is somebody else's.
        "http://localhost@evil.example/v1",
        # Plain text to another machine is a key given away.
        "http://gateway.example/v1",
        # Nothing to reach, nothing to parse, nothing a client could use.
        "https://",
        "http://",
        "https://gateway.example:notaport/v1",
        "gateway.example/v1",
        "ftp://gateway.example/v1",
        "https://gateway.example/v1?key=leaked",
        "https://gateway.example/v1#fragment",
        "https://gateway example/v1",
        "https://gateway.exämple/v1",
        "",
    ],
)
def test_an_endpoint_a_key_may_not_be_sent_to(url: str) -> None:
    assert not is_endpoint_url(url)


@pytest.mark.parametrize("value", [None, 42, b"https://gateway.example/v1", ["https://x/v1"]])
def test_an_endpoint_is_text(value: object) -> None:
    assert not is_endpoint_url(value)


def test_the_loopback_hosts_are_spelt_the_way_a_split_url_reports_them() -> None:
    # No brackets and lower case: `urlsplit(...).hostname` unbrackets an IPv6
    # address and folds the case, so a bracketed entry here would be an entry
    # nothing ever matches.
    assert all(host == host.lower() for host in LOOPBACK_HOSTS)
    assert not [host for host in LOOPBACK_HOSTS if "[" in host or "]" in host]
    assert LOOPBACK_HOSTS == {"localhost", "127.0.0.1", "::1"}


# --- the record's own rules about base_url -----------------------------------
#
# The parser says where in the file the mistake is; the **record** is the rule
# (``core.models_config._built``), and it is what a caller building one by hand
# -- a test, the demo, a later step -- meets. Both are checked: a record that
# let a base_url through for a kind that names a vendor would be a way around
# the parser's refusal and a way to send the operator's key elsewhere.


@pytest.mark.parametrize("kind", sorted(KINDS_WITH_BASE_URL))
def test_a_record_of_a_kind_that_names_a_protocol_needs_its_base_url(kind: ProviderKind) -> None:
    with pytest.raises(InvalidValueError) as raised:
        ModelProviderConfig(id="gateway", kind=kind, api_key_env="K")

    assert str(raised.value) == (
        f"a provider of kind {kind.value} needs its base_url: there is no endpoint" f" to guess"
    )


@pytest.mark.parametrize("kind", sorted(frozenset(ProviderKind) - KINDS_WITH_BASE_URL))
def test_a_record_of_a_kind_that_names_a_vendor_refuses_a_base_url(kind: ProviderKind) -> None:
    # `anthropic` among them, and that is the one that matters: the engines
    # reach it and an `anthropic-compatible` endpoint with the same client, so
    # only this refusal keeps the vendor's own endpoint the engine's constant.
    with pytest.raises(InvalidValueError) as raised:
        ModelProviderConfig(
            id="vendor", kind=kind, api_key_env="K", base_url="https://evil.example/v1"
        )

    assert str(raised.value) == (
        f"only these kinds have a base_url: anthropic-compatible, openai-compatible;"
        f" {kind.value} has one endpoint of its own"
    )


def test_a_record_holds_a_configured_endpoint_to_the_same_rule_as_the_parser() -> None:
    # Plain text to another machine is the key given away, wherever the record
    # was built (`is_endpoint_url`).
    with pytest.raises(InvalidValueError) as raised:
        ModelProviderConfig(
            id="openrouter",
            kind=ProviderKind.ANTHROPIC_COMPATIBLE,
            api_key_env="K",
            base_url="http://openrouter.ai/api",
        )

    assert str(raised.value).startswith("base_url is an https:// endpoint")


# --- tool servers, and the agents that use them --------------------------------


def test_a_tool_server_becomes_its_record_and_an_agent_names_it() -> None:
    config = parse_models_config(
        data(
            mcp_servers={"github": dict(GITHUB)},
            agents={"assistant": {**ASSISTANT, "tools": ["github"]}},
        )
    )

    assert config.tool_servers["github"] == ToolServerConfig(
        id="github",
        url="https://api.githubcopilot.com/mcp/",
        secret_env="ROBINAUTS_GITHUB_TOKEN",
        auth=ToolServerAuth.BEARER,
        prefix="github",
        timeout_seconds=DEFAULT_TOOL_TIMEOUT_SECONDS,
    )
    assert config.agents["assistant"].tools == ("github",)


def test_what_a_tool_server_does_not_say_is_left_to_the_defaults() -> None:
    config = parse_models_config(data(mcp_servers={"github": dict(GITHUB)}))

    github = config.tool_servers["github"]
    assert (github.auth, github.user, github.prefix) == (ToolServerAuth.BEARER, "", "github")
    assert github.timeout_seconds == DEFAULT_TOOL_TIMEOUT_SECONDS
    assert config.agents["assistant"].tools == ()


def test_a_tool_server_may_use_basic_auth_a_prefix_and_a_timeout_of_its_own() -> None:
    jira = {
        "url": "https://your-site.atlassian.net/mcp",
        "auth": "basic",
        "user": "robinauts@example.com",
        "secret_env": "JIRA",
        "prefix": "atlassian",
        "timeout_seconds": 30,
    }

    config = parse_models_config(data(mcp_servers={"jira": jira}))

    server_ = config.tool_servers["jira"]
    assert (server_.auth, server_.user, server_.prefix, server_.timeout_seconds) == (
        ToolServerAuth.BASIC,
        "robinauts@example.com",
        "atlassian",
        30.0,
    )


def test_a_file_with_no_servers_is_a_deployment_whose_agents_have_no_tools() -> None:
    config = parse_models_config(data())

    assert config.tool_servers == {}
    assert config.agents["assistant"].tools == ()


def test_a_tool_server_id_is_spelt_the_way_every_configured_id_is() -> None:
    found = only(problems(mcp_servers={"GitHub": dict(GITHUB)}))

    assert found.startswith("mcp_servers.GitHub: an id")


def test_a_tool_server_is_a_table() -> None:
    assert only(problems(mcp_servers={"github": "https://x"})) == "mcp_servers.github: a table"


def test_a_tool_server_refuses_an_unknown_key() -> None:
    assert only(server(token="sk-live")) == "mcp_servers.github: unknown key 'token'"


def test_a_tool_server_names_its_url_and_the_variable_its_secret_is_read_from() -> None:
    assert sorted(problems(mcp_servers={"github": {}})) == [
        "mcp_servers.github.secret_env: missing, or not a non-empty string",
        "mcp_servers.github.url: missing, or not a non-empty string",
    ]


@pytest.mark.parametrize(
    "url",
    [
        "http://github.example/mcp",
        "https://user:sk-live@github.example/mcp",
        "https://github.example/mcp?token=x",
        "https://github.example/mcp#f",
        "ftp://github.example/mcp",
    ],
)
def test_a_tool_server_s_url_is_an_endpoint_a_secret_may_be_sent_to(url: str) -> None:
    found = only(server(url=url))

    assert found.startswith("mcp_servers.github.url: an https:// endpoint")
    assert "sk-live" not in found


def test_a_secret_pasted_where_its_variable_s_name_belongs_is_refused_when_it_can_be() -> None:
    # A token with a '-' in it is not a variable name and is refused here; a
    # GitHub token is letters, digits and underscores, which *is* one, so the
    # refusal cannot see it -- the start-up check then declines to repeat it
    # (``adapters.config_file.named``, tested with the secrets).
    assert only(server(secret_env="sk-ant-pasted-by-mistake")) == (
        "mcp_servers.github.secret_env: the NAME of an environment variable holding the"
        " secret, not the secret itself"
    )
    token = "ghp_" + "A1b2" * 9
    accepted = parse_models_config(data(mcp_servers={"github": {**GITHUB, "secret_env": token}}))
    assert accepted.tool_servers["github"].secret_env == token


def test_a_tool_server_s_auth_is_bearer_basic_or_none() -> None:
    assert only(server(auth="digest")) == (
        "mcp_servers.github.auth: one of basic, bearer, none, not 'digest'"
    )


def test_a_server_with_no_auth_names_no_variable_and_no_user() -> None:
    """A public server -- Microsoft Learn's is one -- is sent no credential,
    so there is no variable to name; naming one is a mistake to say."""
    learn = {"url": "https://learn.microsoft.com/api/mcp", "auth": "none"}

    config = parse_models_config(data(mcp_servers={"learn": learn}))

    assert config.tool_servers["learn"] == ToolServerConfig(
        id="learn", url="https://learn.microsoft.com/api/mcp", auth=ToolServerAuth.NONE
    )
    assert config.tool_servers["learn"].secret_env == ""
    assert only(server(auth="none")) == (
        'mcp_servers.github.secret_env: auth = "none" sends no credential, so there is no'
        " variable to name; leave secret_env out"
    )
    assert only(problems(mcp_servers={"learn": {**learn, "user": "me"}})) == (
        "mcp_servers.learn.user: only basic auth has a user part; none sends no credential"
    )


def test_basic_auth_names_the_user_part_and_bearer_has_none() -> None:
    assert only(server(auth="basic")) == (
        "mcp_servers.github.user: basic auth names the user part; there is nothing to guess"
    )
    assert only(server(user="me")) == (
        "mcp_servers.github.user: only basic auth has a user part; bearer sends the secret" " alone"
    )
    assert only(server(auth="basic", user="")) == (
        "mcp_servers.github.user: missing, or not a non-empty string"
    )
    assert only(server(auth="basic", user="a:b")) == (
        "mcp_servers.github.user: the user part of a basic credential holds no ':'"
    )
    # Under a misspelt auth, the user part is not a second mistake.
    assert only(server(auth="digest", user="me")).startswith("mcp_servers.github.auth:")


@pytest.mark.parametrize("prefix", ["git__hub", "github_", "GitHub Tools"])
def test_a_prefix_is_a_name_a_tool_can_be_told_apart_under(prefix: str) -> None:
    assert only(server(prefix=prefix)) == (
        f"mcp_servers.github.prefix: letters, digits, _ and -, at most {MAX_TOOL_PREFIX_CHARS} of"
        f" them, holding no '__' and not ending in '_'"
    )


def test_a_prefix_is_bounded_and_written_or_left_out() -> None:
    assert only(server(prefix="g" * (MAX_TOOL_PREFIX_CHARS + 1))) == (
        f"mcp_servers.github.prefix: at most {MAX_TOOL_PREFIX_CHARS} characters"
    )
    assert only(server(prefix="")) == (
        "mcp_servers.github.prefix: missing, or not a non-empty string"
    )


def test_an_id_that_would_not_do_as_a_prefix_needs_one_written_down() -> None:
    long = "g" * (MAX_TOOL_PREFIX_CHARS + 1)
    assert only(problems(mcp_servers={long: dict(GITHUB)})) == (
        f"mcp_servers.{long}: this id is not one its tools can be named under (at most"
        f" {MAX_TOOL_PREFIX_CHARS} characters, no '__', not ending in '_'); write a prefix"
    )
    assert only(problems(mcp_servers={"git__hub": dict(GITHUB)})).startswith(
        "mcp_servers.git__hub: this id is not one"
    )
    # With one written, the id is free to be what it is.
    config = parse_models_config(data(mcp_servers={long: {**GITHUB, "prefix": "github"}}))
    assert config.tool_servers[long].prefix == "github"


def test_two_servers_under_one_prefix_are_refused_by_both_names() -> None:
    jira = {**GITHUB, "secret_env": "JIRA", "prefix": "github"}

    assert only(problems(mcp_servers={"github": dict(GITHUB), "jira": jira})) == (
        "mcp_servers.jira: its tools would be named under 'github', as mcp_servers.github's"
        " are; give one of them a prefix of its own"
    )
    # In the same pass as the other mistakes in the clashing table.
    found = problems(mcp_servers={"github": dict(GITHUB), "jira": {**jira, "url": "not a url"}})
    assert [line.split(":")[0] for line in found] == ["mcp_servers.jira.url", "mcp_servers.jira"]


@pytest.mark.parametrize("seconds", [0, -1, "30", True, float("inf"), MAX_TOOL_TIMEOUT_SECONDS + 1])
def test_a_tool_call_s_timeout_is_a_number_of_seconds_inside_its_bounds(seconds: Any) -> None:
    assert only(server(timeout_seconds=seconds)).startswith(
        "mcp_servers.github.timeout_seconds: a number of seconds over 0 and at most"
    )


def test_an_agent_names_tool_servers_that_are_configured() -> None:
    assert only(agent(tools=["github"])) == (
        "agents.assistant.tools: 'github' is not one of [mcp_servers]"
    )


def test_an_agent_s_tools_are_a_list_of_ids_each_once() -> None:
    assert only(agent(tools="github")) == (
        "agents.assistant.tools: a list of tool server ids, or no tools at all"
    )
    assert only(agent(tools=[1])) == (
        "agents.assistant.tools: a list of tool server ids, or no tools at all"
    )
    assert (
        only(
            problems(
                mcp_servers={"github": dict(GITHUB)},
                agents={"assistant": {**ASSISTANT, "tools": ["github", "github"]}},
            )
        )
        == "agents.assistant.tools: each tool server once"
    )
    # An unknown id named twice is one unknown id, and one repeat.
    assert agent(tools=["x", "x"]) == [
        "agents.assistant.tools: 'x' is not one of [mcp_servers]",
        "agents.assistant.tools: each tool server once",
    ]


def test_a_server_with_a_mistake_does_not_bury_it_under_the_agents_that_name_it() -> None:
    # The declared table is what an agent's `tools` is checked against, as a
    # model's provider is: one mistake, one line.
    found = problems(
        mcp_servers={"github": {**GITHUB, "url": "not a url"}},
        agents={"assistant": {**ASSISTANT, "tools": ["github"]}},
    )

    assert len(found) == 1 and found[0].startswith("mcp_servers.github.url:")


# --- the tool server record's own rules ----------------------------------------


def tool_server(**changes: Any) -> ToolServerConfig:
    fields: dict[str, Any] = {"id": "github", "url": "https://x/mcp", "secret_env": "G"}
    fields.update(changes)
    return ToolServerConfig(**fields)


def test_a_tool_server_record_fills_its_prefix_in_from_its_id() -> None:
    assert tool_server().prefix == "github"


def test_a_tool_server_record_refuses_an_id_it_cannot_name_tools_under_unless_told() -> None:
    with pytest.raises(InvalidValueError, match="needs a prefix written down"):
        tool_server(id="git__hub")

    assert tool_server(id="git__hub", prefix="gh").prefix == "gh"


def test_a_tool_server_record_holds_basic_auth_to_its_user_part() -> None:
    with pytest.raises(InvalidValueError, match="names the user part"):
        tool_server(auth=ToolServerAuth.BASIC)
    with pytest.raises(InvalidValueError, match="holds no ':'"):
        tool_server(auth=ToolServerAuth.BASIC, user="a:b")
    with pytest.raises(InvalidValueError, match="only basic auth has a user part"):
        tool_server(user="me")


def test_a_tool_server_record_with_no_auth_names_no_secret_env_and_every_other_names_one() -> None:
    public = tool_server(auth=ToolServerAuth.NONE, secret_env="")
    assert (public.auth, public.secret_env) == (ToolServerAuth.NONE, "")
    with pytest.raises(InvalidValueError, match="names no secret_env"):
        tool_server(auth=ToolServerAuth.NONE)
    with pytest.raises(InvalidValueError, match="sends no credential"):
        tool_server(auth=ToolServerAuth.NONE, secret_env="", user="me")
    for auth in (ToolServerAuth.BEARER, ToolServerAuth.BASIC):
        with pytest.raises(InvalidValueError, match="secret_env"):
            tool_server(auth=auth, secret_env="", user="me" if auth is ToolServerAuth.BASIC else "")


def test_a_tool_server_record_holds_its_endpoint_to_the_same_rule_as_the_parser() -> None:
    with pytest.raises(InvalidValueError) as raised:
        tool_server(url="http://github.example/mcp")

    assert str(raised.value).startswith("a tool server's url is an https:// endpoint")


def test_the_whole_configuration_holds_agents_to_configured_servers_and_prefixes_apart() -> None:
    config = parse_models_config(data(mcp_servers={"github": dict(GITHUB)}))
    github = config.tool_servers["github"]
    uses_jira = AgentDefinition(
        id="a",
        title="A",
        system_prompt="",
        model="sonnet",
        engine=Engine.LANGGRAPH,
        tools=("jira",),
    )

    with pytest.raises(InvalidValueError, match="uses tool server 'jira', which is not configured"):
        ModelsConfig(
            providers=config.providers,
            models=config.models,
            agents={"a": uses_jira},
            tool_servers={"github": github},
        )
    with pytest.raises(InvalidValueError, match="would both name their tools under 'github'"):
        ModelsConfig(
            providers=config.providers,
            models=config.models,
            tool_servers={"github": github, "jira": tool_server(id="jira", prefix="github")},
        )


def test_an_agent_record_names_each_tool_server_once_by_id() -> None:
    with pytest.raises(InvalidValueError, match="each tool server once"):
        AgentDefinition(
            id="a",
            title="A",
            system_prompt="",
            model="sonnet",
            engine=Engine.LANGGRAPH,
            tools=("g", "g"),
        )
    with pytest.raises(InvalidValueError, match="a tuple of ids"):
        AgentDefinition(
            id="a",
            title="A",
            system_prompt="",
            model="sonnet",
            engine=Engine.LANGGRAPH,
            tools=["g"],  # type: ignore[arg-type]
        )


# --- the record's own rules about a model's title --------------------------


def test_a_model_record_built_without_a_title_is_named_after_its_id() -> None:
    assert ModelConfig(id="sonnet", provider="anthropic", name="claude-sonnet-5").title == "sonnet"


@pytest.mark.parametrize("title", ["   ", "two\nlines", 5, "C" * (MAX_MODEL_TITLE_CHARS + 1)])
def test_a_model_record_refuses_a_title_nobody_could_pick_it_by(title: Any) -> None:
    with pytest.raises(InvalidValueError):
        ModelConfig(id="sonnet", provider="anthropic", name="claude-sonnet-5", title=title)

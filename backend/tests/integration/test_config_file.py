# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Reading the configuration: the file, the environment, and the start-up checks.

Marked ``io``: it writes small files into a temporary directory and reads them
back, which is the whole point -- a reader tested against a string would not
be tested against a file that is a directory, or is not there.

Both halves of the file are here: the sign-in tables with their client
secrets, and the model tables with the providers' API keys. Neither secret is
ever in the file, and neither check ever prints one.

The last test of each half reads the very example in ``docs/specs/sign-in.md``
and ``docs/specs/agents.md`` through both halves, ``adapters.read_toml`` and
the matching parser, because a documented example that does not parse is a bug
report waiting to be filed. The end of the module does the same to the worked
file in ``docs/deployment.md`` -- which is a whole configuration rather than
half of one, so it goes through **both** parsers at once, with the engines and
the provider kinds a real deployment passes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from robinauts.adapters import (
    ProviderKeys,
    check_api_keys,
    check_client_secrets,
    check_tool_secrets,
    environment,
    read_toml,
)
from robinauts.app import BUILDABLE_KINDS, SIGN_IN_TABLES, WIRED_ENGINES
from robinauts.core import parse_models_config, parse_sign_in_config
from robinauts.domain import (
    ConfigError,
    Engine,
    Matcher,
    ModelProviderConfig,
    ModelsConfig,
    ProviderConfig,
    ProviderKind,
    SignInConfig,
    ToolServerAuth,
    ToolServerConfig,
)

pytestmark = pytest.mark.io

SPEC = Path(__file__).resolve().parents[3] / "docs" / "specs" / "sign-in.md"
AGENTS_SPEC = Path(__file__).resolve().parents[3] / "docs" / "specs" / "agents.md"
GUIDE = Path(__file__).resolve().parents[3] / "docs" / "deployment.md"

GOOD = """
public_url = "https://robinauts.example.com"
session_hours = 8

[providers.google]
title = "Google"
issuer = "https://accounts.google.com"
client_id = "1234.apps.googleusercontent.com"
client_secret_env = "ROBINAUTS_GOOGLE_SECRET"

[[allow]]
provider = "google"
hosted_domain = "example.com"
"""


def written(directory: Path, text: str, *, name: str = "sign-in.toml") -> Path:
    """``text`` in a file of that name, under ``directory``."""
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def test_a_good_file_comes_back_as_the_tables_it_holds(tmp_path: Path) -> None:
    data = read_toml(written(tmp_path, GOOD))

    assert data["public_url"] == "https://robinauts.example.com"
    assert data["session_hours"] == 8
    assert data["providers"]["google"]["client_secret_env"] == "ROBINAUTS_GOOGLE_SECRET"
    assert data["allow"] == [{"provider": "google", "hosted_domain": "example.com"}]


def test_the_reader_judges_nothing(tmp_path: Path) -> None:
    # Adapters read, core validates. A file full of keys nobody knows is a
    # mapping full of keys nobody knows, and `core` is what refuses it.
    data = read_toml(written(tmp_path, "nonsense = true\n[what]\never = 1\n"))

    assert data == {"nonsense": True, "what": {"ever": 1}}


def test_an_empty_file_is_an_empty_mapping(tmp_path: Path) -> None:
    assert read_toml(written(tmp_path, "")) == {}


def test_a_missing_file_names_itself(tmp_path: Path) -> None:
    path = tmp_path / "not-there.toml"

    with pytest.raises(ConfigError) as raised:
        read_toml(path)

    assert raised.value.problems and str(path) in raised.value.problems[0]


def test_a_directory_is_not_a_configuration_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as raised:
        read_toml(tmp_path)

    assert str(tmp_path) in raised.value.problems[0]


def test_a_syntax_error_names_the_file_and_the_line(tmp_path: Path) -> None:
    path = written(
        tmp_path,
        'public_url = "https://robinauts.example.com"\n'
        "session_hours = 8\n"
        "this line is not TOML\n",
    )

    with pytest.raises(ConfigError) as raised:
        read_toml(path)

    (problem,) = raised.value.problems
    assert str(path) in problem
    # tomllib puts the position in its own message, which is why the message
    # is repeated whole rather than summarised.
    assert re.search(r"line 3", problem), problem


def test_a_file_that_is_not_utf_8_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "sign-in.toml"
    path.write_bytes(b'title = "\xff\xfe not utf-8"\n')

    with pytest.raises(ConfigError) as raised:
        read_toml(path)

    assert "UTF-8" in raised.value.problems[0]


def test_every_problem_is_one_config_error(tmp_path: Path) -> None:
    # A ConfigError always lists at least one problem; the reader has exactly
    # one to report, because a file either reads or it does not.
    with pytest.raises(ConfigError) as raised:
        read_toml(tmp_path / "nowhere.toml")

    assert len(raised.value.problems) == 1


# The environment, and the start-up check over it.


def test_environment_reads_a_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROBINAUTS_TEST_ONLY_SECRET", "s3cret")

    assert environment("ROBINAUTS_TEST_ONLY_SECRET") == "s3cret"


@pytest.mark.parametrize("value", [None, ""])
def test_an_unset_or_empty_variable_is_no_secret(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    # An exported but empty variable is not a configured secret: answering
    # "yes, and it is the empty string" would turn a start-up failure naming
    # the variable into a sign-in the provider refuses.
    if value is None:
        monkeypatch.delenv("ROBINAUTS_TEST_ONLY_SECRET", raising=False)
    else:
        monkeypatch.setenv("ROBINAUTS_TEST_ONLY_SECRET", value)

    assert environment("ROBINAUTS_TEST_ONLY_SECRET") is None


def provider(name: str) -> ProviderConfig:
    return ProviderConfig(
        id=name,
        title=name.title(),
        issuer=f"https://{name}.example.com",
        client_id=f"{name}-client",
        client_secret_env=f"ROBINAUTS_{name.upper()}_SECRET",
    )


def configured(*names: str) -> SignInConfig:
    return SignInConfig(
        public_url="https://robinauts.example.com",
        providers={name: provider(name) for name in names},
        allow=(),
    )


def test_the_start_up_check_passes_when_every_secret_is_there() -> None:
    held = {"ROBINAUTS_ONE_SECRET": "a", "ROBINAUTS_TWO_SECRET": "b"}

    check_client_secrets(configured("one", "two"), secret_for=held.get)


def test_the_start_up_check_names_every_missing_variable_at_once() -> None:
    # All problems at once (docs/specs/operations.md): an operator with three
    # unset variables fixes a deployment in one pass, not in three restarts.
    held = {"ROBINAUTS_TWO_SECRET": "b"}

    with pytest.raises(ConfigError) as raised:
        check_client_secrets(configured("one", "two", "three"), secret_for=held.get)

    assert len(raised.value.problems) == 2
    assert "ROBINAUTS_ONE_SECRET" in raised.value.problems[0]
    assert "ROBINAUTS_THREE_SECRET" in raised.value.problems[1]


def test_the_start_up_check_reports_the_name_and_never_a_value() -> None:
    held = {"ROBINAUTS_ONE_SECRET": "the-real-secret"}

    with pytest.raises(ConfigError) as raised:
        check_client_secrets(configured("one", "two"), secret_for=held.get)

    assert "the-real-secret" not in str(raised.value)


def test_an_empty_variable_fails_the_start_up_check() -> None:
    with pytest.raises(ConfigError):
        check_client_secrets(configured("one"), secret_for={"ROBINAUTS_ONE_SECRET": ""}.get)


def test_a_deployment_with_no_provider_has_nothing_to_check() -> None:
    check_client_secrets(configured(), secret_for=lambda name: None)


# The two halves together, on the documented example.


def spec_example() -> str:
    """The TOML block of ``docs/specs/sign-in.md``, as it is written there."""
    blocks = re.findall(r"```toml\n(.*?)```", SPEC.read_text(encoding="utf-8"), re.DOTALL)
    assert len(blocks) == 1, f"{SPEC} should hold one TOML example, not {len(blocks)}"
    return blocks[0]


def test_the_example_in_the_specification_reads_and_parses(tmp_path: Path) -> None:
    # Roles are deferred, so the [[admin]] table the spec still shows is
    # refused by `core` on purpose; the rest of the example must parse.
    text = spec_example()
    assert "[[admin]]" in text, "the spec's example no longer shows an admin table"
    without_admin = text.partition("[[admin]]")[0]

    config = parse_sign_in_config(read_toml(written(tmp_path, without_admin)))

    assert config.public_url == "https://robinauts.example.com"
    assert config.session_hours == 12
    assert sorted(config.providers) == ["google", "okta"]
    assert config.provider("okta").groups_claim == "groups"
    assert [entry.matcher for entry in config.allow] == [Matcher.HOSTED_DOMAIN, Matcher.GROUP]


def test_the_example_still_names_the_variables_rather_than_the_secrets(tmp_path: Path) -> None:
    config = parse_sign_in_config(
        read_toml(written(tmp_path, spec_example().partition("[[admin]]")[0]))
    )

    assert config.provider("google").client_secret_env == "ROBINAUTS_GOOGLE_SECRET"
    with pytest.raises(ConfigError) as raised:
        check_client_secrets(config, secret_for=lambda name: None)
    assert len(raised.value.problems) == 2


# The model half of the same file: the keys, and where they are read from.


def model_provider(name: str) -> ModelProviderConfig:
    return ModelProviderConfig(
        id=name, kind=ProviderKind.ANTHROPIC, api_key_env=f"ROBINAUTS_{name.upper()}_KEY"
    )


def with_models(*names: str) -> ModelsConfig:
    return ModelsConfig(providers={name: model_provider(name) for name in names})


def test_the_keys_of_every_provider_are_read_at_start_up() -> None:
    held = {"ROBINAUTS_ONE_KEY": "a", "ROBINAUTS_TWO_KEY": "b"}

    keys = check_api_keys(with_models("one", "two"), secret_for=held.get)

    assert keys.key_for("one") == "a"
    assert keys.key_for("two") == "b"


def test_every_unset_key_variable_is_named_at_once() -> None:
    held = {"ROBINAUTS_TWO_KEY": "b"}

    with pytest.raises(ConfigError) as raised:
        check_api_keys(with_models("one", "two", "three"), secret_for=held.get)

    assert len(raised.value.problems) == 2
    assert "ROBINAUTS_ONE_KEY" in raised.value.problems[0]
    assert "ROBINAUTS_THREE_KEY" in raised.value.problems[1]


def test_a_missing_key_is_reported_by_the_name_of_its_variable_and_never_a_value() -> None:
    held = {"ROBINAUTS_ONE_KEY": "sk-the-real-key"}

    with pytest.raises(ConfigError) as raised:
        check_api_keys(with_models("one", "two"), secret_for=held.get)

    assert "sk-the-real-key" not in str(raised.value)


def test_an_empty_key_variable_is_an_unset_one() -> None:
    with pytest.raises(ConfigError):
        check_api_keys(with_models("one"), secret_for={"ROBINAUTS_ONE_KEY": ""}.get)


def test_a_provider_no_model_uses_still_needs_its_key() -> None:
    # It is a provider the operator meant to have; a deployment that started
    # without its key would work until somebody picked the wrong agent.
    with pytest.raises(ConfigError):
        check_api_keys(with_models("unused"), secret_for=lambda name: None)


def test_a_deployment_with_no_model_provider_has_no_key_to_read() -> None:
    assert repr(check_api_keys(ModelsConfig(), secret_for=lambda name: None)) == "ProviderKeys()"


def test_the_keys_print_the_providers_and_never_a_key() -> None:
    keys = check_api_keys(with_models("one"), secret_for={"ROBINAUTS_ONE_KEY": "sk-secret"}.get)

    assert repr(keys) == "ProviderKeys(one)"
    assert "sk-secret" not in f"{keys!r} {keys}"


def test_asking_for_a_provider_this_process_read_no_key_for_says_so() -> None:
    keys = check_api_keys(with_models("one"), secret_for={"ROBINAUTS_ONE_KEY": "a"}.get)

    with pytest.raises(ConfigError) as raised:
        keys.key_for("two")

    assert "model_providers.two" in str(raised.value)


def test_the_keys_hold_a_copy_of_what_they_were_given() -> None:
    given = {"one": "a"}
    keys = ProviderKeys(given)

    given["one"] = "changed"

    assert keys.key_for("one") == "a"


# The model half of the documented example, read through both halves.


def models_examples() -> tuple[str, str, str]:
    """The three model TOML blocks of ``docs/specs/agents.md``, as they are written there.

    The first two are configurations this build runs -- the vendor's own
    endpoint, and an ``anthropic-compatible`` one, which is how OpenRouter is
    reached here. The third shows the shape of an OpenAI-compatible provider,
    which this build refuses. All three are read here, and each is held to the
    thing it is an example of. The fourth block of the file is the tool
    servers' (``tool_servers_example``).
    """
    blocks = spec_blocks()
    return blocks[0], blocks[1], blocks[2]


def spec_blocks() -> list[str]:
    """Every TOML block of ``docs/specs/agents.md``, in order."""
    blocks = re.findall(r"```toml\n(.*?)```", AGENTS_SPEC.read_text(encoding="utf-8"), re.DOTALL)
    assert len(blocks) == 4, f"{AGENTS_SPEC} should hold four TOML examples, not {len(blocks)}"
    return blocks


def tool_servers_example() -> str:
    """The tool servers' block: one ``bearer`` server, one ``basic``, an agent naming both.

    It names a model the first block declares, so it is read together with
    that one, as an operator would write both in one file.
    """
    deployable, _, _, servers = spec_blocks()
    return deployable + servers


def test_the_model_example_in_the_specification_reads_and_parses(tmp_path: Path) -> None:
    # With the kinds and engines a deployment really passes, so that the
    # documented example is one an operator can start a server with.
    deployable, _, _ = models_examples()

    config = parse_models_config(
        read_toml(written(tmp_path, deployable)),
        engines=WIRED_ENGINES,
        kinds=BUILDABLE_KINDS,
    )

    assert sorted(config.providers) == ["anthropic"]
    assert config.providers["anthropic"].kind is ProviderKind.ANTHROPIC
    assert config.models["sonnet"].name == "claude-sonnet-5"
    assert config.models["sonnet"].timeout_seconds == 120.0
    assert config.models["sonnet"].max_output_tokens == 8192
    assert config.agents["assistant"].engine is Engine.LANGGRAPH


def test_the_second_example_is_the_shape_this_build_refuses(tmp_path: Path) -> None:
    # It is in the spec because the configuration language is settled; it is
    # refused because the client that reaches it does not pass the licence
    # policy (DEPENDENCIES.md, "Known exclusions").
    _, _, not_buildable = models_examples()
    tables = read_toml(written(tmp_path, not_buildable))

    parsed = parse_models_config(tables)
    with pytest.raises(ConfigError) as raised:
        parse_models_config(tables, engines=WIRED_ENGINES, kinds=BUILDABLE_KINDS)

    assert parsed.providers["gateway"].kind is ProviderKind.OPENAI_COMPATIBLE
    assert parsed.providers["gateway"].base_url == "https://gateway.example.com/v1"
    assert raised.value.problems[0] == (
        "model_providers.gateway.kind: this build cannot reach 'openai-compatible'"
        " providers; it was built with anthropic, anthropic-compatible"
    )


def test_the_openrouter_example_is_one_this_build_runs(tmp_path: Path) -> None:
    # The kind the exclusion above does **not** cost: OpenRouter serves
    # Anthropic's Messages API, so it is reached with the client both engines
    # already have. `base_url` is the prefix the client appends `/v1/messages`
    # to, which is why the documented value stops at `/api`.
    _, openrouter, _ = models_examples()

    config = parse_models_config(
        read_toml(written(tmp_path, openrouter)),
        engines=WIRED_ENGINES,
        kinds=BUILDABLE_KINDS,
    )

    provider = config.providers["openrouter"]
    assert provider.kind is ProviderKind.ANTHROPIC_COMPATIBLE
    assert provider.base_url == "https://openrouter.ai/api"
    assert config.models["sonnet-via-openrouter"].name == "anthropic/claude-sonnet-5"
    assert config.agents["assistant-openrouter"].engine is Engine.PYDANTIC_AI


def test_the_model_example_names_the_variables_rather_than_the_keys(tmp_path: Path) -> None:
    # All three blocks at once, which is also how it is known that the three
    # declare three different providers rather than two spellings of one: the
    # file would not parse if two `[model_providers.x]` tables collided.
    config = parse_models_config(read_toml(written(tmp_path, "".join(models_examples()))))

    assert config.providers["anthropic"].api_key_env == "ROBINAUTS_ANTHROPIC_KEY"
    assert config.providers["openrouter"].api_key_env == "ROBINAUTS_OPENROUTER_KEY"
    assert config.providers["gateway"].api_key_env == "ROBINAUTS_GATEWAY_KEY"
    with pytest.raises(ConfigError) as raised:
        check_api_keys(config, secret_for=lambda name: None)
    assert len(raised.value.problems) == 3


def test_the_tool_servers_example_in_the_specification_reads_and_parses(tmp_path: Path) -> None:
    config = parse_models_config(
        read_toml(written(tmp_path, tool_servers_example())),
        engines=WIRED_ENGINES,
        kinds=BUILDABLE_KINDS,
    )

    github, jira = config.tool_servers["github"], config.tool_servers["jira"]
    assert (github.auth, github.prefix, github.user) == (ToolServerAuth.BEARER, "github", "")
    assert (jira.auth, jira.prefix, jira.user) == (
        ToolServerAuth.BASIC,
        "atlassian",
        "robinauts@example.com",
    )
    assert jira.timeout_seconds == 30.0
    # The public server: no credential, so no variable named.
    learn = config.tool_servers["learn"]
    assert (learn.auth, learn.secret_env, learn.prefix) == (ToolServerAuth.NONE, "", "learn")
    assert config.agents["assistant-with-tools"].tools == ("github", "jira", "learn")


def test_the_tool_servers_example_names_the_variables_rather_than_the_secrets(
    tmp_path: Path,
) -> None:
    config = parse_models_config(read_toml(written(tmp_path, tool_servers_example())))

    assert config.tool_servers["github"].secret_env == "ROBINAUTS_GITHUB_TOKEN"
    assert config.tool_servers["jira"].secret_env == "ROBINAUTS_JIRA_TOKEN"
    with pytest.raises(ConfigError) as raised:
        check_tool_secrets(config, secret_for=lambda name: None)
    assert list(raised.value.problems) == [
        "mcp_servers.github: the secret is read from the environment variable"
        " ROBINAUTS_GITHUB_TOKEN, which is unset or empty",
        "mcp_servers.jira: the secret is read from the environment variable"
        " ROBINAUTS_JIRA_TOKEN, which is unset or empty",
    ]


# --- the tool servers' secrets, read at start-up ------------------------------


def tool_server(name: str, variable: str) -> ToolServerConfig:
    return ToolServerConfig(id=name, url=f"https://{name}.example/mcp", secret_env=variable)


def with_servers(**variables: str) -> ModelsConfig:
    return ModelsConfig(
        tool_servers={name: tool_server(name, variable) for name, variable in variables.items()}
    )


def test_every_tool_server_s_secret_is_read_at_start_up() -> None:
    read = check_tool_secrets(
        with_servers(github="GH", jira="JI"), secret_for={"GH": "g", "JI": "j"}.get
    )

    assert (read.secret_for("github"), read.secret_for("jira")) == ("g", "j")


def test_every_unset_secret_variable_is_named_at_once_and_never_a_value() -> None:
    with pytest.raises(ConfigError) as raised:
        check_tool_secrets(with_servers(github="GH", jira="JI"), secret_for={"GH": ""}.get)

    assert list(raised.value.problems) == [
        "mcp_servers.github: the secret is read from the environment variable GH, which is"
        " unset or empty",
        "mcp_servers.jira: the secret is read from the environment variable JI, which is"
        " unset or empty",
    ]


def test_a_token_pasted_as_a_variable_name_is_not_repeated_by_the_refusal() -> None:
    # A GitHub token is letters, digits and underscores -- a valid variable
    # name, as far as the spelling rule can tell -- so the refusal describes
    # it rather than repeating it into the start-up log.
    token = "ghp_" + "A1b2C3d4" * 5
    config = ModelsConfig(tool_servers={"github": tool_server("github", token)})

    with pytest.raises(ConfigError) as raised:
        check_tool_secrets(config, secret_for=lambda _: None)

    (line,) = raised.value.problems
    assert token not in line
    assert line.startswith("mcp_servers.github: the secret is read from the environment variable")
    # And a name spelt as variables are is printed, as the operator needs.
    assert "ROBINAUTS_X" in check_missing("ROBINAUTS_X")
    assert "lower_case" not in check_missing("lower_case")


def check_missing(variable: str) -> str:
    with pytest.raises(ConfigError) as raised:
        check_tool_secrets(
            ModelsConfig(tool_servers={"s": tool_server("s", variable)}), secret_for=lambda _: None
        )
    return raised.value.problems[0]


def test_the_tool_secrets_print_the_servers_and_never_a_secret() -> None:
    read = check_tool_secrets(with_servers(github="GH"), secret_for=lambda _: "sk-nobody")

    assert repr(read) == "ToolServerSecrets(github)"
    assert "sk-nobody" not in repr(read) + str(read)
    with pytest.raises(ConfigError) as raised:
        read.secret_for("jira")
    assert list(raised.value.problems) == [
        "mcp_servers.jira: no secret was read for this tool server"
    ]


def test_a_deployment_with_no_tool_server_has_no_secret_to_read() -> None:
    assert repr(check_tool_secrets(ModelsConfig(), secret_for=lambda _: None)) == (
        "ToolServerSecrets()"
    )


# The deployment guide's file, read through both halves at once.


def guide_examples() -> tuple[str, str]:
    """The two TOML blocks of ``docs/deployment.md``, as they are written there.

    The first is the worked ``robinauts.toml`` an operator copies: one file,
    both halves, and it must parse through both parsers with the engines and
    the kinds a real deployment passes. The second is the **local development
    mode**'s file, which the guide says holds the model tables and nothing
    else -- a claim that is checked here rather than believed.
    """
    blocks = re.findall(r"```toml\n(.*?)```", GUIDE.read_text(encoding="utf-8"), re.DOTALL)
    assert len(blocks) == 2, f"{GUIDE} should hold two TOML examples, not {len(blocks)}"
    return blocks[0], blocks[1]


def test_the_guides_configuration_is_one_a_deployment_starts_with(tmp_path: Path) -> None:
    deployment, _ = guide_examples()
    tables = read_toml(written(tmp_path, deployment, name="robinauts.toml"))

    config = parse_sign_in_config(tables)
    models = parse_models_config(tables, engines=WIRED_ENGINES, kinds=BUILDABLE_KINDS)

    assert config.public_url == "https://robinauts.example.com"
    assert config.session_hours == 12
    assert sorted(config.providers) == ["google", "okta"]
    assert config.provider("okta").groups_claim == "groups"
    assert [entry.matcher for entry in config.allow] == [
        Matcher.HOSTED_DOMAIN,
        Matcher.GROUP,
        Matcher.EMAIL,
    ]
    assert sorted(models.agents) == ["assistant"]
    assert models.agents["assistant"].engine is Engine.LANGGRAPH
    assert models.providers["anthropic"].kind is ProviderKind.ANTHROPIC


def test_the_guide_prints_the_redirect_uris_the_platform_builds(tmp_path: Path) -> None:
    # The two URIs the guide tells an operator to paste into Google and Okta
    # are the two the platform sends. A guide that printed a third thing would
    # be a sign-in that fails at the provider, for everybody, at once.
    deployment, _ = guide_examples()
    config = parse_sign_in_config(read_toml(written(tmp_path, deployment)))
    text = GUIDE.read_text(encoding="utf-8")

    for provider_id in config.providers:
        assert config.redirect_uri(provider_id) in text


def test_the_guides_configuration_holds_no_secret(tmp_path: Path) -> None:
    deployment, _ = guide_examples()
    tables = read_toml(written(tmp_path, deployment))
    config = parse_sign_in_config(tables)
    models = parse_models_config(tables, engines=WIRED_ENGINES, kinds=BUILDABLE_KINDS)

    assert config.provider("google").client_secret_env == "ROBINAUTS_GOOGLE_SECRET"
    assert models.providers["anthropic"].api_key_env == "ROBINAUTS_ANTHROPIC_KEY"
    with pytest.raises(ConfigError) as secrets:
        check_client_secrets(config, secret_for=lambda name: None)
    with pytest.raises(ConfigError) as keys:
        check_api_keys(models, secret_for=lambda name: None)
    assert len(secrets.value.problems) == 2
    assert len(keys.value.problems) == 1


def test_the_guides_development_file_is_the_model_half_alone(tmp_path: Path) -> None:
    # What the local development mode may be given: the model tables, and none
    # of the sign-in ones, which the composition root refuses there (BOTH_MODES).
    _, development = guide_examples()
    tables = read_toml(written(tmp_path, development, name="development.toml"))

    models = parse_models_config(tables, engines=WIRED_ENGINES, kinds=BUILDABLE_KINDS)

    assert models.agents["assistant"].engine is Engine.PYDANTIC_AI
    assert not [key for key in tables if key in SIGN_IN_TABLES]
    with pytest.raises(ConfigError) as raised:
        parse_sign_in_config(tables)
    assert any("public_url" in problem for problem in raised.value.problems)

# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What one Fabric runtime is started with: the ``FabricConfig`` of NeMo Fabric's Claude adapter
for an agent, a model and the conversation so far, and Claude Code's permissions for the
conversation's workspace.

**No secret is in the configuration.** Fabric hands its configuration to an adapter host process
as JSON, so the model's key and the tool servers' credentials go in this process's environment
under names of the worker's own (``ROBINAUTS_FABRIC_SECRET_...``), which the adapter host
inherits: the model names its key by that variable, and a tool server's header is a
``${VARIABLE}`` reference the Claude adapter resolves into Claude Code's environment, never into
a file. The values come from the engine's lookups, as for every engine.

**Permissions.** Claude Code runs in ``dontAsk``, as the Claude Code worker's does: what is
allowed runs, anything else is denied with a message the model sees. The Claude adapter's own
tool list names tools exactly, and a tool server's tools are not known ahead, so the allow list
is Claude Code's own permission rules, which take a whole server (``mcp__<id>``): written in the
workspace's ``.claude/settings.local.json`` before the runtime starts, the one settings source
the runtime reads.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path

from nemo_fabric import (
    EnvironmentConfig,
    FabricConfig,
    HarnessConfig,
    InstructionConfig,
    InstructionsConfig,
    McpConfig,
    McpServerConfig,
    MetadataConfig,
    ModelConfig,
    ProvenanceConfig,
    RuntimeConfig,
    ToolsConfig,
)

from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    ModelProviderConfig,
    ProviderKind,
    ToolServerAuth,
)
from robinauts.agent_engines.contract.domain import ModelConfig as RobinautsModel
from robinauts.agent_engines.contract.ports import EngineSettings
from robinauts_fabric_worker.history import Turn, seed
from robinauts_fabric_worker.settings import Settings, SystemPromptMode

ADAPTER_ID = "nvidia.fabric.claude"
"""NeMo Fabric's Claude adapter, which drives Claude Code through the Claude Agent SDK."""
SETTING_SOURCES = ["local"]
"""The one settings file the runtime's Claude Code reads: the workspace's
``.claude/settings.local.json``, which holds the permissions written here."""
LOCAL_SETTINGS = Path(".claude") / "settings.local.json"
SECRET_PREFIX = "ROBINAUTS_FABRIC_SECRET_"
QUIET = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
"""What Claude Code is handed beside its credentials: no telemetry, no error reports, no update
checks (docs/specs/core.md, "Nothing phones home"). The Claude adapter passes Claude Code no
variable of the worker's but those it names, so this one is set here, not in the image."""


def secret_variable(kind: str, owner: str) -> str:
    """The variable a secret of ``owner`` goes in: the owner's id, and a digest of it so that
    two ids that spell the same in upper case stay apart."""
    digest = hashlib.sha256(f"{kind}\0{owner}".encode()).hexdigest()[:8].upper()
    spelled = re.sub(r"[^A-Z0-9]", "_", owner.upper())
    return f"{SECRET_PREFIX}{kind}_{spelled}_{digest}"


def export(environ: MutableMapping[str, str], variable: str, value: str) -> str:
    """Put a secret in the environment the adapter host inherits; its variable's name."""
    environ[variable] = value
    return variable


@dataclass(frozen=True, slots=True)
class Runtime:
    """What a runtime is started with, and what makes two turns' runtimes the same one."""

    config: FabricConfig
    signature: str
    """A digest of everything but the conversation so far: a turn whose signature differs from
    the live runtime's, another model say, starts a runtime of its own."""


def _model(
    model: RobinautsModel,
    provider: ModelProviderConfig,
    settings: EngineSettings,
    environ: MutableMapping[str, str],
) -> ModelConfig:
    variable = export(
        environ, secret_variable("MODEL", provider.id), settings.keys.key_for(provider.id)
    )
    compatible = provider.kind is ProviderKind.ANTHROPIC_COMPATIBLE or bool(provider.base_url)
    return ModelConfig(
        provider="anthropic-compatible" if compatible else "anthropic",
        model=model.name,
        api_key_env=variable,
        base_url=provider.base_url,
    )


def _mcp(
    agent: AgentDefinition, settings: EngineSettings, environ: MutableMapping[str, str]
) -> McpConfig | None:
    servers: dict[str, McpServerConfig] = {}
    for server_id in agent.tools:
        table = settings.models.tool_servers[server_id]
        header_name = ""
        header_value = ""
        if table.auth is not ToolServerAuth.NONE:
            secret = settings.tool_secrets.secret_for(server_id)
            if table.auth is ToolServerAuth.BEARER:
                header_name, header_value = "Authorization", f"Bearer {secret}"
            elif table.auth is ToolServerAuth.BASIC:
                pair = base64.b64encode(f"{table.user}:{secret}".encode()).decode()
                header_name, header_value = "Authorization", f"Basic {pair}"
            else:
                header_name, header_value = table.header, secret
        headers = {}
        if header_name:
            variable = export(environ, secret_variable("TOOL", server_id), header_value)
            headers[header_name] = f"${{{variable}}}"
        servers[server_id] = McpServerConfig(
            transport="streamable-http", url=table.url, custom_headers=headers
        )
    return McpConfig(servers=servers) if servers else None


def excluded(agent: AgentDefinition, settings: EngineSettings) -> list[str]:
    """The tools of the agent's servers that a table's ``exclude`` leaves out, as Claude Code
    names them."""
    return [
        f"mcp__{server_id}__{name}"
        for server_id in agent.tools
        for name in settings.models.tool_servers[server_id].exclude
    ]


def permissions(agent: AgentDefinition, settings: EngineSettings, worker: Settings) -> dict:
    """Claude Code's permission rules for the agent: the worker's allowed tools and the agent's
    tool servers, whole, run without asking; the worker's disallowed tools and the excluded ones
    never run."""
    allow = [*worker.allowed_tools, *(f"mcp__{server_id}" for server_id in agent.tools)]
    deny = [*worker.disallowed_tools, *excluded(agent, settings)]
    return {"permissions": {"allow": allow, "deny": deny}}


def write_permissions(workspace: Path, rules: dict) -> None:
    """Write the rules where the runtime's Claude Code reads them, afresh at every start."""
    target = workspace / LOCAL_SETTINGS
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(rules, indent=2) + "\n", encoding="utf-8")


def runtime(
    *,
    agent: AgentDefinition,
    model_id: str,
    settings: EngineSettings,
    worker: Settings,
    workspace: Path,
    artifacts: Path,
    chain: list[Turn],
    environ: MutableMapping[str, str] = os.environ,
) -> Runtime:
    """The configuration of a runtime for ``agent`` on ``model_id`` in ``workspace``, seeded with
    the conversation so far, ``chain``."""
    model = settings.models.models[model_id]
    provider = settings.models.providers[model.provider]
    harness: dict[str, object] = {
        "permission_mode": worker.permission_mode,
        "setting_sources": SETTING_SOURCES,
    }
    if worker.effort is not None:
        harness["reasoning_effort"] = worker.effort
    if worker.max_budget_usd is not None:
        harness["max_budget_usd"] = worker.max_budget_usd
    blocked = [*worker.disallowed_tools, *excluded(agent, settings)]
    rules = permissions(agent, settings, worker)
    mode = "append" if worker.system_prompt is SystemPromptMode.PRESET else "replace"
    signature_parts = {
        "agent": agent.system_prompt,
        "tools": list(agent.tools),
        "model": model_id,
        "harness": harness,
        "rules": rules,
        "mode": mode,
        "max_turns": worker.max_turns,
    }
    signature = hashlib.sha256(json.dumps(signature_parts, sort_keys=True).encode()).hexdigest()
    instructions = "\n\n".join(part for part in (agent.system_prompt, seed(chain)) if part)
    config = FabricConfig(
        metadata=MetadataConfig(name="robinauts"),
        harness=HarnessConfig(adapter_id=ADAPTER_ID, settings=harness),
        runtime=RuntimeConfig(max_turns=worker.max_turns, artifacts=str(artifacts)),
        environment=EnvironmentConfig(workspace=str(workspace), env=dict(QUIET)),
        models={"default": _model(model, provider, settings, environ)},
        instructions=(
            InstructionsConfig(system=InstructionConfig(content=instructions, mode=mode))
            if instructions
            else None
        ),
        mcp=_mcp(agent, settings, environ),
        tools=ToolsConfig(blocked=blocked) if blocked else None,
        provenance=ProvenanceConfig(enabled=False),
    )
    return Runtime(config, signature)

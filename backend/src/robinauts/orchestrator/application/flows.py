# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The flows a person may start, as forms, and the start of one: the inputs are checked against
the flow's form and then handed to the engine as they came."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from robinauts.orchestrator.contract.domain import (
    FlowForm,
    Flows,
    InvalidInputsError,
    NoSuchFlowError,
    OrchestrationConfig,
)
from robinauts.orchestrator.core.forms import DeclaredInputs, build_forms, check_inputs
from robinauts.orchestrator.ports.engine import FlowEngine
from robinauts.shared_domain.domain import Config


class FlowForms(Flows):
    """The flows of one flows folder. ``declared`` is what its flow files declare, read at the
    start; ``config`` the agents' configuration, which the lists of models and agents come
    from. Both are fixed for the server's life, so the forms are built once."""

    def __init__(
        self,
        engine: FlowEngine,
        orchestration: OrchestrationConfig,
        declared: DeclaredInputs,
        config: Config,
    ) -> None:
        self._engine = engine
        self._forms = build_forms(
            declared,
            orchestration.forms,
            repositories=orchestration.repositories,
            config=config,
        )
        self._by_name = {form.name: form for form in self._forms}

    async def forms(self) -> tuple[FlowForm, ...]:
        return self._forms

    async def start(self, flow: str, inputs: Mapping[str, Any]) -> str:
        form = self._by_name.get(flow)
        if form is None:
            raise NoSuchFlowError(f"no flow {flow!r}")
        if problems := check_inputs(form, inputs):
            raise InvalidInputsError(f"{flow}: " + "; ".join(problems))
        return await self._engine.start_run(flow, inputs)

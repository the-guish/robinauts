# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Flows as forms: a form file read, checked against the inputs its flow declares; the lists of
values its inputs take theirs from; and inputs checked against the form before a run starts.
Pure: the declared inputs and the agents' configuration are handed in."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from robinauts.orchestrator.contract.domain import (
    FORM_SUFFIX,
    FlowForm,
    FormInput,
    FormSpec,
    InputSpec,
    LovKind,
    Option,
    Repository,
)
from robinauts.shared_domain.domain import Config

FORM_KEYS = frozenset({"title", "inputs"})
INPUT_KEYS = frozenset({"label", "lov"})
LOV_TYPE = "string"
"""The declared type of an input with a list of values: each value is a string."""

DeclaredInputs = Mapping[str, Mapping[str, str]]
"""By flow name, the flow's inputs and the type it declares for each, in its order."""


def parse_form(flow: str, table: Mapping[str, Any]) -> tuple[FormSpec | None, list[str]]:
    """The form of ``flow`` as its file holds it, or ``None``, with every problem."""
    where = f"{flow}{FORM_SUFFIX}"
    problems = [f"{where} {key}: unknown key" for key in sorted(table.keys() - FORM_KEYS)]
    title = table.get("title", flow)
    if not isinstance(title, str) or not title:
        problems.append(f"{where} title: a non-empty string")
        title = flow
    inputs = table.get("inputs", {})
    if not isinstance(inputs, Mapping):
        return None, [*problems, f"{where} [inputs]: a table of inputs by name"]
    specs: dict[str, InputSpec] = {}
    for name, entry in inputs.items():
        here = f"{where} [inputs.{name}]"
        if not isinstance(entry, Mapping):
            problems.append(f"{here}: a table with label and lov")
            continue
        problems += [f"{here} {key}: unknown key" for key in sorted(entry.keys() - INPUT_KEYS)]
        label = entry.get("label", name)
        if not isinstance(label, str) or not label:
            problems.append(f"{here} label: a non-empty string")
            continue
        lov = entry.get("lov")
        if lov is not None and lov not in set(LovKind):
            known = ", ".join(LovKind)
            problems.append(f"{here} lov: {lov!r} is not one of {known}")
            continue
        specs[name] = InputSpec(label=label, lov=None if lov is None else LovKind(lov))
    if problems:
        return None, problems
    return FormSpec(title=title, inputs=specs), []


def check_forms(forms: Mapping[str, FormSpec], declared: DeclaredInputs) -> list[str]:
    """What does not fit between the forms and the flows: a form of no flow, an input its flow
    does not declare, a list of values on an input that is not a string."""
    problems = []
    for flow, form in sorted(forms.items()):
        where = f"{flow}{FORM_SUFFIX}"
        inputs = declared.get(flow)
        if inputs is None:
            problems.append(f"{where}: the flows folder holds no flow {flow!r}")
            continue
        for name, spec in form.inputs.items():
            if name not in inputs:
                problems.append(f"{where} [inputs.{name}]: {flow} declares no input {name!r}")
            elif spec.lov is not None and inputs[name] != LOV_TYPE:
                problems.append(
                    f"{where} [inputs.{name}] lov: only a {LOV_TYPE} input takes a list of"
                    f" values, and {name} is a {inputs[name]}"
                )
    return problems


def options_of(
    kind: LovKind, *, repositories: tuple[Repository, ...], config: Config
) -> tuple[Option, ...]:
    """The values a list of values offers, in the order their configuration gives them."""
    if kind is LovKind.REPO_BRANCH:
        return tuple(
            Option(value=f"{r.id}@{branch}", label=f"{r.id} · {branch}")
            for r in repositories
            for branch in r.branches
        )
    if kind is LovKind.MODEL:
        return tuple(Option(value=m.id, label=m.title or m.id) for m in config.models.values())
    return tuple(
        Option(value=a.id, label=f"{a.title or a.id} · {a.worker}", models=a.models)
        for a in config.agents.values()
    )


def build_forms(
    declared: DeclaredInputs,
    forms: Mapping[str, FormSpec],
    *,
    repositories: tuple[Repository, ...],
    config: Config,
) -> tuple[FlowForm, ...]:
    """Every declared flow as a form, by name: its form's inputs first, in the form's order,
    then the inputs the form leaves out as plain fields."""
    built = []
    for flow in sorted(declared):
        inputs = declared[flow]
        form = forms.get(flow)
        specs = {} if form is None else form.inputs
        order = [*specs, *(name for name in inputs if name not in specs)]
        fields = []
        for name in order:
            spec = specs.get(name, InputSpec(label=name))
            options = (
                None
                if spec.lov is None
                else options_of(spec.lov, repositories=repositories, config=config)
            )
            fields.append(
                FormInput(
                    name=name, label=spec.label, type=inputs[name], lov=spec.lov, options=options
                )
            )
        title = flow if form is None else form.title
        built.append(
            FlowForm(name=flow, title=title, inputs=tuple(fields), has_form=form is not None)
        )
    return tuple(built)


def check_inputs(form: FlowForm, inputs: Mapping[str, Any]) -> list[str]:
    """What keeps ``inputs`` from starting ``form``'s flow, as far as the form can tell: every
    input there, none it does not have, and each value with a list of values one of them. The
    declared types are the engine's to check."""
    names = {field.name for field in form.inputs}
    problems = [f"missing input {name!r}" for name in sorted(names - inputs.keys())]
    problems += [f"{form.name} has no input {name!r}" for name in sorted(inputs.keys() - names)]
    for field in form.inputs:
        if field.options is None or field.name not in inputs:
            continue
        value = inputs[field.name]
        # A list, not a set: a value a client sent may be one no set can hold.
        if value not in [option.value for option in field.options]:
            problems.append(f"input {field.name!r}: {value!r} is not one of its {field.lov} values")
    agent = _chosen(form, inputs, LovKind.AGENT)
    model = _chosen(form, inputs, LovKind.MODEL)
    if agent is not None and agent.models is not None and model is not None:
        if model.value not in agent.models:
            problems.append(f"model {model.value!r} is not one of agent {agent.value!r}'s models")
    return problems


def _chosen(form: FlowForm, inputs: Mapping[str, Any], lov: LovKind) -> Option | None:
    """The option chosen for the form's one input with that list of values, if it has one."""
    fields = [f for f in form.inputs if f.lov is lov and f.options is not None]
    if len(fields) != 1 or fields[0].name not in inputs:
        return None
    options = fields[0].options or ()
    return next((o for o in options if o.value == inputs[fields[0].name]), None)

# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

from __future__ import annotations

import pytest

from robinauts.controller.core.one_shots import request_digest

REQUEST = ("agent", None, "Triage this", {"a": "1", "b": "2"})


@pytest.mark.parametrize(
    ("other", "same"),
    [
        (REQUEST, True),
        (("agent", None, "Triage this", {"b": "2", "a": "1"}), True),
        (("other", None, "Triage this", {"a": "1", "b": "2"}), False),
        # The agent's default named and left out are two requests.
        (("agent", "sonnet", "Triage this", {"a": "1", "b": "2"}), False),
        (("agent", None, "Triage this ", {"a": "1", "b": "2"}), False),
        (("agent", None, "Triage this", {"a": "1"}), False),
        (("agent", None, "Triage this", {"a": "1", "b": "3"}), False),
        # Nothing runs together: the fields are kept apart.
        (("agen", None, "tTriage this", {"a": "1", "b": "2"}), False),
    ],
)
def test_a_repeat_is_the_same_request_field_for_field(
    other: tuple[str, str | None, str, dict[str, str]], same: bool
) -> None:
    assert (request_digest(*other) == request_digest(*REQUEST)) is same


def test_a_prompt_with_a_lone_surrogate_has_one_digest() -> None:
    first = request_digest("agent", None, "half \ud800 a pair", {})
    assert first == request_digest("agent", None, "half \ud800 a pair", {})
    assert len(first) == 64

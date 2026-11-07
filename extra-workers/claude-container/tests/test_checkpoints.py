# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

from __future__ import annotations

import uuid

import pytest

from robinauts.agent_engines.contract.domain import CheckpointNotFoundError
from robinauts_claude_worker.checkpoints import Checkpoint, decode, encode


def test_a_checkpoint_is_the_session_and_the_message() -> None:
    checkpoint = Checkpoint(str(uuid.uuid4()), str(uuid.uuid4()))
    text = encode(checkpoint)
    assert text == f"{checkpoint.session}:{checkpoint.message}"
    assert decode(text) == checkpoint


@pytest.mark.parametrize(
    "text", ["", "abc", str(uuid.uuid4()), f"{uuid.uuid4()}:", f"not-a-uuid:{uuid.uuid4()}", "a:b"]
)
def test_anything_else_is_not_one_of_this_engines(text: str) -> None:
    with pytest.raises(CheckpointNotFoundError, match="is not one of this engine's"):
        decode(text)

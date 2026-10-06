# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The channels endpoint: AG-UI's own run input, from a bridge to a chat platform.

A bridge (``channels/`` at the root of the repository) receives messages on Telegram, Slack or
another platform and runs the agent over AG-UI with a stock client. It speaks for the people
writing there, so it is trusted as a whole: it proves itself with the one secret the operator
shares with it, ``ROBINAUTS_CHANNELS_SECRET``, sent as a bearer, and names in each request the
platform user it speaks for. That user is a user of their own, under ``CHANNEL_PROVIDER``, a
provider no configuration can name, so a bridge can never speak for a person who signs in
through an identity provider, nor for the local development mode's user.

The run input is AG-UI's ``RunAgentInput``, and only three things are read from it:

- ``threadId``: a conversation of that user, to continue it; anything that is not a
  conversation's id starts a new one. The bridge keeps which platform thread is which
  conversation, from the ``X-Robinauts-Conversation-Id`` header of each answer.
- ``messages``: the text of the last user message. The history before it is the store's, as
  it is on the web's wire (``docs/specs/wire.md``), and the rest is not read.
- ``forwardedProps.robinauts``: the user the bridge speaks for, and optionally a model.

A shared secret is the first step, not the last: it is to be replaced by tokens minted for one
bridge each, with the platform identities each may assert (``docs/specs/channels.md``).
"""

from __future__ import annotations

import hmac
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from robinauts.controller.contract.domain import ConfigError, Identity

SECRET_VARIABLE = "ROBINAUTS_CHANNELS_SECRET"
SHORTEST_SECRET = 32

CHANNEL_PROVIDER = "!channel"
"""The provider of every user a bridge speaks for. Not a provider id a configuration can name
(``sign_in.PROVIDER_ID_PATTERN``), so no identity provider's user can be one of these."""

LONGEST_ID = 256


@dataclass(frozen=True, slots=True)
class ChannelsConfig:
    secret: str

    def admits(self, authorization: str) -> bool:
        """Whether an ``Authorization`` header carries the shared secret as a bearer."""
        scheme, _, given = authorization.partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(
            given.encode(), self.secret.encode()
        )


def channels_config(environ: Mapping[str, str]) -> ChannelsConfig | None:
    """The endpoint's configuration, ``None`` when ``ROBINAUTS_CHANNELS_SECRET`` is unset and
    the endpoint is not served. ``ConfigError`` for a secret too short to be one."""
    secret = environ.get(SECRET_VARIABLE, "")
    if not secret:
        return None
    if len(secret) < SHORTEST_SECRET or secret != secret.strip():
        raise ConfigError(
            f"{SECRET_VARIABLE}: a shared secret of at least {SHORTEST_SECRET} characters with"
            " no surrounding spaces, such as the output of `openssl rand -hex 32`"
        )
    return ChannelsConfig(secret)


# --- what a bridge sends -----------------------------------------------------------------


class ChannelUser(BaseModel):
    id: str = Field(min_length=1, max_length=LONGEST_ID)
    name: str | None = Field(default=None, max_length=LONGEST_ID)


class ChannelProps(BaseModel):
    model_config = ConfigDict(extra="ignore")

    user: ChannelUser
    model_id: str | None = None


class ForwardedProps(BaseModel):
    model_config = ConfigDict(extra="ignore")

    robinauts: ChannelProps


class ChannelRunInput(BaseModel):
    """AG-UI's ``RunAgentInput``, as much of it as a channel turn reads. The rest -- the
    client's tools, context and state -- is accepted and ignored."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    thread_id: str = Field(alias="threadId", max_length=LONGEST_ID)
    run_id: str = Field(alias="runId", max_length=LONGEST_ID)
    messages: list[dict[str, Any]]
    forwarded_props: ForwardedProps = Field(alias="forwardedProps")

    def identity(self) -> Identity:
        user = self.forwarded_props.robinauts.user
        return Identity(provider=CHANNEL_PROVIDER, subject=user.id, name=user.name)

    def conversation(self) -> uuid.UUID | None:
        """The conversation the thread names, or ``None`` for a new one."""
        try:
            return uuid.UUID(self.thread_id)
        except ValueError:
            return None

    def question(self) -> str:
        """The text of the last user message, its text parts joined; empty when there is none."""
        return question_of(self.messages)


def question_of(messages: Sequence[Mapping[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                part["text"]
                for part in content
                if isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
            )
        return ""
    return ""

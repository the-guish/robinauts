# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Slack, through Bolt for Python and the Slack SDK (MIT): Socket Mode to connect, the Events API
to serve.

Installed by the ``slack`` extra. ``slack_bolt`` and ``slack_sdk`` are imported here and nowhere
else, and only for their types until the stub is filled in. Socket Mode suits an app for one's own
workspace; Slack admits only Events API apps to its public Marketplace.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import TYPE_CHECKING, Any, ClassVar, Self

from robinauts_connectors.connector import Connector, Inbox, Reply, Webhook
from robinauts_connectors.domain import AllowList, AnswerEvent, Incoming, Mode

if TYPE_CHECKING:
    from slack_sdk.web.async_client import AsyncWebClient


class SlackConnector(Connector):
    name: ClassVar[str] = "slack"
    modes: ClassVar[frozenset[Mode]] = frozenset({Mode.CONNECT, Mode.SERVE})

    BOT_TOKEN = "SLACK_BOT_TOKEN"
    """The bot token, ``xoxb-…``."""

    APP_TOKEN = "SLACK_APP_TOKEN"
    """Connect only: the app-level token, ``xapp-…``, with ``connections:write``."""

    SIGNING_SECRET = "SLACK_SIGNING_SECRET"
    """Serve only: what Slack signs every call with."""

    ALLOWED_USERS = "SLACK_ALLOWED_USERS"
    """Optional: member ids, comma-separated. Unset, the whole workspace."""

    def __init__(
        self,
        *,
        bot_token: str,
        app_token: str | None,
        signing_secret: str | None,
        allowed: AllowList,
    ) -> None:
        raise NotImplementedError

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> Self:
        raise NotImplementedError

    @property
    def allowed(self) -> AllowList:
        raise NotImplementedError

    async def connect(self, inbox: Inbox) -> None:
        """Socket Mode: a Bolt ``AsyncApp`` with an ``AsyncSocketModeHandler``, listening to
        ``app_mention`` and ``message.im``; each event that ``incoming`` keeps is submitted with a
        ``SlackReply``. Slack spreads events across up to ten connections, so a second replica
        shares the work rather than doubling it."""
        raise NotImplementedError

    def webhook(self, inbox: Inbox) -> Webhook:
        """The Events API: refused unless ``X-Slack-Signature`` is the signing secret's over
        ``X-Slack-Request-Timestamp`` and the raw body, and the timestamp is recent;
        ``url_verification`` answered with its challenge; a retry (``X-Slack-Retry-Num``)
        answered and not submitted again. Answered within Slack's three seconds."""
        raise NotImplementedError

    def incoming(self, event: Mapping[str, Any], *, team: str) -> Incoming | None:
        """A person's text, or ``None`` for anything else: the bot itself, another bot, an edit, a
        deletion, a message subtype.

        - Author: ``slack:<team>:<user>``.
        - Thread: ``<team>:<channel>:<thread_ts>``, the thread a mention starts or continues; a
          direct message without a thread is ``<team>:<channel>``.
        - Text: the event's, with ``<@bot>`` removed.
        - Message id: ``<team>:<channel>:<ts>``.
        """
        raise NotImplementedError


class SlackReply(Reply):
    """An answer in one Slack thread.

    Streamed with ``chat.startStream``, ``chat.appendStream`` and ``chat.stopStream`` through the
    SDK's ``AsyncChatStream``, which buffers; where the workspace has no streaming, one message
    posted and updated. Markdown as the agent wrote it, with mentions and links the agent did not
    mean neutralised. A tool shows as a task update while it runs, when the operator asks for it.
    """

    def __init__(
        self,
        client: AsyncWebClient,
        *,
        channel: str,
        thread_ts: str | None,
        recipient_team: str,
        recipient_user: str,
    ) -> None:
        raise NotImplementedError

    async def stream(self, answer: AsyncIterator[AnswerEvent]) -> None:
        raise NotImplementedError

    async def say(self, text: str) -> None:
        raise NotImplementedError

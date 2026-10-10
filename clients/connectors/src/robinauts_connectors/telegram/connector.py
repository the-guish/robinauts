# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Telegram, through aiogram (MIT): long polling to connect, Telegram's webhook to serve.

Installed by the ``telegram`` extra. aiogram is imported here and nowhere else, and only for its
types until the stub is filled in.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import TYPE_CHECKING, ClassVar, Self

from robinauts_connectors.connector import Connector, Inbox, Reply, Webhook
from robinauts_connectors.domain import AllowList, AnswerEvent, Incoming, Mode

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import Message


class TelegramConnector(Connector):
    name: ClassVar[str] = "telegram"
    modes: ClassVar[frozenset[Mode]] = frozenset({Mode.CONNECT, Mode.SERVE})

    TOKEN = "TELEGRAM_BOT_TOKEN"
    """From @BotFather."""

    ALLOWED_USERS = "TELEGRAM_ALLOWED_USERS"
    """Required: user ids or usernames, comma-separated, or ``*``. Anybody can find a bot, and
    spend the deployment's model budget through it."""

    WEBHOOK_SECRET = "TELEGRAM_WEBHOOK_SECRET"
    """Serve only: the ``secret_token`` given to ``setWebhook``, which Telegram sends back in
    ``X-Telegram-Bot-Api-Secret-Token`` on every call."""

    def __init__(self, *, token: str, allowed: AllowList, webhook_secret: str | None) -> None:
        raise NotImplementedError

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> Self:
        raise NotImplementedError

    @property
    def allowed(self) -> AllowList:
        raise NotImplementedError

    async def connect(self, inbox: Inbox) -> None:
        """Long polling (``getUpdates``) through an aiogram ``Dispatcher``: each message that
        ``incoming`` keeps is submitted with a ``TelegramReply``. One process per token: Telegram
        refuses a second poller, and a webhook set for the token, which this removes first."""
        raise NotImplementedError

    def webhook(self, inbox: Inbox) -> Webhook:
        """Telegram's calls, refused unless ``X-Telegram-Bot-Api-Secret-Token`` is
        ``TELEGRAM_WEBHOOK_SECRET``; each update is read as ``connect`` reads one, and answered
        200 at once."""
        raise NotImplementedError

    def incoming(self, message: Message) -> Incoming | None:
        """A person's text, or ``None`` for anything else: a bot, a service message, a message in a
        group that neither mentions the bot nor answers it.

        - Author: ``telegram:<user id>``, the same person in every chat.
        - Thread: ``<chat id>:dm`` in a private chat, ``<chat id>:topic:<thread id>`` in a forum
          topic, ``<chat id>`` in any other group.
        - Text: the message's, with the bot's mention removed.
        - Message id: ``<chat id>:<message id>``.
        """
        raise NotImplementedError


class TelegramReply(Reply):
    """An answer in one chat, under the message it answers.

    While it is written, ``sendMessageDraft`` (Bot API 9.5) shows it growing, at most once a
    second; once it is whole, ``sendMessage`` sends it, split at Telegram's 4,096 characters.
    Formatted as Telegram HTML, with everything the agent wrote escaped first. A tool shows as
    "using <name>…" while it runs, when the operator asks for it.
    """

    def __init__(self, bot: Bot, *, chat_id: int, topic_id: int | None, reply_to: int) -> None:
        raise NotImplementedError

    async def stream(self, answer: AsyncIterator[AnswerEvent]) -> None:
        raise NotImplementedError

    async def say(self, text: str) -> None:
        raise NotImplementedError

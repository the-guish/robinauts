# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""An identity provider a test writes the answers of.

It fetches nothing. Like the real adapter, it has no opinion about what it
hands back: a test can make it publish a discovery document naming the wrong
issuer, or a token response with no ``id_token``, and the application is what
refuses them.

Both methods yield to the event loop before answering, so that two sign-ins in
a ``gather`` really do overlap.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from robinauts.legacy.domain import ProviderConfig, ProviderUnavailableError
from robinauts.legacy.ports import IdentityProvider

Answer = Mapping[str, Any] | BaseException
"""What a test puts in ``documents`` or ``tokens``: an answer, or a failure to raise."""


def discovery_for(provider: ProviderConfig, **changes: Any) -> dict[str, Any]:
    """A discovery document as that provider would publish it, with changes applied.

    A key given as ``None`` is left out, which is how a test makes a document
    that names no token endpoint at all.
    """
    document: dict[str, Any] = {
        "issuer": provider.issuer,
        "authorization_endpoint": f"{provider.issuer}/authorize",
        "token_endpoint": f"{provider.issuer}/token",
    }
    document.update(changes)
    return {key: value for key, value in document.items() if value is not None}


class ScriptedIdentityProvider(IdentityProvider):
    """Answers a test put in ``documents`` and ``tokens``, by provider id."""

    def __init__(self) -> None:
        self.documents: dict[str, Answer] = {}
        """What discovery answers for a provider; an exception is raised instead."""
        self.tokens: dict[str, Answer] = {}
        """What the token endpoint answers; an exception is raised instead."""
        self.discoveries: list[str] = []
        """Every provider discovery was asked for, in order: caching is visible here."""
        self.exchanges: list[dict[str, Any]] = []
        """Every code exchange, as it was posted."""
        self.pause: asyncio.Event | None = None
        """Set it, and discovery waits there until the test lets it go.

        For seeing what the callers waiting on one fetch do while it is still
        in flight -- one of them being cancelled, for instance.
        """

    def publishes(self, provider: ProviderConfig, **changes: Any) -> dict[str, Any]:
        """Make discovery answer with this provider's own document."""
        document = discovery_for(provider, **changes)
        self.documents[provider.id] = document
        return document

    def answers(self, provider: ProviderConfig, response: Answer) -> None:
        """Make the token endpoint answer ``response`` -- or raise it."""
        self.tokens[provider.id] = response

    async def discovery_document(self, provider: ProviderConfig) -> Mapping[str, Any]:
        self.discoveries.append(provider.id)
        await asyncio.sleep(0)
        if self.pause is not None:
            await self.pause.wait()
        return _answer(
            self.documents.get(provider.id),
            f"nothing answers discovery for {provider.id}",
        )

    async def exchange_code(
        self,
        provider: ProviderConfig,
        *,
        token_endpoint: str,
        code: str,
        verifier: str,
        redirect_uri: str,
    ) -> Mapping[str, Any]:
        self.exchanges.append(
            {
                "provider": provider.id,
                "token_endpoint": token_endpoint,
                "code": code,
                "verifier": verifier,
                "redirect_uri": redirect_uri,
            }
        )
        await asyncio.sleep(0)
        return _answer(
            self.tokens.get(provider.id),
            f"nothing answers the token endpoint for {provider.id}",
        )


def _answer(scripted: Answer | None, missing: str) -> Mapping[str, Any]:
    """What the test scripted; raise it if that is what it scripted."""
    if scripted is None:
        raise ProviderUnavailableError(missing)
    if isinstance(scripted, BaseException):
        raise scripted
    return scripted

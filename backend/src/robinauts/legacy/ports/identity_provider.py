# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The two things a sign-in needs from the outside world: a GET and a POST.

This port fetches and posts, and nothing else. It hands back what the provider
sent, as it sent it: the discovery document as a mapping, the token response
as a mapping. It reads no claim, checks no issuer, believes no endpoint.

That is deliberate, and it is what ``docs/layout.md`` requires: an adapter may
not import ``core``, and every rule of sign-in lives in ``core``. So the
application asks here for the bytes and then decides -- the issuer discovery
publishes (``core.check_published_issuer``), whether the endpoints are
``https`` or loopback, what the ID token's claims are worth
(``core.identity_from_id_token``). An implementation of this port that grew an
opinion would be a second place where sign-in is decided.

Failures are the spec's fixed codes, and only these two:

- ``SignInErrorCode.PROVIDER_UNAVAILABLE`` (``ProviderUnavailableError``) --
  the provider could not be reached, timed out, or answered something that is
  not a JSON object at all. Nobody is at fault; trying again may work.
- ``SignInErrorCode.PROVIDER_REFUSED`` -- the provider was reached and said
  no: the token endpoint refused the code, the client or the redirect URI.
  Raised as ``SignInError(SignInErrorCode.PROVIDER_REFUSED, detail)``.

In both, ``detail`` may repeat what the provider said, because it goes to the
log alone; the browser is told the code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any

from robinauts.legacy.domain import ProviderConfig


class IdentityProvider(ABC):
    """An OpenID Connect provider, reached over the network."""

    @abstractmethod
    async def discovery_document(self, provider: ProviderConfig) -> Mapping[str, Any]:
        """Fetch ``{issuer}/.well-known/openid-configuration`` and parse it.

        Returns the document as the provider published it, unchecked: the
        caller decides whether it names the right issuer and usable endpoints.
        Anything that is not a JSON object -- a redirect, an error page, a
        timeout -- is ``ProviderUnavailableError``.

        Nothing is cached here. The application caches a document it has
        checked, so that a failure is never remembered as a success.
        """
        raise NotImplementedError

    @abstractmethod
    async def exchange_code(
        self,
        provider: ProviderConfig,
        *,
        token_endpoint: str,
        code: str,
        verifier: str,
        redirect_uri: str,
    ) -> Mapping[str, Any]:
        """Post the authorization code to ``token_endpoint`` and return the answer.

        The implementation authenticates the client the way
        ``provider.token_endpoint_auth`` says, reading the secret from the
        environment variable ``provider.client_secret_env`` names -- the only
        layer that may touch the environment -- and sends ``verifier`` and
        ``redirect_uri``, which the provider checks against what it was given
        when the sign-in began.

        Returns the token response as a mapping, untouched: the caller takes
        ``id_token`` out of it and checks it. The tokens for the provider's own
        APIs are of no interest here and are discarded with the rest.

        ``token_endpoint`` is the one the caller read out of a discovery
        document it checked, so that the URL posted to is never one this port
        chose for itself.
        """
        raise NotImplementedError

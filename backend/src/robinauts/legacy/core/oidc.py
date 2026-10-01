# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The OpenID Connect requests this deployment makes, as text.

Building an authorization URL is string work with rules in it -- which
parameters, in which form, and what may not already be there -- so it is here,
where a test is input and output and nothing else. The application decides
when to build one; this decides what one is.

The rules that matter: the request carries ``state``, ``nonce`` and a PKCE
``S256`` challenge (``docs/specs/sign-in.md``), and an endpoint a provider
published may not set any parameter the request sets. A discovered endpoint
may carry a query of its own -- Okta's authorization servers do, and it is
kept -- but ours are appended to it, so a ``redirect_uri`` already there would
be the first of two, and which of the two a server reads is its business and
not ours. That is a provider (or whoever answered for one) choosing where a
person's authorization code is sent.
"""

from __future__ import annotations

from urllib.parse import urlencode

from robinauts.legacy.core.hashing import pkce_challenge
from robinauts.legacy.core.urls import endpoint_query
from robinauts.legacy.domain import ProviderConfig

AUTHORIZATION_PARAMETERS = frozenset(
    {
        "response_type",
        "client_id",
        "redirect_uri",
        "scope",
        "state",
        "nonce",
        "code_challenge",
        "code_challenge_method",
    }
)
"""What the authorization request sets, and what its endpoint may not."""

TOKEN_PARAMETERS = frozenset(
    {
        "grant_type",
        "code",
        "redirect_uri",
        "code_verifier",
        "client_id",
        "client_secret",
    }
)
"""What the token request sends, and what its endpoint may not name in a query.

A token endpoint is posted to, so its query is not where the form goes; a
published one that names ``code`` or ``client_secret`` all the same is not an
endpoint this deployment will use.
"""

MAX_CODE_CHARS = 2048
"""The longest authorization code that is sent back to a provider.

Generous: the longest anyone issues is a few hundred characters. Past it the
callback is refused before the code reaches an HTTP client, a log or a
provider -- it came out of a query parameter, and a query parameter is as long
as whoever wrote the link cared to make it.
"""


def parameters_taken(endpoint: str, ours: frozenset[str]) -> tuple[str, ...]:
    """Which of ``ours`` an endpoint's own query already sets, in order.

    Compared without regard to case, and for the same reason the rest of this
    is careful: a server that reads ``Redirect_URI`` as ``redirect_uri`` would
    otherwise be handed one.
    """
    return tuple(sorted({name for name, _ in endpoint_query(endpoint) if name.lower() in ours}))


def authorization_url(
    endpoint: str,
    provider: ProviderConfig,
    *,
    state: str,
    nonce: str,
    verifier: str,
    redirect_uri: str,
) -> str:
    """Where to send the browser to sign in: the authorization request, PKCE and all.

    ``endpoint`` is the authorization endpoint of a discovery document that
    has been checked (``normalise_endpoint``, ``parameters_taken``). Only the
    challenge is sent, never the verifier: that is what PKCE is.
    """
    query = urlencode(
        {
            "response_type": "code",
            "client_id": provider.client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(provider.scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": pkce_challenge(verifier),
            "code_challenge_method": "S256",
        }
    )
    # An endpoint may carry a query of its own; ours are added to it.
    separator = "&" if endpoint_query(endpoint) else "?"
    return f"{endpoint}{separator}{query}"

# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What a conversation can be started with: ``GET /api/agents`` and ``GET /api/models``.

Its own module and its own router, because it is its own subject: the
conversation routes are about one person's conversations, and this is about
what the **deployment** is configured with -- the same lists for everybody
signed in, holding no conversation, no run and nothing of anybody's
(``docs/specs/agents.md``).

Signed in all the same. The lists say which agents an operator runs, on which
engine and on which models, which is not something a deployment tells the
world.

**They may also answer 405 and 500**, described once as the document's
``default`` answer, and they do not serve HEAD -- both for the reasons
``robinauts.legacy.api.conversation_routes`` gives.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from robinauts.legacy.api.access import signed_in, turning
from robinauts.legacy.api.refusals import LISTING_OFFERED
from robinauts.legacy.api.schemas import (
    AgentListResponse,
    AgentSummary,
    ModelListResponse,
    ModelSummary,
)

agent_router = APIRouter(prefix="/api")


@agent_router.get("/agents", tags=["agents"], dependencies=[signed_in()], responses=LISTING_OFFERED)
async def list_agents(request: Request) -> AgentListResponse:
    """The agents this deployment is configured with, for the picker.

    The id to start a conversation with, the title to show, the engine it
    runs on and the model a conversation with it starts on unless another is
    picked. **Not the system prompt**, which is the operator's and is not a
    message, and nothing about a vendor or a key (``docs/specs/agents.md``).

    Declared rather than injected: the list is the deployment's and is the
    same for everybody signed in, so the route needs the declaration and not
    the person.

    They are fixed at start-up in this version: the configuration is read once
    by the composition root, so an agent added or removed shows here after a
    restart.
    """
    return AgentListResponse(
        items=[AgentSummary.of(definition) for definition in turning(request).agents]
    )


@agent_router.get("/models", tags=["models"], dependencies=[signed_in()], responses=LISTING_OFFERED)
async def list_models(request: Request) -> ModelListResponse:
    """The models a conversation of this deployment may run on, for the picker.

    The id to pick one by and the title to show. Nothing about the provider
    it is reached through or the vendor's name for it, which are the
    operator's (``docs/specs/agents.md``). Every agent's default is among
    them, because a deployment that configured it otherwise does not start
    (``application.Turns``).

    **In configuration order**, as the agents are: the operator wrote the
    list, so the order they wrote it in is the one thing about its order that
    means something -- and it is the same on every request, so a picker does
    not reshuffle. A client that wants them sorted by title sorts them.

    Declared rather than injected, and fixed at start-up, for the reasons
    ``list_agents`` gives.
    """
    return ModelListResponse(items=[ModelSummary.of(model) for model in turning(request).models])

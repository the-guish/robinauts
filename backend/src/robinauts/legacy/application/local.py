# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Who everything runs as when there is no sign-in: the local development mode.

The counterpart of ``SignIn``. Where that one answers "who does this cookie
stand for", this one answers "who is anybody here", and the answer is always
the same person: the one local user of ``docs/specs/sign-in.md``.

**A real user, in the real store.** The point of the mode is that the rest of
the platform behaves exactly as it does in a deployment, so the local user is
not an object made up for the occasion: it is a row, got or created by the
same ``CredentialStore.user_at_sign_in`` a sign-in uses, under the reserved
key ``(LOCAL_PROVIDER, LOCAL_SUBJECT)``. It therefore has a real id, it owns
what is created in the mode, ownership checks run against it, and restarting
the server finds the same person rather than making a second one.

**Nothing is cached, and nothing is written twice.** Every request asks the
store, as every request in a deployment asks it for a session: a user held in
the process would be one more thing to be wrong after somebody edited the row,
and ``docs/layout.md`` allows the application to keep only what is public, a
hint, or a number nobody acts on. What every request costs is therefore one
**read** -- ``user_by_key``, an indexed lookup that writes nothing. Creating
is the other call, and it is made only when there is nobody there:
``user_at_sign_in`` is an upsert, which in PostgreSQL rewrites the row it
finds, so asking it on every request would leave a row version and a dead
tuple behind each time somebody read their own conversations.

The mode itself -- that it is loopback only, and what it is served on -- is
``domain.LocalMode``, which this holds and hands to whoever has to enforce it.
"""

from __future__ import annotations

from robinauts.legacy.domain import LOCAL_PROVIDER, LOCAL_SUBJECT, LOCAL_USER_NAME, LocalMode, User
from robinauts.legacy.ports import Clock, CredentialStore


class LocalAccess:
    """The local development mode, and the one user it runs everything as."""

    def __init__(
        self,
        mode: LocalMode,
        *,
        credentials: CredentialStore,
        clock: Clock,
    ) -> None:
        self.mode = mode
        """Where it is served, and the loopback rule that goes with it."""
        self._credentials = credentials
        self._clock = clock

    async def user(self) -> User:
        """The local user: read, and created if this is the first time anybody asked.

        The read is what nearly every request does, and it writes nothing. The
        create is the other call, and it is get-or-create rather than insert,
        so the race the two steps open has already been closed by the port: of
        several first requests arriving at once, all of them find nobody, all
        of them create, and ``user_at_sign_in`` leaves **one** user and hands
        it to each -- the same atomic operation a first sign-in relies on.
        """
        found = await self._credentials.user_by_key(LOCAL_PROVIDER, LOCAL_SUBJECT)
        if found is not None:
            return found
        return await self._credentials.user_at_sign_in(
            LOCAL_PROVIDER,
            LOCAL_SUBJECT,
            name=LOCAL_USER_NAME,
            email=None,
            now=self._clock.now(),
        )

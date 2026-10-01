# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What every ``IdSource`` must do: a real uuid, a new one every time.

Subclass ``IdSourceContract`` and override ``new_source``. It is the smallest
of the contract suites because the port is the smallest, and it exists for the
same reason as the others: the test double and the real adapter are held to
one description, so that nothing downstream is exercised against a shape no
real source has.

**The fake is exempt from nothing here.** Its ids are predictable, which is
the whole point of it, and predictable is not the same as malformed: they are
version 4, they carry the variant a uuid carries, and no two of them are
equal. A test that knew the third id it would be given must still be a test
about ids.

What is **not** here is whether two sources collide with each other, because
that is the one thing the two implementations differ on by design: the real
one must not, the fake must. Each says so where it is tested.
"""

from __future__ import annotations

import uuid

from robinauts.legacy.ports import IdSource

DRAWS = 1_000
"""How many ids a test takes. Enough for a counter that wrapped, a source that
returned a constant, or one that repeated after a short cycle to be seen."""


class IdSourceContract:
    """Subclass this and override ``new_source``."""

    def new_source(self) -> IdSource:
        """A source, as a deployment or a test would make one."""
        raise NotImplementedError("an IdSourceContract subclass overrides `new_source`")

    def test_an_id_is_a_uuid_and_not_the_text_of_one(self) -> None:
        # `domain.checked_uuid` refuses the string spelling rather than
        # parsing it, so a source handing out text would fail at every record
        # it was used to build.
        made = self.new_source().new_id()

        assert isinstance(made, uuid.UUID)
        assert not isinstance(made, str)

    def test_every_id_is_a_version_4_uuid(self) -> None:
        # Version 4 and nothing else: a version 1 uuid says when it was made
        # and on which machine, and these go in URLs.
        source = self.new_source()

        for _ in range(DRAWS):
            made = source.new_id()
            assert made.version == 4
            assert made.variant == uuid.RFC_4122

    def test_no_id_is_handed_out_twice(self) -> None:
        source = self.new_source()

        made = [source.new_id() for _ in range(DRAWS)]

        assert len(set(made)) == DRAWS

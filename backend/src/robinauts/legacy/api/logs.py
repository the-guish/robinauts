# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The one way text from a request is written to a log.

A log is a file of lines, and a line ends at a newline. A path, a header, an
id in a path parameter, a provider's error message: each of them is whatever
somebody sent, and a server writes them percent-decoded, so ``%0A`` in a path
arrives as a real newline. Written out as they are, they do not go **into** a
line -- they make new ones, of whatever shape whoever sent them chose, in the
middle of the record of what the deployment did.

So every log call in ``robinauts.legacy.api`` that carries text from a request puts
it through ``shown``, which lives in ``robinauts.legacy.domain.logs`` -- the
application logs what an engine or a store raised by the same rule, and one
rule is one function -- and is re-exported here, where ``api`` reaches for it:

- **escaped**, with ``ascii()``, which quotes the text and writes every
  control character, every newline and everything outside ASCII as an escape.
  Nothing that comes out of it can end a line or start one;
- **bounded**, because the length is the sender's choice too, and a log that
  can be filled a megabyte at a time is a log that can be made to lose what
  came before.

The body of a response is the other half of the same rule, and it is stricter:
it repeats nothing from the request at all (``robinauts.legacy.api.errors``). This is
for the log, where the particulars are the point.
"""

from __future__ import annotations

from robinauts.legacy.domain import MAX_SHOWN, shown

__all__ = ["MAX_SHOWN", "shown"]

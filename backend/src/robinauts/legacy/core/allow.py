# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The allow list: whether the person a provider authenticated may sign in.

Authentication says who someone is; this says whether we know them. The rules
are deliberately plain, because a mistake here lets a stranger in:

- an entry only ever matches identities from its own provider;
- an address matches only if the provider says it verified it, and case is
  ignored on both sides, as domains and mailbox names are handed around in
  whatever case a person typed;
- case is ignored **within ASCII only**. ``str.lower`` folds characters far
  outside it onto ASCII letters -- ``\u212a``, the Kelvin sign, lowers to
  ``k`` -- so an address holding one would otherwise match an entry it is
  not. An address or a domain with any character outside ASCII therefore
  matches neither ``EMAIL``, ``EMAIL_DOMAIN`` nor ``HOSTED_DOMAIN``; an
  internationalised domain is written in its A-label form, which is ASCII,
  and the configuration says so when it is not;
- a group matches exactly: a groups claim is a list of opaque names, and
  folding case could join two distinct groups at a provider that tells them
  apart;
- the matcher is compared by identity, never by its text, and an entry whose
  matcher is not a ``Matcher`` is refused where it is built.

What ``email_verified`` is worth at Google is decided in
``robinauts.legacy.core.claims``, where the claims are read.
"""

from __future__ import annotations

from collections.abc import Iterable

from robinauts.legacy.domain import AllowEntry, Identity, InvalidValueError, Matcher


def ascii_lower(text: str | None) -> str | None:
    """``text`` lower-cased, if every character of it is ASCII; ``None`` if not.

    ``None`` is "this cannot be compared", which for an allow list means no
    match. Lower-casing outside ASCII is not a safe way to ignore case: the
    Kelvin sign lowers to ``k``, so ``\u212aelvin@example.com`` would
    otherwise be ``kelvin@example.com``.
    """
    if text is None or not text.isascii():
        return None
    return text.lower()


def verified_email(identity: Identity) -> str | None:
    """The identity's address, lower-cased, if the provider verified it.

    ``None`` unless the provider said it verified the address, and the
    address is an ASCII one with an ``@`` in it: an address this cannot
    compare safely is an address it will not match.
    """
    email = identity.email
    if not identity.email_verified or not email or "@" not in email:
        return None
    return ascii_lower(email)


def matches(entry: AllowEntry, identity: Identity) -> bool:
    """Whether this one entry lets ``identity`` sign in."""
    if identity.provider != entry.provider:
        return False
    matcher, value = entry.matcher, entry.value or ""
    # ``is``, not ``==`` and not ``match``: ``Matcher`` is a ``StrEnum``, and
    # a bare string would take a branch the entry never passed the rules of.
    if matcher is Matcher.EVERYONE:
        return True
    if matcher is Matcher.SUBJECT:
        return identity.subject == value
    if matcher is Matcher.EMAIL:
        email, wanted = verified_email(identity), ascii_lower(value)
        return email is not None and wanted is not None and email == wanted
    if matcher is Matcher.EMAIL_DOMAIN:
        email, wanted = verified_email(identity), ascii_lower(value)
        return email is not None and wanted is not None and email.rpartition("@")[2] == wanted
    if matcher is Matcher.HOSTED_DOMAIN:
        domain, wanted = ascii_lower(identity.hosted_domain), ascii_lower(value)
        return domain is not None and wanted is not None and domain == wanted
    if matcher is Matcher.GROUP:
        return value in identity.groups
    raise InvalidValueError(f"no rule for matcher {matcher!r}")


def is_allowed(identity: Identity, allow: Iterable[AllowEntry]) -> bool:
    """Whether any entry of the allow list lets ``identity`` sign in.

    An empty list allows nobody: a deployment that named no entry refuses
    everyone rather than everyone in the world, and the configuration refuses
    to start in that state anyway.
    """
    return any(matches(entry, identity) for entry in allow)

#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The dependency licence gate: DEPENDENCIES.md, enforced.

`DEPENDENCIES.md` states the policy; this script applies it to the whole
locked set of `backend/uv.lock`, runtime and development groups alike. The
categories, and the packages excepted by name, are read from that document:
it is the only place where a dependency is excepted, so the two cannot drift.

The standard library only, on purpose. A gate is worth no more than the code
it runs, and every tool it used would itself have to pass the policy first.

Licence metadata is messy, so the gate fails closed: a package passes only
when its metadata names a licence the gate can resolve to an identifier that
the document allows. Ambiguity is not consent -- the classifier
"License :: OSI Approved :: BSD License", for instance, names no version of
the BSD licence, and no amount of guessing turns it into one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
import urllib.error
import urllib.request
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field
from email.parser import BytesParser
from enum import Enum
from pathlib import Path

PYPI_METADATA_URL = "https://pypi.org/pypi/{name}/{version}/json"
# The one index this gate knows how to ask. A package locked from any other
# is not on pypi.org, whatever a package of that name there may say.
PYPI_INDEX = "https://pypi.org/simple"
NETWORK_TIMEOUT_SECONDS = 30

# The lockfile formats this gate has been read against. uv writes `version`
# for the format and `revision` for changes within it; a new format number
# means the shape of what follows may have changed, and a gate that guesses
# at a format it has not seen is a gate that reports on nothing.
SUPPORTED_LOCK_VERSIONS = (1,)


class Verdict(Enum):
    """What the policy says about a licence, before exceptions are applied."""

    ALLOWED = "allowed"
    RESTRICTED = "restricted"
    UNKNOWN = "unknown"
    FORBIDDEN = "forbidden"


# Ordered from the most to the least acceptable outcome.
_SEVERITY = {
    Verdict.ALLOWED: 0,
    Verdict.RESTRICTED: 1,
    Verdict.UNKNOWN: 2,
    Verdict.FORBIDDEN: 3,
}


def worst(verdicts: Iterable[Verdict]) -> Verdict:
    """The verdict of an `AND`: every part has to be satisfied at once."""
    return max(verdicts, key=_SEVERITY.__getitem__)


def best(verdicts: Iterable[Verdict]) -> Verdict:
    """The verdict of an `OR`: we may take whichever alternative we like."""
    return min(verdicts, key=_SEVERITY.__getitem__)


# ---------------------------------------------------------------------------
# Spelling licences
# ---------------------------------------------------------------------------
# What packages write in `License`, `License-Expression` and in the tail of a
# `License ::` classifier, mapped to one identifier. Only spellings that name
# exactly one licence belong here.

_SPELLINGS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("MIT", ("mit license", "mit license (mit)", "the mit license", "expat")),
    # MIT with the condition that the notice be reproduced taken out. An
    # identifier of its own to SPDX and to this table: "MIT" never means it,
    # and it never means "MIT".
    ("MIT-0", ("mit no attribution", "mit no attribution license", "mit-0 license")),
    (
        "Apache-2.0",
        (
            "apache 2",
            "apache 2.0",
            "apache-2",
            "apache license 2.0",
            "apache license v2.0",
            "apache license version 2.0",
            "apache license, version 2.0",
            "apache software license 2.0",
            "asl 2.0",
        ),
    ),
    (
        "BSD-2-Clause",
        (
            "bsd 2-clause",
            "bsd 2-clause license",
            'bsd 2-clause "simplified" license',
            "bsd-2",
            "freebsd license",
            "simplified bsd license",
        ),
    ),
    (
        "BSD-3-Clause",
        (
            "bsd 3-clause",
            "bsd 3-clause license",
            'bsd 3-clause "new" or "revised" license',
            "bsd-3",
            "modified bsd license",
            "new bsd license",
            "revised bsd license",
        ),
    ),
    ("0BSD", ("bsd zero clause license", "zero-clause bsd")),
    ("ISC", ("isc license", "isc license (iscl)", "iscl")),
    ("Zlib", ("zlib license", "zlib/libpng license")),
    ("PostgreSQL", ("postgresql license", "the postgresql license")),
    # PSF-2.0 and Python-2.0 both come out of the Python Software Foundation,
    # and the trove vocabulary has one classifier for the pair of them: the
    # unversioned name is a family, listed with the others below.
    ("PSF-2.0", ("psf-2", "python software foundation license 2.0")),
    ("Python-2.0", ("python license 2.0",)),
    # The licence CNRI released Python 1.6.1 under, and a third identifier
    # beside the two above: SPDX keeps PSF-2.0, Python-2.0 and CNRI-Python
    # apart, and so does this table. CNRI-Python-GPL-Compatible is a fourth,
    # and is deliberately not spelt here -- it is on no list, and an
    # identifier nobody spelt stays unknown.
    ("CNRI-Python", ("cnri python license", "cnri-python license")),
    ("CC0-1.0", ("cc0", "cc0 1.0 universal (cc0 1.0) public domain dedication")),
    ("Unlicense", ("the unlicense", "the unlicense (unlicense)")),
    # Restricted.
    (
        "MPL-2.0",
        (
            "mpl 2.0",
            "mozilla public license 2.0",
            "mozilla public license 2.0 (mpl 2.0)",
            "mozilla public license version 2.0",
        ),
    ),
    (
        "EPL-2.0",
        ("eclipse public license 2.0", "eclipse public license 2.0 (epl-2.0)"),
    ),
    (
        "CDDL-1.0",
        ("common development and distribution license 1.0 (cddl-1.0)",),
    ),
    (
        "CDDL-1.1",
        ("common development and distribution license 1.1 (cddl-1.1)",),
    ),
    # Forbidden. A version-less family name is enough: every member is
    # forbidden, so there is nothing to disambiguate.
    (
        "GPL",
        (
            "gnu gpl",
            "gnu general public license",
            "gnu general public license (gpl)",
            "gnu public license",
            "gplv1",
        ),
    ),
    (
        "GPL-2.0",
        (
            "gnu general public license v2 (gplv2)",
            "gnu general public license v2 or later (gplv2+)",
            "gpl-2.0-only",
            "gpl-2.0-or-later",
            "gplv2",
            "gplv2+",
        ),
    ),
    (
        "GPL-3.0",
        (
            "gnu general public license v3 (gplv3)",
            "gnu general public license v3 or later (gplv3+)",
            "gpl-3.0-only",
            "gpl-3.0-or-later",
            "gplv3",
            "gplv3+",
        ),
    ),
    (
        "LGPL",
        (
            "gnu lgpl",
            "gnu lesser general public license",
            "gnu library general public license",
            "gnu library or lesser general public license (lgpl)",
        ),
    ),
    (
        "LGPL-2.1",
        (
            "gnu lesser general public license v2 (lgplv2)",
            "gnu lesser general public license v2 or later (lgplv2+)",
            "lgpl-2.1-only",
            "lgpl-2.1-or-later",
            "lgplv2",
            "lgplv2+",
        ),
    ),
    (
        "LGPL-3.0",
        (
            "gnu lesser general public license v3 (lgplv3)",
            "gnu lesser general public license v3 or later (lgplv3+)",
            "lgpl-3.0-only",
            "lgpl-3.0-or-later",
            "lgplv3",
            "lgplv3+",
        ),
    ),
    ("AGPL", ("gnu agpl", "gnu affero general public license")),
    (
        "AGPL-3.0",
        (
            "gnu affero general public license v3",
            "gnu affero general public license v3 or later (agplv3+)",
            "agpl-3.0-only",
            "agpl-3.0-or-later",
            "agplv3",
            "agplv3+",
        ),
    ),
    ("SSPL-1.0", ("server side public license", "sspl")),
    # "BSL" in DEPENDENCIES.md is the Business Source License. The Boost
    # Software License is BSL-1.0 and is deliberately absent from this table:
    # it is not on the allowed list, so it fails as unknown, not as forbidden.
    ("BUSL-1.1", ("business source license 1.1", "bsl-1.1")),
    ("Elastic-2.0", ("elastic license 2.0", "elastic license v2")),
    ("Commons-Clause", ("commons clause",)),
    ("proprietary", ("other/proprietary license", "all rights reserved", "commercial")),
    # "No commercial use" and "do no evil" terms, which DEPENDENCIES.md
    # forbids outright. The trove classifiers that carry them are named here
    # so that they end as forbidden rather than as something a hand-written
    # exception could cover.
    (
        "non-commercial",
        (
            "free for non-commercial use",
            "free for educational use",
            "free for home use",
            "free to use but restricted",
            "non-commercial",
            # The JSON licence: MIT plus "shall be used for Good, not Evil".
            "json license",
        ),
    ),
)

_ALIASES: dict[str, str] = {}
for _identifier, _spellings in _SPELLINGS:
    _ALIASES[_identifier.lower()] = _identifier
    for _spelling in _spellings:
        _ALIASES[_spelling] = _identifier

# Recognised, but naming a family rather than a licence. They resolve to
# nothing: a family is not an identifier. This is the only kind of vagueness a
# by-name exception in DEPENDENCIES.md may cover, so every family listed here
# is one whose members are all permissive -- the trove vocabulary has no more
# precise classifier for them. A family with a forbidden member does not
# belong here; "Creative Commons" covers CC-BY-NC, and no hand-written
# exception should be able to wave that through.
_AMBIGUOUS = frozenset(
    {
        "apache software license",
        "bsd",
        "bsd license",
        "psf",
        "psf license",
        "python software foundation license",
    }
)

# Applied to a resolved identifier, for the sake of a precise message. The
# allowed list is closed, so an unmatched identifier fails either way.
_FORBIDDEN_IDENTIFIER = re.compile(
    r"^(a?gpl|lgpl|sspl|busl|elastic|commons-clause|cc-by-nc|cc-by-nd"
    r"|proprietary|non-commercial)\b",
    re.IGNORECASE,
)


def normalise(text: str) -> str:
    """The form used to look a licence spelling up: no case, no extra space."""
    return re.sub(r"\s+", " ", text.strip().strip('"').strip()).lower().rstrip(".")


def identifier_for(text: str) -> str | None:
    """The licence `text` names, or None if it names none or names a family."""
    key = normalise(text)
    if not key:
        return None
    # A trailing "+" ("GPL-2.0+") means "or later"; the family is the same.
    return _ALIASES.get(key) or _ALIASES.get(key.rstrip("+"))


def identifier_from_classifier(classifier: str) -> str | None:
    """The licence a trove `License :: ...` classifier names, if exactly one."""
    if not classifier.startswith("License ::"):
        return None
    return identifier_for(classifier.split("::")[-1])


def names_a_family(text: str) -> bool:
    """Whether `text` is a licence family we recognise but cannot resolve."""
    return normalise(text) in _AMBIGUOUS


# ---------------------------------------------------------------------------
# The policy, read from DEPENDENCIES.md
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NamedException:
    """One row of an exception table of DEPENDENCIES.md."""

    licence: str
    """The licence a person read and wrote down."""
    version: str = ""
    """The version they read it in; an exception holds for that version only."""
    scope: str = ""
    """What the row says the package is for; "development only" is enforced."""

    @property
    def development_only(self) -> bool:
        return "development only" in self.scope.lower()


@dataclass(frozen=True)
class Policy:
    """The rules of DEPENDENCIES.md, as data."""

    allowed: frozenset[str]
    restricted: frozenset[str]
    restricted_packages: dict[str, NamedException]
    """Canonical package name -> the row of the restricted table."""
    development_exceptions: dict[str, NamedException]
    """Canonical package name -> the row of the development-only table."""
    licence_text_exceptions: dict[str, NamedException] = field(default_factory=dict)
    """Canonical package name -> the row of the licence-text table.

    Empty unless the document says otherwise, so that a policy built some other
    way than from DEPENDENCIES.md excepts nothing it was not told to.
    """

    def category(self, identifier: str) -> Verdict:
        """What the policy makes of one licence.

        Forbidden first, and deliberately: the lists come out of a document
        anyone can edit, and a line added to the allowed paragraph must not be
        able to turn the GPL into something we may ship. `parse_policy` refuses
        such a document outright; this order means the gate would hold even if
        a policy were built some other way.
        """
        folded = identifier.lower().rstrip("+")
        if _FORBIDDEN_IDENTIFIER.match(folded):
            return Verdict.FORBIDDEN
        if folded in self.allowed:
            return Verdict.ALLOWED
        if folded in self.restricted:
            return Verdict.RESTRICTED
        return Verdict.UNKNOWN


def canonical_name(name: str) -> str:
    """The PEP 503 form of a package name."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _section(document: str, heading: str) -> str:
    """The text under `heading`, up to the next heading of any level."""
    match = re.search(
        rf"^#+\s+{re.escape(heading)}\s*$\n(.*?)(?=^#+\s|\Z)",
        document,
        re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise ValueError(f"DEPENDENCIES.md has no {heading!r} section")
    return match.group(1)


def _expand(identifier: str) -> set[str]:
    """Expand a `CDDL-1.x` style wildcard into the versions it stands for."""
    if identifier.lower().endswith(".x"):
        stem = identifier[: -len(".x")]
        return {f"{stem}.0", f"{stem}.1"}
    return {identifier}


def _identifier_list(section: str, category: str) -> frozenset[str]:
    """The comma-separated licence list in the first paragraph of a section.

    A category that lists something the gate knows to be forbidden is not a
    policy decision, it is an accident or an attack; either way the gate
    refuses the document rather than the licence.
    """
    paragraph = next(part for part in section.strip().split("\n\n") if part.strip())
    identifiers: set[str] = set()
    for token in paragraph.replace("\n", " ").rstrip(".").split(","):
        token = token.strip()
        if not token:
            continue
        if not re.fullmatch(r"[A-Za-z0-9.+-]+", token):
            raise ValueError(f"not a licence identifier in DEPENDENCIES.md: {token!r}")
        for value in _expand(token):
            if _FORBIDDEN_IDENTIFIER.match(value):
                raise ValueError(
                    f"DEPENDENCIES.md lists {value} as {category}, and the gate knows it "
                    "as forbidden; the two cannot both be true"
                )
            identifiers.add(value.lower())
    if not identifiers:
        raise ValueError("a licence category in DEPENDENCIES.md is empty")
    return frozenset(identifiers)


def _table(section: str) -> tuple[list[str], list[list[str]]]:
    """The header and the body rows of the first markdown table in a section."""
    header: list[str] = []
    rows: list[list[str]] = []
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", cell) for cell in cells):
            continue
        if not header:
            header = [cell.lower() for cell in cells]
            continue
        rows.append(cells)
    return header, rows


def _named_packages(row: str) -> list[str]:
    """The package names of a table cell, which may list several."""
    return [canonical_name(name) for name in re.findall(r"`([^`]+)`", row)]


def _exceptions(section: str, *, versioned: bool, scope: str = "") -> dict[str, NamedException]:
    """One exception table of DEPENDENCIES.md, read by its column headings.

    The columns are found by name rather than by position, so a column may be
    added to the document without silently changing what the gate reads.
    `scope` is what every row of the table means by being in it, for a table
    that has no scope column of its own.
    """
    header, rows = _table(section)
    for wanted in ("package", "licence") + (("version",) if versioned else ()):
        if wanted not in header:
            raise ValueError(f"an exception table of DEPENDENCIES.md has no {wanted!r} column")
    package_at = header.index("package")
    licence_at = header.index("licence")
    version_at = header.index("version") if versioned else None
    scope_at = header.index("scope") if "scope" in header else None

    exceptions: dict[str, NamedException] = {}
    for cells in rows:
        if len(cells) != len(header):
            raise ValueError(f"a row of DEPENDENCIES.md has {len(cells)} of {len(header)} cells")
        names = _named_packages(cells[package_at])
        if not names:
            raise ValueError("a row of DEPENDENCIES.md names no package in backticks")
        version = cells[version_at].strip("` ") if version_at is not None else ""
        stated = cells[scope_at] if scope_at is not None else scope
        for name in names:
            exceptions[name] = NamedException(cells[licence_at].strip("` "), version, stated)
    return exceptions


def parse_policy(document: str) -> Policy:
    """Read the categories and the named exceptions out of DEPENDENCIES.md.

    A package is excepted from being unclassifiable in one table or the other,
    never both: the two tables hold a package to different conditions, and a
    row in each would leave which of them applies to the order of the code.
    """
    development_exceptions = _exceptions(
        _section(document, "Excepted development-only dependencies"),
        versioned=True,
        # A development-only exception says so by being in that table at all.
        scope="development only",
    )
    licence_text_exceptions = _exceptions(
        _section(document, "Excepted licence texts"), versioned=True
    )
    both = sorted(set(development_exceptions) & set(licence_text_exceptions))
    if both:
        raise ValueError(
            "DEPENDENCIES.md excepts " + ", ".join(both) + " in two tables; "
            "a package is excepted once, in the table whose conditions it meets"
        )
    return Policy(
        allowed=_identifier_list(_section(document, "Allowed"), "allowed"),
        restricted=_identifier_list(_section(document, "Restricted"), "restricted"),
        restricted_packages=_exceptions(
            _section(document, "Restricted dependencies in use"), versioned=False
        ),
        development_exceptions=development_exceptions,
        licence_text_exceptions=licence_text_exceptions,
    )


# ---------------------------------------------------------------------------
# Licence expressions
# ---------------------------------------------------------------------------

# SPDX operators are uppercase, which is what makes them safe to split on:
# "GNU Library or Lesser General Public License" is one name, not a choice.
_OPERATOR = re.compile(r"\(|\)|\bAND\b|\bOR\b|\bWITH\b")


def _tokenise(text: str) -> list[str]:
    tokens: list[str] = []
    position = 0
    for match in _OPERATOR.finditer(text):
        operand = text[position : match.start()].strip()
        if operand:
            tokens.append(operand)
        tokens.append(match.group())
        position = match.end()
    operand = text[position:].strip()
    if operand:
        tokens.append(operand)
    return tokens


class _Expression:
    """A recursive-descent reading of `a AND (b OR c)`, evaluated as it goes."""

    def __init__(self, tokens: Sequence[str], policy: Policy) -> None:
        self._tokens = tokens
        self._policy = policy
        self._at = 0
        self.resolved: set[str] = set()
        """Every licence an operand named, whatever the operators did with it."""

    def _peek(self) -> str | None:
        return self._tokens[self._at] if self._at < len(self._tokens) else None

    def _take(self) -> str:
        token = self._tokens[self._at]
        self._at += 1
        return token

    def parse(self) -> Verdict:
        verdict = self._choice()
        if self._peek() is not None:
            raise ValueError(f"trailing {self._peek()!r}")
        return verdict

    def _choice(self) -> Verdict:
        verdicts = [self._conjunction()]
        while self._peek() == "OR":
            self._take()
            verdicts.append(self._conjunction())
        return best(verdicts)

    def _conjunction(self) -> Verdict:
        verdicts = [self._atom()]
        while self._peek() == "AND":
            self._take()
            verdicts.append(self._atom())
        return worst(verdicts)

    def _atom(self) -> Verdict:
        token = self._peek()
        if token is None:
            raise ValueError("an operand is missing")
        if token == "(":
            self._take()
            verdict = self._choice()
            if self._peek() != ")":
                raise ValueError("a parenthesis is unclosed")
            self._take()
        elif token in {")", "AND", "OR", "WITH"}:
            raise ValueError(f"unexpected {token!r}")
        else:
            self._take()
            identifier = identifier_for(token)
            if identifier is None:
                verdict = Verdict.UNKNOWN
            else:
                self.resolved.add(identifier)
                verdict = self._policy.category(identifier)
        if self._peek() == "WITH":
            # An exception rewrites the licence it applies to; nothing here
            # can judge the result, so it does not pass.
            self._take()
            if self._peek() is None:
                raise ValueError("an exception name is missing")
            self._take()
            return Verdict.UNKNOWN
        return verdict


def evaluate_expression(text: str, policy: Policy) -> tuple[Verdict, frozenset[str]]:
    """Judge a licence expression.

    Returns the verdict and every licence the text named. An empty set means
    the text named none, which lets a caller tell "this says nothing" -- the
    whole licence pasted into a metadata field, say -- from "this says
    something bad".
    """
    # A licence name may itself contain brackets ("MIT License (MIT)") or a
    # lowercase "or", so a name the tables know is read as a name, not parsed.
    identifier = identifier_for(text)
    if identifier is not None:
        return policy.category(identifier), frozenset({identifier})
    try:
        expression = _Expression(_tokenise(text), policy)
        verdict = expression.parse()
    except (ValueError, IndexError):
        return Verdict.UNKNOWN, frozenset()
    return verdict, frozenset(expression.resolved)


# ---------------------------------------------------------------------------
# From package metadata to a verdict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Metadata:
    """What a package says about its own licence, and where we read it."""

    expression: str | None = None
    declared: str | None = None
    classifiers: tuple[str, ...] = ()
    origin: str = ""


@dataclass(frozen=True)
class Finding:
    verdict: Verdict
    licence: str
    """Every claim the metadata made, as written, for the report."""
    family_only: bool = False
    """The metadata named permissive licence families and nothing sharper.

    This is the one kind of vagueness a by-name exception may cover: a person
    can open the package and read the licence the classifier would not say.
    Metadata that states no licence at all, or one we cannot read, is not
    vague -- it is missing, and DEPENDENCIES.md forbids that outright. (A
    licence text pasted whole is the one unreadable case a row may cover, and
    it does so through `title`, in a table of its own, never through this.) One
    claim out of several being readable is not vagueness either: it is a
    contradiction, and a person settles that by fixing the metadata upstream.
    """
    identifiers: frozenset[str] = frozenset()
    """The licences the claims resolved to, whatever the verdict made of them."""
    title: str | None = None
    """The licence the first line of a pasted licence text names, if it names one.

    Set only for metadata whose one licence claim is the free-text field, and
    whose field named nothing as a whole -- the licence text pasted where its
    name belongs. A licence text begins with its title ("MIT License"), so the
    first line is the part of it that says which licence it is, and what it
    says is held against a row of the "Excepted licence texts" table. It is evidence
    for that row and for nothing else: it is never a claim, and it never makes
    a verdict.
    """
    text_names: frozenset[str] = frozenset()
    """The licences a tripwire over that same pasted text found, the title's included.

    Set alongside `title`, and for the same row: a text headed "MIT License"
    that says "Portions: GPL-3.0-only" further down is not the text a row
    saying MIT was written about, however well its first line reads. It is a
    tripwire, not a reading. What is looked at is each whole line, the pieces
    of a line between punctuation and the SPDX operators, and each word shaped
    like an identifier -- one with a digit, a hyphen or a "+" in it, or one
    written in capitals -- which is also held to the forbidden pattern, so
    `CC-BY-NC-4.0` or `GPL-licensed` is caught though no spelling names it.

    A licence named only in running prose -- "licensed under the GNU General
    Public License version 3" in the middle of a sentence -- is **not** seen,
    and matching prose for substrings is deliberately not attempted: it would
    refuse the permissive texts themselves ("commercial" is a spelling of a
    forbidden term, and an ordinary word in more than one of them). The
    safeguard for prose is the person who signs the row for that version,
    having read the whole text.
    """


def assess(metadata: Metadata, policy: Policy) -> Finding:
    """Judge one package's metadata.

    A package may say what its licence is three times over -- in
    `License-Expression`, in the free-text `License` field, and in its
    classifiers -- and the three do not always agree. Every claim that names a
    licence counts, and the worst of them wins: a disagreement between an SPDX
    expression and a GPL classifier is not ours to resolve in our own favour,
    and the report prints both so that a person can.

    A claim that names no licence counts for nothing, because the free-text
    field so often holds the whole licence rather than its name. The exception
    is `License-Expression`, which is SPDX by definition: if we cannot read
    that, we have not understood the package. A package that names nothing
    anywhere is judged by `_unresolved`.
    """
    claims: list[tuple[str, Verdict]] = []
    identifiers: set[str] = set()

    if metadata.expression:
        verdict, resolved = evaluate_expression(metadata.expression, policy)
        claims.append((metadata.expression.strip(), verdict))
        identifiers |= resolved

    if metadata.declared:
        verdict, resolved = evaluate_expression(metadata.declared, policy)
        if resolved:
            claims.append((metadata.declared.strip(), verdict))
            identifiers |= resolved

    for classifier in metadata.classifiers:
        identifier = identifier_from_classifier(classifier)
        if identifier is not None:
            claims.append((identifier, policy.category(identifier)))
            identifiers.add(identifier)

    if not claims:
        return _unresolved(metadata)
    return Finding(
        worst(verdict for _, verdict in claims),
        ", ".join(dict.fromkeys(claim for claim, _ in claims)),
        identifiers=frozenset(identifiers),
    )


def _unresolved(metadata: Metadata) -> Finding:
    """Judge metadata that named no licence we could resolve.

    Three different things, which the policy treats differently: licence
    families we recognise but cannot pin down, which an exception may cover;
    something we cannot read, which it may not -- save the one narrow case of
    a licence text standing alone in the free-text field, whose title line is
    carried on the finding for the "Excepted licence texts" table to check;
    and no statement at all, which DEPENDENCIES.md lists among the forbidden
    outright.
    """
    classifiers = [c for c in metadata.classifiers if c.startswith("License ::")]
    claims = ([metadata.declared] if metadata.declared else []) + classifiers

    if claims and all(names_a_family(text.split("::")[-1]) for text in claims):
        return Finding(Verdict.UNKNOWN, f"a licence family only ({claims[0].strip()})", True)
    if claims:
        # Only when the text is all the metadata says: a classifier beside it,
        # even one that names a mere family, is a second claim the row was not
        # written about.
        alone = metadata.declared if metadata.declared and not classifiers else None
        return Finding(
            Verdict.UNKNOWN,
            f"nothing we can read ({' '.join(claims[0].split())[:60]})",
            title=_title_of(alone) if alone else None,
            text_names=_named_in_text(alone) if alone else frozenset(),
        )
    return Finding(Verdict.FORBIDDEN, "no licence at all")


def _title_of(text: str) -> str | None:
    """The licence the first non-empty line of a licence text names, if any."""
    first = next((line for line in text.splitlines() if line.strip()), "")
    return identifier_for(first)


# What separates one licence name from another inside a line of prose: the
# punctuation of "Portions: GPL-3.0-only" or "(MIT)", and the SPDX operators.
_FRAGMENT_BREAK = re.compile(r"[:;,()\[\]]|\bAND\b|\bOR\b|\bWITH\b")
# A word shaped like an identifier rather than like prose.
_IDENTIFIER_SHAPED = re.compile(r"[0-9+-]")


def _named_in_text(text: str) -> frozenset[str]:
    """What the tripwire of `Finding.text_names` finds in a pasted licence text."""
    found: set[str] = set()
    for line in text.splitlines():
        words = [word.strip(".,;:()[]\"'") for word in line.split()]
        shaped = [w for w in words if w and (_IDENTIFIER_SHAPED.search(w) or w.isupper())]
        for candidate in [line, *_FRAGMENT_BREAK.split(line), *shaped]:
            identifier = identifier_for(candidate)
            if identifier is not None:
                found.add(identifier)
        # A word shaped like an identifier that no spelling knows --
        # `CC-BY-NC-4.0`, `AGPL-3.0`, `GPL-licensed` -- is still held to the
        # forbidden pattern, and kept as written.
        found.update(
            word
            for word in shaped
            if identifier_for(word) is None and _FORBIDDEN_IDENTIFIER.match(word)
        )
    return frozenset(found)


# ---------------------------------------------------------------------------
# The locked set
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LockedPackage:
    name: str
    version: str
    local: bool = False
    """The project itself, or a member of its workspace: ours, and Apache-2.0.

    Nothing else earns this, however it is spelled in the lock. An editable
    path dependency is somebody else's code sitting in our tree, and it proves
    its licence like any other package.
    """
    index: str | None = PYPI_INDEX
    """The package index it was locked from, or None for anything else.

    Which index matters: PyPI can only be asked about what it serves, and a
    package of the same name on another index is a different package.
    """
    artefacts: frozenset[str] = frozenset()
    """The sha256 of every file the lock pins for this package.

    Metadata is only believed when it belongs to one of these, so what the
    gate reads about a package is what the build would install.
    """


def _edges(entry: dict) -> list[str]:
    names = [dependency["name"] for dependency in entry.get("dependencies", [])]
    for group in entry.get("optional-dependencies", {}).values():
        names.extend(dependency["name"] for dependency in group)
    return [canonical_name(name) for name in names]


def _closure(roots: Iterable[str], edges: dict[str, list[str]]) -> set[str]:
    reached: set[str] = set()
    queue = list(roots)
    while queue:
        name = queue.pop()
        if name in reached:
            continue
        reached.add(name)
        queue.extend(edges.get(name, ()))
    return reached


def _is_ours(
    name: str,
    source: dict,
    members: set[str],
    project: str | None,
    beside: Collection[tuple[str, str]] = (),
) -> bool:
    """Whether a locked package is this repository's own code.

    Two ways to be ours, and no other: to be the project itself, which is
    locked as an editable (or virtual) source at the root of the lock and
    carries the name its `pyproject.toml` gives; or to be a workspace member
    uv names in `[manifest]`. A third party's code that happens to live in a
    directory of ours is judged like anything else, whatever it calls itself
    and however its source is spelled.

    An extra worker's lock adds the projects it is composed from, as `beside`
    names them, each by its name and locked editable at exactly the path it
    gives: the backend, and the projects beside the worker under
    `extra-workers/` (`worker-common`).
    """
    if not {"editable", "virtual"} & set(source):
        return False
    if name in members:
        return True
    if (name, source.get("editable")) in beside:
        return True
    if project is None or name != project:
        return False
    return any(source.get(kind) == "." for kind in ("editable", "virtual"))


def _artefacts(entry: dict) -> frozenset[str]:
    """The sha256 of every file the lock pins for one package."""
    files = list(entry.get("wheels") or [])
    sdist = entry.get("sdist")
    if isinstance(sdist, dict):
        files.append(sdist)
    digests = set()
    for published in files:
        digest = published.get("hash", "") if isinstance(published, dict) else ""
        prefix, _, value = digest.partition(":")
        if prefix == "sha256" and value:
            digests.add(value)
    return frozenset(digests)


def _index(source: dict) -> str | None:
    """The package index a source names, in a form that can be compared."""
    registry = source.get("registry")
    if not isinstance(registry, str):
        return None
    return registry.rstrip("/").lower()


def _entries(lock: dict) -> list[dict]:
    """The `[[package]]` tables, checked for the shape the rest relies on."""
    entries = lock.get("package", [])
    if not isinstance(entries, list):
        raise ValueError("the lockfile's `package` is not a list of packages")
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise ValueError(f"the lockfile holds a package with no name: {entry!r:.60}")
    return entries


def read_lock(
    lock: dict, project: str | None = None, beside: Collection[tuple[str, str]] = ()
) -> tuple[list[LockedPackage], set[str]]:
    """The locked packages, and the names that only development brings in.

    Everything that could leave the gate with nothing to check is an error
    here rather than an empty, green run: a lock in a format we have not been
    taught, a lock that holds no package, or one that does not contain the
    project itself and so is not this project's lock at all.
    """
    version = lock.get("version")
    if version not in SUPPORTED_LOCK_VERSIONS:
        raise ValueError(
            f"the lockfile says version = {version!r}; this gate reads "
            f"{', '.join(str(known) for known in SUPPORTED_LOCK_VERSIONS)} and has to be "
            "taught the rest before it can be believed"
        )

    packages: list[LockedPackage] = []
    edges: dict[str, list[str]] = {}
    runtime_roots: list[str] = []
    development_roots: list[str] = []
    members = {canonical_name(name) for name in lock.get("manifest", {}).get("members", [])}

    for entry in _entries(lock):
        name = canonical_name(entry["name"])
        source = entry.get("source") or {}
        local = _is_ours(name, source, members, project, beside)
        packages.append(
            LockedPackage(
                name,
                str(entry.get("version", "")),
                local,
                index=_index(source),
                artefacts=_artefacts(entry),
            )
        )
        edges[name] = _edges(entry)
        if local:
            runtime_roots.extend(edges[name])
            for group in entry.get("dev-dependencies", {}).values():
                development_roots.extend(canonical_name(item["name"]) for item in group)

    if not packages:
        raise ValueError("the lockfile names no package at all; there is nothing to check")
    if not any(package.local for package in packages):
        raise ValueError(
            "the lockfile does not contain the project itself, so it is not this "
            "project's lock; refusing to report on it"
        )

    runtime = _closure(runtime_roots, edges)
    development_only = _closure(development_roots, edges) - runtime
    return sorted(packages, key=lambda package: package.name), development_only


# ---------------------------------------------------------------------------
# Where licence metadata comes from
# ---------------------------------------------------------------------------


def _file_digest(entry: dict) -> str:
    """The sha256 PyPI publishes for a file itself, to match against the lock."""
    digests = entry.get("digests")
    return digests.get("sha256", "") if isinstance(digests, dict) else ""


def _metadata_digest(entry: dict) -> str | None:
    """The sha256 PyPI publishes for a file's METADATA, if it publishes one."""
    digest = entry.get("core-metadata") or entry.get("data-dist-info-metadata")
    return digest.get("sha256") if isinstance(digest, dict) else None


def _metadata_from_message(raw: bytes, origin: str) -> Metadata:
    message = BytesParser().parsebytes(raw)
    return Metadata(
        expression=message.get("License-Expression"),
        declared=message.get("License"),
        classifiers=tuple(message.get_all("Classifier") or ()),
        origin=origin,
    )


@dataclass(frozen=True)
class Lookup:
    """The metadata of one package, or why there is none to judge."""

    metadata: Metadata | None = None
    problem: str = ""
    unavailable: bool = True
    """The gate could not get at the metadata, rather than disbelieving it.

    The two fail the build alike, but not for the same reason: one says the
    gate could not do its work, the other says what it read does not check
    out. Only the second is anything to do with the package's licence.
    """


class MetadataSource:
    """Package metadata, from the synced environment first, PyPI second.

    Both are metadata we can stand behind. The environment holds what uv
    installed from the hash-pinned artefact of the lock. PyPI serves the
    METADATA of a published file beside it (PEP 658) with a digest of its own,
    and the gate reads it only for a file the lockfile itself pins by hash --
    so what it judges is the licence of the artefact the build would install,
    not of whatever the index is serving under that name today. Metadata it
    cannot tie to the lock, or cannot verify, it does not read at all.
    """

    def __init__(self, environment: Path | None, offline: bool = False) -> None:
        self._offline = offline
        self._installed: dict[tuple[str, str], Metadata] = {}
        if environment is not None:
            self._scan(environment)

    # Where a virtual environment keeps its installed distributions, on this
    # platform and on Windows.
    _LAYOUTS = (
        "lib/*/site-packages/*.dist-info/METADATA",
        "Lib/site-packages/*.dist-info/METADATA",
    )

    def _scan(self, environment: Path) -> None:
        found = {path for layout in self._LAYOUTS for path in environment.glob(layout)}
        for metadata_file in sorted(found):
            try:
                raw = metadata_file.read_bytes()
            except OSError:
                continue
            message = BytesParser().parsebytes(raw)
            name, version = message.get("Name"), message.get("Version")
            if not name or not version:
                continue
            origin = f"{environment.name}"
            self._installed[(canonical_name(name), version)] = _metadata_from_message(raw, origin)

    def get(self, package: LockedPackage) -> Lookup:
        installed = self._installed.get((package.name, package.version))
        if installed is not None:
            return Lookup(installed)
        if package.index != PYPI_INDEX:
            where = package.index or "outside any package index"
            return Lookup(
                problem=(
                    f"locked from {where} and not installed here, so there is no "
                    "metadata to read; pypi.org would be a different package"
                )
            )
        if self._offline:
            return Lookup(problem="not installed here, and PyPI was not asked (--offline)")
        return self._from_pypi(package)

    def _from_pypi(self, package: LockedPackage) -> Lookup:
        raw = self._fetch(PYPI_METADATA_URL.format(name=package.name, version=package.version))
        if raw is None:
            return Lookup(problem="PyPI could not be reached")
        try:
            release = json.loads(raw)
        except ValueError:
            return Lookup(problem="PyPI answered with something that is not a release")
        return self._from_published_files(release.get("urls") or [], package.artefacts)

    def _from_published_files(self, files: Sequence[dict], artefacts: frozenset[str]) -> Lookup:
        """Read the METADATA of a file the lockfile pins.

        Two digests have to hold. The file has to be one of those the lock
        pins by sha256, so the metadata belongs to the artefact the build
        would install; and its METADATA has to match the digest PyPI publishes
        beside it (PEP 658). A mismatch there is never shrugged off --
        something is wrong with the file or with what served it, and either
        way the package does not pass.
        """
        pinned = [entry for entry in files if _file_digest(entry) in artefacts]
        wheels_first = sorted(pinned, key=lambda entry: entry.get("packagetype") != "bdist_wheel")
        hashed = [entry for entry in wheels_first if _metadata_digest(entry) and entry.get("url")]
        if not pinned:
            return Lookup(problem="PyPI lists no file this lockfile pins for that version")
        if not hashed:
            return Lookup(problem="PyPI publishes no hashed metadata for the file we pin")

        for entry in hashed:
            raw = self._fetch(f"{entry['url']}.metadata")
            if raw is None:
                continue
            if hashlib.sha256(raw).hexdigest() != _metadata_digest(entry):
                return Lookup(
                    problem=(
                        f"the metadata PyPI served for {entry['filename']} does not match "
                        "the digest it publishes for it"
                    ),
                    unavailable=False,
                )
            return Lookup(_metadata_from_message(raw, "PyPI"))
        return Lookup(problem="the metadata PyPI publishes could not be downloaded")

    @staticmethod
    def _fetch(url: str) -> bytes | None:
        request = urllib.request.Request(url, headers={"User-Agent": "robinauts-licence-gate"})
        try:
            with urllib.request.urlopen(request, timeout=NETWORK_TIMEOUT_SECONDS) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError, ValueError):
            return None


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@dataclass
class Outcome:
    package: LockedPackage
    scope: str
    finding: Finding
    origin: str
    passed: bool
    reason: str
    unchecked: bool = False
    """Nothing was read about this package, so nothing was judged."""


def decide(
    package: LockedPackage,
    development_only: bool,
    finding: Finding,
    policy: Policy,
) -> tuple[bool, str]:
    """Apply the policy, and the exceptions DEPENDENCIES.md names, to one package.

    An exception is a person's signature under a licence they went and read.
    It is worth something only where it is checkable, so the gate holds it to
    what the table says: the right package, the licence the table states, and
    -- for a development-only exception, where nothing in the metadata can be
    re-checked later -- the very version somebody read it in.
    """
    if finding.verdict is Verdict.ALLOWED:
        return True, "on the allowed list"

    if finding.verdict is Verdict.FORBIDDEN:
        # No exception reaches this far. DEPENDENCIES.md forbids these for
        # development and test dependencies too.
        return False, "forbidden by DEPENDENCIES.md"

    if finding.verdict is Verdict.RESTRICTED:
        return _restricted(package, development_only, finding, policy)

    return _unclassified(package, development_only, finding, policy)


def _restricted(
    package: LockedPackage,
    development_only: bool,
    finding: Finding,
    policy: Policy,
) -> tuple[bool, str]:
    """A restricted licence passes only on the terms the table writes down.

    The table names one licence, and that has to be the only restricted
    licence the metadata resolves to. A package offered under a choice of two
    restricted licences therefore fails: which of them we rely on is a
    decision, and the row has room for only one. The row's scope is held to as
    well -- "development only" means the gate fails if the runtime brings the
    package in.
    """
    named = policy.restricted_packages.get(package.name)
    if named is None:
        return False, (
            "restricted, and not in the 'Restricted dependencies in use' table of DEPENDENCIES.md"
        )

    stated = identifier_for(named.licence)
    if stated is None or policy.category(stated) is not Verdict.RESTRICTED:
        return False, (
            f"the 'Restricted dependencies in use' table states {named.licence} for it, "
            "which is not a restricted licence of DEPENDENCIES.md"
        )

    relied_on = {
        identifier
        for identifier in finding.identifiers
        if policy.category(identifier) is Verdict.RESTRICTED
    }
    if relied_on != {stated}:
        offered = ", ".join(sorted(relied_on)) or "nothing we could resolve"
        return False, (
            f"restricted; DEPENDENCIES.md states {named.licence} for it, and its metadata "
            f"now says {offered} -- re-read the licence and update the table"
        )

    if named.development_only and not development_only:
        return False, (
            f"restricted, and DEPENDENCIES.md allows it as '{named.scope}', but the runtime "
            "dependencies bring it in"
        )
    return True, f"restricted, and named in DEPENDENCIES.md as {named.licence}"


def _unclassified(
    package: LockedPackage,
    development_only: bool,
    finding: Finding,
    policy: Policy,
) -> tuple[bool, str]:
    text_row = policy.licence_text_exceptions.get(package.name)
    if text_row is not None:
        return _licence_text(package, finding, policy, text_row)
    named = policy.development_exceptions.get(package.name)
    if named is None:
        return False, "no licence this gate can classify; it does not pass unclassified"
    if not finding.family_only:
        return False, (
            "excepted in DEPENDENCIES.md as development-only, but that exception covers "
            f"metadata that names a licence family and no more; this says {finding.licence}"
        )
    if not development_only:
        return False, (
            "excepted in DEPENDENCIES.md as development-only, but the "
            "runtime dependencies bring it in"
        )
    if named.version != package.version:
        return False, (
            f"excepted in DEPENDENCIES.md at version {named.version}, but the lock pins "
            f"{package.version} -- read the licence of that version and update the table"
        )
    identifier = identifier_for(named.licence)
    if identifier is None or policy.category(identifier) is not Verdict.ALLOWED:
        return False, (
            f"excepted in DEPENDENCIES.md as {named.licence}, which is not on the allowed list"
        )
    return True, (
        f"excepted by name in DEPENDENCIES.md: {named.licence}, read by hand "
        f"in {named.version}, development-only"
    )


def _licence_text(
    package: LockedPackage,
    finding: Finding,
    policy: Policy,
    row: NamedException,
) -> tuple[bool, str]:
    """A row of "Excepted licence texts": a pasted licence text, read by a person.

    The narrowest loosening the gate has, so every condition is held, and none
    of them depends on where the package is used -- the row answers which
    licence a text is, and the answer has to be on the allowed list either way:

    - the metadata named nothing at all that resolves. Its one claim is the
      free-text field, and that field is not an identifier, not a family, and
      not an expression with anything in it we could read (a `WITH` included).
      Metadata that names something is judged by what it names;
    - the version is the one the text was read in;
    - the row's licence is on the allowed list;
    - the text's own first line names that same licence. That is what keeps
      the row a check rather than a switch: a release that swaps the text for
      another licence fails, whatever the row says;
    - and the tripwire of `Finding.text_names` finds no other licence in the
      text -- a forbidden or restricted one above all, but an allowed one too,
      since a second licence is a second question the row did not answer. It
      sees lines, punctuation-separated pieces and identifier-shaped words,
      not running prose; for that, the person who signed the row for this
      version, having read the whole text, is the safeguard.

    Forbidden and restricted findings never reach here; `decide` settles them
    first.
    """
    if (
        finding.verdict is not Verdict.UNKNOWN
        or finding.identifiers
        or finding.family_only
        or finding.title is None
    ):
        return False, (
            "excepted in DEPENDENCIES.md as a licence text, but that exception covers "
            "metadata whose one claim is a licence text headed by a title the gate can "
            f"read; this says {finding.licence}"
        )
    if row.version != package.version:
        return False, (
            f"excepted in DEPENDENCIES.md at version {row.version}, but the lock pins "
            f"{package.version} -- read the licence of that version and update the table"
        )
    stated = identifier_for(row.licence)
    if stated is None or policy.category(stated) is not Verdict.ALLOWED:
        return False, (
            f"excepted in DEPENDENCIES.md as {row.licence}, which is not on the allowed list"
        )
    if finding.title != stated:
        return False, (
            f"excepted in DEPENDENCIES.md as {row.licence}, but the licence text in its "
            f"metadata is headed {finding.title} -- re-read it and update the table"
        )
    others = sorted(finding.text_names - {stated})
    if others:
        kinds = sorted(
            {
                policy.category(identifier).value
                for identifier in others
                if policy.category(identifier) in (Verdict.FORBIDDEN, Verdict.RESTRICTED)
            }
        )
        worse = f" ({' and '.join(kinds)})" if kinds else ""
        return False, (
            f"excepted in DEPENDENCIES.md as {row.licence}, but the licence text in its "
            f"metadata also names {', '.join(others)}{worse} -- a person has to read "
            "what that text really grants"
        )
    return True, (
        f"excepted by name in DEPENDENCIES.md: {row.licence}, a licence text headed "
        f"{finding.title} and read by hand in {row.version}"
    )


def _project_name(pyproject_path: Path) -> str | None:
    """The name of the project the lockfile belongs to, if it can be read."""
    try:
        pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    name = pyproject.get("project", {}).get("name")
    return canonical_name(name) if isinstance(name, str) else None


def run(
    lock_path: Path,
    dependencies_path: Path,
    environment: Path | None,
    offline: bool,
    out=sys.stdout,
    composed: bool | None = None,
) -> int:
    """`composed` says whether the lock's project is composed from the backend, as an extra
    worker is; left out, any lock but the backend's own is taken to be."""
    policy = parse_policy(dependencies_path.read_text(encoding="utf-8"))
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    beside: set[tuple[str, str]] = set()
    if composed is None:
        composed = lock_path.parent != LOCK.parent
    if composed:
        # An extra worker's lock: the backend it is composed from is ours too, and so is each
        # project beside it under extra-workers/ that it may be composed from.
        projects = [LOCK.parent, *(path.parent for path in EXTRA_WORKER_PROJECTS)]
        for folder in projects:
            name = _project_name(folder / "pyproject.toml")
            if name is not None and folder != lock_path.parent:
                path = folder.relative_to(lock_path.parent, walk_up=True).as_posix()
                beside.add((name, path))
    packages, development_only = read_lock(
        lock, _project_name(lock_path.parent / "pyproject.toml"), beside
    )
    source = MetadataSource(environment if environment and environment.exists() else None, offline)

    outcomes: list[Outcome] = []
    for package in packages:
        scope = "development" if package.name in development_only else "runtime"
        if package.local:
            outcomes.append(
                Outcome(
                    package,
                    scope,
                    Finding(Verdict.ALLOWED, "Apache-2.0"),
                    "this repository",
                    True,
                    "the project itself",
                )
            )
            continue
        lookup = source.get(package)
        if lookup.metadata is None:
            finding = Finding(Verdict.UNKNOWN, lookup.problem)
            origin = "nowhere"
        else:
            finding = assess(lookup.metadata, policy)
            origin = lookup.metadata.origin
        passed, reason = decide(package, scope == "development", finding, policy)
        unchecked = lookup.metadata is None and lookup.unavailable
        outcomes.append(Outcome(package, scope, finding, origin, passed, reason, unchecked))

    return _report(outcomes, policy, out)


def _report(outcomes: Sequence[Outcome], policy: Policy, out) -> int:
    width = max((len(outcome.package.name) for outcome in outcomes), default=0)
    print(f"Licence gate over {len(outcomes)} locked packages\n", file=out)
    for outcome in outcomes:
        mark = "ok  " if outcome.passed else "FAIL"
        print(
            f"  {mark} {outcome.package.name:<{width}}  "
            f"{outcome.finding.licence}  [{outcome.scope}, from {outcome.origin}]",
            file=out,
        )
        # Why, whenever the licence alone did not settle it.
        if not outcome.passed or outcome.finding.verdict is not Verdict.ALLOWED:
            print(f"       {outcome.reason}", file=out)

    locked = {outcome.package.name for outcome in outcomes}
    stale = sorted((set(policy.restricted_packages) | set(policy.development_exceptions)) - locked)
    for name in stale:
        print(f"\nnote: DEPENDENCIES.md excepts {name}, which is no longer locked", file=out)
    # A licence-text row may name a package ahead of its adoption, so its
    # absence from the lock is said as that rather than as something that left.
    for name in sorted(set(policy.licence_text_exceptions) - locked):
        print(
            f"\nnote: DEPENDENCIES.md excepts {name}, which is not locked "
            "(a row may name a package ahead of its adoption)",
            file=out,
        )

    failed = [outcome for outcome in outcomes if not outcome.passed]
    unchecked = [outcome for outcome in failed if outcome.unchecked]
    if unchecked:
        # Nothing was read about these, so nothing about them passed; but the
        # build is red because the gate could not work, not because a licence
        # is wrong, and the exit code says which.
        print(
            f"\nThe gate could not read the licence of {len(unchecked)} package(s): "
            + ", ".join(outcome.package.name for outcome in unchecked),
            file=out,
        )
    if failed and len(failed) > len(unchecked):
        print(
            f"\n{len(failed) - len(unchecked)} package(s) fail the policy of DEPENDENCIES.md: "
            + ", ".join(outcome.package.name for outcome in failed if not outcome.unchecked),
            file=out,
        )
        return 1
    if unchecked:
        return 2
    print("\nEvery locked package passes the policy of DEPENDENCIES.md.", file=out)
    return 0


ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "backend" / "uv.lock"
"""The lockfile whose every package is checked."""
DEPENDENCIES = ROOT / "DEPENDENCIES.md"
"""The document that states the policy and names the exceptions."""
ENVIRONMENT = ROOT / "backend" / ".venv"
"""A synced virtual environment to read metadata from first."""
EXTRA_WORKERS = {
    lock.parent.name: (lock, lock.parent / ".venv")
    for lock in sorted((ROOT / "extra-workers").glob("*/uv.lock"))
}
"""Each extra worker with a lock, by its directory name: its lock and its environment."""
EXTRA_WORKER_PROJECTS = sorted((ROOT / "extra-workers").glob("*/pyproject.toml"))
"""Each project under extra-workers/, a worker or a library the workers share: what an extra
worker's lock may hold, editable, as ours."""
MCP_SERVERS = {
    lock.parent.name: (lock, lock.parent / ".venv")
    for lock in sorted((ROOT / "mcp").glob("*/uv.lock"))
}
"""Each MCP server of this repository with a lock, by its directory name, as EXTRA_WORKERS.
An MCP server stands alone: nothing of the backend is in its lock, nor counts as ours there."""


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--offline",
        action="store_true",
        help="do not ask PyPI about packages the environment does not have",
    )
    which = parser.add_mutually_exclusive_group()
    which.add_argument(
        "--extra-worker",
        choices=sorted(EXTRA_WORKERS),
        help="check this extra worker's lock and environment; the backend's when not given",
    )
    which.add_argument(
        "--mcp-server",
        choices=sorted(MCP_SERVERS),
        help="check this MCP server's lock and environment (mcp/<name>/)",
    )
    arguments = parser.parse_args(argv)
    try:
        # The paths come from the tables, never from the argument itself.
        if arguments.mcp_server is not None:
            lock, environment = MCP_SERVERS[arguments.mcp_server]
            return run(lock, DEPENDENCIES, environment, arguments.offline, composed=False)
        lock, environment = EXTRA_WORKERS.get(arguments.extra_worker, (LOCK, ENVIRONMENT))
        return run(lock, DEPENDENCIES, environment, arguments.offline)
    except (ValueError, TypeError, KeyError, AttributeError, OSError) as problem:
        # Nothing was checked, so nothing passed. Exit 2 rather than 1, to
        # separate "this dependency fails the policy" from "the gate could not
        # do its work".
        print(f"the licence gate could not run: {problem}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The classification logic of the licence gate (scripts/licence_gate.py).

The gate is the only thing standing between a forbidden dependency and the
tree, so what it decides is tested here rather than trusted. The last tests
read DEPENDENCIES.md itself, so that the document and the gate cannot drift
apart unnoticed.
"""

import hashlib
import io
import json
import re
import tomllib
from pathlib import Path

import pytest
from licence_gate import (
    _FORBIDDEN_IDENTIFIER,
    MCP_SERVERS,
    SUPPORTED_LOCK_VERSIONS,
    Finding,
    LockedPackage,
    Metadata,
    MetadataSource,
    NamedException,
    Policy,
    Verdict,
    _metadata_from_message,
    _report,
    assess,
    decide,
    evaluate_expression,
    identifier_for,
    identifier_from_classifier,
    main,
    parse_policy,
    read_lock,
)

ROOT = Path(__file__).resolve().parents[3]

POLICY_DOCUMENT = """# A policy

### Allowed

Apache-2.0, MIT, BSD-2-Clause, BSD-3-Clause, PSF-2.0, CNRI-Python.

Prose that follows the list and must not be read as part of it.

### Restricted

MPL-2.0, CDDL-1.x.

## Restricted dependencies in use

| package | licence | scope | why it is acceptable |
|---|---|---|---|
| `pathspec` | MPL-2.0 | development only | unmodified, not shipped |

## Excepted development-only dependencies

| package | version | licence | why it is acceptable |
|---|---|---|---|
| `colorama` | 0.4.6 | BSD-3-Clause | checked by hand |

## Excepted licence texts

| package | version | licence | why it is acceptable |
|---|---|---|---|
| `tiktoken` | 0.14.0 | MIT | the licence text, read by hand |

## Known exclusions

| package | licence | consequence |
|---|---|---|
| `psycopg`, `psycopg-pool` | LGPL-3.0-only | not used |
"""


@pytest.fixture
def policy():
    return parse_policy(POLICY_DOCUMENT)


# ---------------------------------------------------------------------------
# Reading the policy
# ---------------------------------------------------------------------------


def test_the_categories_are_read_from_the_document(policy):
    assert "apache-2.0" in policy.allowed
    assert "psf-2.0" in policy.allowed
    assert "mpl-2.0" in policy.restricted
    # A "-1.x" wildcard stands for the versions it covers.
    assert {"cddl-1.0", "cddl-1.1"} <= policy.restricted
    # The prose under the list is not part of it.
    assert not any(" " in identifier for identifier in policy.allowed)


def test_only_the_exception_tables_are_read_as_exceptions(policy):
    assert policy.restricted_packages == {
        "pathspec": NamedException("MPL-2.0", scope="development only")
    }
    assert policy.development_exceptions == {
        "colorama": NamedException("BSD-3-Clause", "0.4.6", "development only")
    }
    # No scope: a licence-text row says which licence a text is, not where the
    # package is used, and it is not development-only for being in a table
    # with a version column.
    assert policy.licence_text_exceptions == {"tiktoken": NamedException("MIT", "0.14.0")}
    # The known exclusions are the opposite of an exception.
    assert "psycopg" not in policy.restricted_packages
    assert "psycopg" not in policy.development_exceptions
    assert "psycopg" not in policy.licence_text_exceptions


def test_the_columns_are_found_by_their_heading_not_their_place(policy):
    moved = POLICY_DOCUMENT.replace(
        "| package | version | licence | why it is acceptable |\n|---|---|---|---|\n"
        "| `colorama` | 0.4.6 | BSD-3-Clause | checked by hand |",
        "| licence | package | why it is acceptable | version |\n|---|---|---|---|\n"
        "| BSD-3-Clause | `colorama` | checked by hand | 0.4.6 |",
    )
    assert moved != POLICY_DOCUMENT
    assert parse_policy(moved).development_exceptions == policy.development_exceptions


def test_a_development_exception_without_a_version_is_an_error():
    # The first table with a version column is the development-only one.
    document = POLICY_DOCUMENT.replace("| version | licence |", "| licence |", 1).replace(
        "| `colorama` | 0.4.6 | BSD-3-Clause |", "| `colorama` | BSD-3-Clause |"
    )
    assert document != POLICY_DOCUMENT
    with pytest.raises(ValueError, match="version"):
        parse_policy(document)


def test_a_licence_text_exception_without_a_version_is_an_error():
    document = POLICY_DOCUMENT.replace(
        "| package | version | licence | why it is acceptable |\n|---|---|---|---|\n"
        "| `tiktoken` | 0.14.0 | MIT |",
        "| package | licence | why it is acceptable |\n|---|---|---|\n| `tiktoken` | MIT |",
    )
    assert document != POLICY_DOCUMENT
    with pytest.raises(ValueError, match="version"):
        parse_policy(document)


def test_a_package_is_excepted_in_one_table_not_two():
    # The two tables hold a package to different conditions; a row in each
    # would leave which of them applies to the order of the code.
    document = POLICY_DOCUMENT.replace(
        "| `tiktoken` | 0.14.0 | MIT |", "| `tiktoken`, `colorama` | 0.14.0 | MIT |"
    )
    assert document != POLICY_DOCUMENT
    with pytest.raises(ValueError, match="colorama"):
        parse_policy(document)


@pytest.mark.parametrize("category", ["Allowed", "Restricted"])
def test_a_category_that_lists_a_forbidden_licence_is_refused(category):
    document = POLICY_DOCUMENT.replace(f"### {category}\n\n", f"### {category}\n\nGPL-3.0, ", 1)
    assert document != POLICY_DOCUMENT
    with pytest.raises(ValueError, match="GPL-3.0"):
        parse_policy(document)


def test_forbidden_is_decided_before_the_lists_are_consulted(policy):
    # Not only when the document is parsed: a policy built any other way is
    # held to the same order.
    doctored = Policy(
        allowed=policy.allowed | {"gpl-3.0"},
        restricted=policy.restricted | {"lgpl-3.0"},
        restricted_packages={},
        development_exceptions={},
    )
    assert doctored.category("GPL-3.0") is Verdict.FORBIDDEN
    assert doctored.category("LGPL-3.0") is Verdict.FORBIDDEN
    assert doctored.category("MIT") is Verdict.ALLOWED


def test_a_missing_section_is_an_error():
    with pytest.raises(ValueError):
        parse_policy("# A policy\n\n### Allowed\n\nMIT.\n")


# ---------------------------------------------------------------------------
# Spelling licences
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "identifier"),
    [
        ("MIT", "MIT"),
        ("MIT License", "MIT"),
        ("  the   MIT   license  ", "MIT"),
        ('"MIT"', "MIT"),
        ("MIT-0", "MIT-0"),
        ("MIT No Attribution", "MIT-0"),
        ("Apache-2.0", "Apache-2.0"),
        ("Apache License, Version 2.0", "Apache-2.0"),
        ("BSD 2-Clause License", "BSD-2-Clause"),
        ("BSD-3-Clause", "BSD-3-Clause"),
        ("Mozilla Public License 2.0 (MPL 2.0)", "MPL-2.0"),
        ("LGPL-3.0-only", "LGPL-3.0"),
        ("GPL-2.0+", "GPL-2.0"),
    ],
)
def test_messy_spellings_resolve_to_one_identifier(text, identifier):
    assert identifier_for(text) == identifier


@pytest.mark.parametrize(
    "text",
    [
        "",
        "BSD",
        "BSD License",
        "Apache Software License",
        "See the LICENSE file",
        "Copyright (c) 2026, everyone. Redistribution and use in source ...",
    ],
)
def test_a_family_or_a_muddle_resolves_to_nothing(text):
    assert identifier_for(text) is None


def test_the_unversioned_psf_name_is_a_family_like_any_other():
    # PSF-2.0 and Python-2.0 both come from the same foundation, and the
    # classifier names neither of them; the gate guesses for nobody.
    assert identifier_for("Python Software Foundation License") is None
    classifier = "License :: OSI Approved :: Python Software Foundation License"
    assert identifier_from_classifier(classifier) is None
    assert identifier_for("PSF-2.0") == "PSF-2.0"
    assert identifier_for("Python-2.0") == "Python-2.0"


@pytest.mark.parametrize(
    ("text", "identifier"),
    [
        ("CNRI-Python", "CNRI-Python"),
        ("cnri-python", "CNRI-Python"),
        ("CNRI Python License", "CNRI-Python"),
        # A different SPDX identifier, on no list: it is not spelt, so it
        # stays unknown rather than borrowing CNRI-Python's place.
        ("CNRI-Python-GPL-Compatible", None),
    ],
)
def test_cnri_python_is_an_identifier_of_its_own(text, identifier):
    assert identifier_for(text) == identifier


def test_cnri_python_is_not_the_psf_licence_nor_python_2(policy):
    assert len({identifier_for(name) for name in ("CNRI-Python", "PSF-2.0", "Python-2.0")}) == 3
    assert policy.category("CNRI-Python") is Verdict.ALLOWED
    # Allowing one of the three allows neither of the others.
    assert policy.category("Python-2.0") is Verdict.UNKNOWN


@pytest.mark.parametrize(
    ("classifier", "identifier"),
    [
        ("License :: OSI Approved :: MIT License", "MIT"),
        ("License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)", "MPL-2.0"),
        ("License :: OSI Approved :: GNU General Public License v3 (GPLv3)", "GPL-3.0"),
        ("License :: OSI Approved :: GNU Library or Lesser General Public License (LGPL)", "LGPL"),
        ("License :: Other/Proprietary License", "proprietary"),
        ("License :: OSI Approved :: BSD License", None),
        ("License :: OSI Approved", None),
        ("Programming Language :: Python :: 3.12", None),
        ("Development Status :: 4 - Beta", None),
    ],
)
def test_classifiers_are_read_only_when_they_name_one_licence(classifier, identifier):
    assert identifier_from_classifier(classifier) == identifier


# ---------------------------------------------------------------------------
# Licence expressions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "verdict"),
    [
        ("MIT", Verdict.ALLOWED),
        ("MPL-2.0", Verdict.RESTRICTED),
        ("LGPL-3.0-only", Verdict.FORBIDDEN),
        ("AGPL-3.0-or-later", Verdict.FORBIDDEN),
        ("Elastic-2.0", Verdict.FORBIDDEN),
        ("Zlib", Verdict.UNKNOWN),  # allowed by the real policy, not by this one
        # A choice: we may take the alternative that suits us.
        ("Apache-2.0 OR BSD-2-Clause", Verdict.ALLOWED),
        ("MIT OR GPL-3.0-only", Verdict.ALLOWED),
        ("MPL-2.0 OR GPL-2.0-only", Verdict.RESTRICTED),
        ("GPL-2.0-only OR GPL-3.0-only", Verdict.FORBIDDEN),
        # A conjunction: every part binds at once.
        ("MPL-2.0 AND MIT", Verdict.RESTRICTED),
        ("MIT AND GPL-3.0-only", Verdict.FORBIDDEN),
        ("MIT AND BSD-3-Clause", Verdict.ALLOWED),
        # regex's own expression: two allowed licences, both binding.
        ("Apache-2.0 AND CNRI-Python", Verdict.ALLOWED),
        ("CNRI-Python AND MPL-2.0", Verdict.RESTRICTED),
        ("(MIT OR GPL-3.0-only) AND Apache-2.0", Verdict.ALLOWED),
        ("MIT AND (GPL-3.0-only OR AGPL-3.0-only)", Verdict.FORBIDDEN),
        # AND binds tighter than OR, as SPDX says.
        ("MIT AND GPL-3.0-only OR Apache-2.0", Verdict.ALLOWED),
        # An exception rewrites the licence; nothing here can judge the result.
        ("GPL-2.0-only WITH Classpath-exception-2.0", Verdict.UNKNOWN),
        ("MIT OR (GPL-2.0-only WITH Classpath-exception-2.0)", Verdict.ALLOWED),
        # Malformed, and unknown for it.
        ("MIT AND", Verdict.UNKNOWN),
        ("(MIT OR Apache-2.0", Verdict.UNKNOWN),
        ("", Verdict.UNKNOWN),
    ],
)
def test_expressions_are_evaluated_in_our_favour_only_where_we_have_the_choice(
    expression, verdict, policy
):
    assert evaluate_expression(expression, policy)[0] is verdict


def test_lowercase_or_inside_a_licence_name_is_not_a_choice(policy):
    # The SPDX operators are uppercase, which is what makes them safe to split
    # on: this classifier tail is one name, not "GNU Library" or something else.
    name = "GNU Library or Lesser General Public License (LGPL)"
    assert evaluate_expression(name, policy) == (Verdict.FORBIDDEN, frozenset({"LGPL"}))


def test_an_expression_that_names_no_licence_says_so(policy):
    assert evaluate_expression("See the LICENSE file", policy) == (Verdict.UNKNOWN, frozenset())
    assert evaluate_expression("MIT OR Whatever-1.0", policy) == (
        Verdict.ALLOWED,
        frozenset({"MIT"}),
    )


# ---------------------------------------------------------------------------
# Judging a package's metadata
# ---------------------------------------------------------------------------


def test_a_licence_expression_is_taken_at_its_word(policy):
    metadata = Metadata(expression="Apache-2.0 OR BSD-2-Clause")
    assert assess(metadata, policy) == Finding(
        Verdict.ALLOWED,
        "Apache-2.0 OR BSD-2-Clause",
        identifiers=frozenset({"Apache-2.0", "BSD-2-Clause"}),
    )


def test_a_forbidden_classifier_sinks_a_package_that_claims_otherwise(policy):
    metadata = Metadata(
        expression="MIT",
        classifiers=("License :: OSI Approved :: GNU General Public License v3 (GPLv3)",),
    )
    assert assess(metadata, policy).verdict is Verdict.FORBIDDEN


def test_the_free_text_field_settles_what_a_classifier_leaves_open(policy):
    # grimp, and many another package of its age: the classifier names the
    # family, the free-text field names the licence.
    metadata = Metadata(
        declared="BSD 2-Clause License",
        classifiers=("License :: OSI Approved :: BSD License",),
    )
    assert assess(metadata, policy) == Finding(
        Verdict.ALLOWED,
        "BSD 2-Clause License",
        identifiers=frozenset({"BSD-2-Clause"}),
    )


def test_a_free_text_field_that_contradicts_the_expression_is_not_ignored(policy):
    # PEP 639 says the expression is authoritative, but a package claiming MIT
    # in one field and the GPL in another has not been read by anybody. The
    # report shows both, because settling it means looking at the package.
    metadata = Metadata(expression="MIT", declared="GPL-3.0-only")
    finding = assess(metadata, policy)
    assert finding.verdict is Verdict.FORBIDDEN
    assert finding.licence == "MIT, GPL-3.0-only"


def test_a_free_text_field_that_makes_it_worse_but_not_forbidden_counts_too(policy):
    finding = assess(Metadata(expression="MIT", declared="MPL-2.0"), policy)
    assert finding.verdict is Verdict.RESTRICTED
    assert finding.licence == "MIT, MPL-2.0"


def test_a_proprietary_claim_beside_a_permissive_expression_is_not_ignored(policy):
    metadata = Metadata(expression="MIT", declared="Other/Proprietary License")
    assert assess(metadata, policy).verdict is Verdict.FORBIDDEN


LICENCE_BODY = """Copyright (c) 2026 Somebody

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell ...
"""


def test_a_licence_body_in_the_free_text_field_does_not_sink_a_good_expression(policy):
    # Packages routinely paste the whole licence where its name belongs. That
    # names nothing, and nothing is what it counts for.
    finding = assess(Metadata(expression="MIT", declared=LICENCE_BODY), policy)
    assert finding.verdict is Verdict.ALLOWED
    assert finding.licence == "MIT"


def test_an_expression_we_cannot_read_still_sinks_the_package(policy):
    # The one field that is SPDX by definition. If we cannot read that, we
    # have not understood the package, whatever its classifiers say.
    metadata = Metadata(
        expression="Frobnicate-1.0",
        classifiers=("License :: OSI Approved :: MIT License",),
    )
    assert assess(metadata, policy).verdict is Verdict.UNKNOWN


def test_the_worst_of_what_the_metadata_names_wins(policy):
    metadata = Metadata(
        declared="MIT",
        classifiers=(
            "License :: OSI Approved :: MIT License",
            "License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)",
        ),
    )
    assert assess(metadata, policy).verdict is Verdict.RESTRICTED


def test_metadata_that_names_only_a_family_does_not_pass(policy):
    metadata = Metadata(classifiers=("License :: OSI Approved :: BSD License",))
    finding = assess(metadata, policy)
    assert finding.verdict is Verdict.UNKNOWN
    assert "family" in finding.licence


def test_metadata_with_no_licence_at_all_is_forbidden(policy):
    # DEPENDENCIES.md lists "no licence at all" among the forbidden: a package
    # that says nothing is all rights reserved, not permissive by default.
    finding = assess(Metadata(classifiers=("Development Status :: 5 - Production/Stable",)), policy)
    assert finding.verdict is Verdict.FORBIDDEN
    assert finding.licence == "no licence at all"
    assert finding.family_only is False


@pytest.mark.parametrize(
    "classifier",
    [
        "License :: Free for non-commercial use",
        "License :: Free For Educational Use",
        "License :: Free To Use But Restricted",
        "License :: Other/Proprietary License",
    ],
)
def test_a_no_commercial_use_term_is_forbidden(policy, classifier):
    assert assess(Metadata(classifiers=(classifier,)), policy).verdict is Verdict.FORBIDDEN


@pytest.mark.parametrize(
    "text",
    ["GNU GPL", "GPL", "GPLv3", "LGPL", "GNU General Public License", "AGPL", "GNU LGPL"],
)
def test_the_copyleft_families_are_forbidden_however_they_are_spelled(policy, text):
    assert assess(Metadata(declared=text), policy).verdict is Verdict.FORBIDDEN


def test_one_readable_claim_among_families_is_a_contradiction_not_vagueness(policy):
    # declared "GNU GPL" and a classifier that says BSD: the exception for a
    # package whose metadata only names a family must not cover this.
    finding = assess(
        Metadata(declared="GNU GPL", classifiers=("License :: OSI Approved :: BSD License",)),
        policy,
    )
    assert finding.verdict is Verdict.FORBIDDEN
    assert finding.family_only is False


def test_an_unreadable_claim_beside_a_family_is_not_vagueness_either(policy):
    finding = assess(
        Metadata(
            declared="Frobnicate Public Licence",
            classifiers=("License :: OSI Approved :: BSD License",),
        ),
        policy,
    )
    assert finding.verdict is Verdict.UNKNOWN
    assert finding.family_only is False
    passed, reason = decide(_package("colorama", "0.4.6"), True, finding, policy)
    assert passed is False
    assert "family" in reason


def test_families_all_the_way_down_is_vagueness_a_person_may_settle(policy):
    finding = assess(
        Metadata(
            declared="BSD",
            classifiers=("License :: OSI Approved :: BSD License",),
        ),
        policy,
    )
    assert finding.verdict is Verdict.UNKNOWN
    assert finding.family_only is True


def test_only_a_permissive_family_is_left_open_for_a_person_to_settle(policy):
    # "Creative Commons" covers CC-BY-NC, which is forbidden, so it is not the
    # kind of vagueness an exception may cover.
    finding = assess(Metadata(declared="Creative Commons"), policy)
    assert finding.verdict is Verdict.UNKNOWN
    assert finding.family_only is False


def test_unreadable_free_text_does_not_erase_a_good_classifier(policy):
    metadata = Metadata(
        declared="See the LICENSE file",
        classifiers=("License :: OSI Approved :: MIT License",),
    )
    assert assess(metadata, policy).verdict is Verdict.ALLOWED


# ---------------------------------------------------------------------------
# Applying the exceptions
# ---------------------------------------------------------------------------


def _package(name, version="1.0"):
    return LockedPackage(name=name, version=version)


def _family_only(text="a licence family only (BSD License)"):
    return Finding(Verdict.UNKNOWN, text, family_only=True)


@pytest.mark.parametrize("development_only", [True, False])
def test_an_allowed_licence_needs_no_exception(policy, development_only):
    finding = Finding(Verdict.ALLOWED, "MIT")
    assert decide(_package("pytest"), development_only, finding, policy)[0] is True


def _restricted_finding(*identifiers, label=None):
    return Finding(
        Verdict.RESTRICTED,
        label or ", ".join(identifiers),
        identifiers=frozenset(identifiers),
    )


def test_a_restricted_licence_passes_only_when_the_document_names_it(policy):
    finding = _restricted_finding("MPL-2.0")
    assert decide(_package("pathspec"), True, finding, policy)[0] is True
    assert decide(_package("certifi"), True, finding, policy)[0] is False


def test_a_restricted_dependency_that_changes_licence_fails(policy):
    # The table says pathspec is MPL-2.0. If a release turns it into CDDL-1.0 --
    # restricted too, and just as listed a category -- the name is not enough:
    # somebody has to look at it again.
    finding = assess(Metadata(expression="CDDL-1.0"), policy)
    passed, reason = decide(_package("pathspec"), True, finding, policy)
    assert passed is False
    assert "MPL-2.0" in reason
    assert "CDDL-1.0" in reason


def test_the_stated_licence_is_compared_as_a_licence_not_as_a_string(policy):
    # The metadata says the same thing twice, in two spellings, so the
    # display string is "MPL 2.0, MPL-2.0" -- and one licence was named.
    finding = assess(
        Metadata(
            declared="MPL 2.0",
            classifiers=("License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)",),
        ),
        policy,
    )
    assert finding.licence == "MPL 2.0, MPL-2.0"
    assert decide(_package("pathspec"), True, finding, policy)[0] is True


def test_a_choice_between_two_restricted_licences_is_not_one_the_table_made(policy):
    # "MPL-2.0 OR CDDL-1.0": both restricted, so the verdict is restricted,
    # but which one we rely on is a decision, and the row has room for one.
    finding = assess(Metadata(expression="MPL-2.0 OR CDDL-1.0"), policy)
    assert finding.verdict is Verdict.RESTRICTED
    passed, reason = decide(_package("pathspec"), True, finding, policy)
    assert passed is False
    assert "CDDL-1.0" in reason


def test_a_restricted_row_that_names_a_licence_that_is_not_restricted_fails(policy):
    document = POLICY_DOCUMENT.replace("| `pathspec` | MPL-2.0 |", "| `pathspec` | MIT |")
    assert document != POLICY_DOCUMENT
    passed, reason = decide(
        _package("pathspec"), True, _restricted_finding("MPL-2.0"), parse_policy(document)
    )
    assert passed is False
    assert "not a restricted licence" in reason


def test_a_restricted_row_that_says_development_only_means_it(policy):
    # pathspec is there because black brings it. If it ever turned up in the
    # runtime closure, the reason the row gives would no longer hold.
    passed, reason = decide(_package("pathspec"), False, _restricted_finding("MPL-2.0"), policy)
    assert passed is False
    assert "runtime" in reason


@pytest.mark.parametrize("name", ["psycopg", "pathspec", "colorama"])
def test_no_exception_reaches_a_forbidden_licence(policy, name):
    finding = Finding(Verdict.FORBIDDEN, "LGPL-3.0-only")
    passed, reason = decide(_package(name), True, finding, policy)
    assert passed is False
    assert "forbidden" in reason


def test_no_exception_reaches_a_package_with_no_licence_at_all(policy):
    finding = Finding(Verdict.FORBIDDEN, "no licence at all")
    passed, reason = decide(_package("colorama", "0.4.6"), True, finding, policy)
    assert passed is False
    assert "forbidden" in reason


def test_no_exception_reaches_a_no_commercial_use_term(policy):
    finding = assess(Metadata(classifiers=("License :: Free for non-commercial use",)), policy)
    passed, reason = decide(_package("colorama", "0.4.6"), True, finding, policy)
    assert passed is False
    assert "forbidden" in reason


def test_a_development_exception_covers_a_family_the_gate_cannot_resolve(policy):
    passed, reason = decide(_package("colorama", "0.4.6"), True, _family_only(), policy)
    assert passed is True
    assert "BSD-3-Clause" in reason
    assert "0.4.6" in reason


def test_a_development_exception_does_not_cover_unreadable_metadata(policy):
    finding = Finding(Verdict.UNKNOWN, "nothing we can read (see the LICENSE file)")
    passed, reason = decide(_package("colorama", "0.4.6"), True, finding, policy)
    assert passed is False
    assert "family" in reason


def test_a_development_exception_does_not_cover_a_runtime_dependency(policy):
    passed, reason = decide(_package("colorama", "0.4.6"), False, _family_only(), policy)
    assert passed is False
    assert "runtime" in reason


def test_a_development_exception_holds_for_the_version_it_names(policy):
    passed, reason = decide(_package("colorama", "0.4.7"), True, _family_only(), policy)
    assert passed is False
    assert "0.4.6" in reason
    assert "0.4.7" in reason


def test_a_development_exception_cannot_state_a_licence_that_is_not_allowed(policy):
    document = POLICY_DOCUMENT.replace("| 0.4.6 | BSD-3-Clause |", "| 0.4.6 | GPL-3.0-only |")
    assert document != POLICY_DOCUMENT
    passed, reason = decide(
        _package("colorama", "0.4.6"), True, _family_only(), parse_policy(document)
    )
    assert passed is False
    assert "allowed list" in reason


def test_an_unclassified_licence_does_not_pass(policy):
    finding = Finding(Verdict.UNKNOWN, "nothing we can read")
    assert decide(_package("mystery"), True, finding, policy)[0] is False


@pytest.mark.parametrize("development_only", [True, False])
def test_regex_passes_on_its_own_expression_with_no_row(policy, development_only):
    finding = assess(Metadata(expression="Apache-2.0 AND CNRI-Python"), policy)
    assert finding == Finding(
        Verdict.ALLOWED,
        "Apache-2.0 AND CNRI-Python",
        identifiers=frozenset({"Apache-2.0", "CNRI-Python"}),
    )
    passed, reason = decide(_package("regex", "2026.9.1"), development_only, finding, policy)
    assert passed is True
    assert reason == "on the allowed list"


# ---------------------------------------------------------------------------
# A licence text excepted by name
# ---------------------------------------------------------------------------

# tiktoken 0.14.0's `License` field, as PyPI publishes it: the whole MIT
# licence, with no License-Expression and no `License ::` classifier beside
# it. Its disclaimer is shouted, so it is full of uppercase OR, AND and WITH,
# which is exactly what the expression reader splits on.
TIKTOKEN_LICENCE = """MIT License

Copyright (c) 2022 OpenAI, Shantanu Jain

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
assert " OR " in TIKTOKEN_LICENCE
assert " WITH " in TIKTOKEN_LICENCE


def _tiktoken(text=TIKTOKEN_LICENCE, policy=None, **metadata):
    return assess(Metadata(declared=text, **metadata), policy or parse_policy(POLICY_DOCUMENT))


def test_a_licence_text_alone_is_read_as_nothing_but_its_title_is_kept(policy):
    finding = _tiktoken(policy=policy)
    assert finding.verdict is Verdict.UNKNOWN
    assert finding.identifiers == frozenset()
    assert finding.family_only is False
    assert finding.title == "MIT"
    # The body is prose: nothing in it names a licence but the title does.
    assert finding.text_names == frozenset({"MIT"})
    assert finding.licence.startswith("nothing we can read (MIT License Copyright")


@pytest.mark.parametrize("development_only", [False, True])
def test_a_licence_text_row_covers_the_text_it_was_written_about(policy, development_only):
    # A runtime dependency as much as a development-only one: the row says
    # which licence the text is, not where the package is used.
    passed, reason = decide(
        _package("tiktoken", "0.14.0"), development_only, _tiktoken(policy=policy), policy
    )
    assert passed is True
    assert "MIT" in reason
    assert "0.14.0" in reason


def test_a_licence_text_row_holds_for_the_version_it_names(policy):
    passed, reason = decide(_package("tiktoken", "0.15.0"), False, _tiktoken(policy=policy), policy)
    assert passed is False
    assert "0.14.0" in reason
    assert "0.15.0" in reason


def test_a_licence_text_headed_by_another_licence_fails_the_row(policy):
    # The release swapped its text for the three-clause BSD licence, also on
    # the allowed list. The row says MIT, and nobody has read this one.
    text = TIKTOKEN_LICENCE.replace("MIT License", "BSD 3-Clause License", 1)
    finding = _tiktoken(text, policy)
    assert finding.title == "BSD-3-Clause"
    passed, reason = decide(_package("tiktoken", "0.14.0"), False, finding, policy)
    assert passed is False
    assert "BSD-3-Clause" in reason
    assert "MIT" in reason


def test_a_licence_text_with_no_title_the_gate_can_read_fails_the_row(policy):
    finding = _tiktoken(LICENCE_BODY, policy)
    assert finding.title is None
    passed, reason = decide(_package("tiktoken", "0.14.0"), False, finding, policy)
    assert passed is False
    assert "licence text" in reason


@pytest.mark.parametrize(
    ("row", "title"),
    [
        # Permissive, but not on this policy's allowed list.
        ("Zlib", "zlib License"),
        # Forbidden: a row cannot wave through a text it names correctly.
        ("GPL", "GNU General Public License"),
    ],
)
def test_a_licence_text_row_must_state_an_allowed_licence(row, title):
    policy = parse_policy(
        POLICY_DOCUMENT.replace("| `tiktoken` | 0.14.0 | MIT |", f"| `tiktoken` | 0.14.0 | {row} |")
    )
    finding = _tiktoken(TIKTOKEN_LICENCE.replace("MIT License", title, 1), policy)
    assert finding.title == identifier_for(row)
    passed, reason = decide(_package("tiktoken", "0.14.0"), False, finding, policy)
    assert passed is False
    assert "allowed list" in reason


@pytest.mark.parametrize(
    "metadata",
    [
        # Metadata that names a family: the development-only table's case,
        # not this one's.
        Metadata(declared="BSD", classifiers=("License :: OSI Approved :: BSD License",)),
        # The text beside a classifier. The classifier names a mere family,
        # but it is a second claim, and the row was not written about it.
        Metadata(
            declared=TIKTOKEN_LICENCE,
            classifiers=("License :: OSI Approved :: BSD License",),
        ),
        # An expression that names an identifier nothing here knows: the
        # SPDX field, unreadable, beside a text that is headed MIT.
        Metadata(expression="Frobnicate-1.0", declared=TIKTOKEN_LICENCE),
        # Something resolved, and something did not: that is not a licence
        # text, it is a claim only half understood.
        Metadata(expression="MIT AND Frobnicate-1.0"),
        # An exception rewrites the licence it is attached to.
        Metadata(declared="MIT License WITH Frobnicate-exception"),
        Metadata(expression="MIT WITH Frobnicate-exception"),
    ],
)
def test_a_licence_text_row_covers_nothing_but_a_licence_text(policy, metadata):
    finding = assess(metadata, policy)
    assert finding.verdict is Verdict.UNKNOWN
    passed, reason = decide(_package("tiktoken", "0.14.0"), False, finding, policy)
    assert passed is False
    assert "licence text" in reason


@pytest.mark.parametrize(
    "metadata",
    [
        # States nothing at all.
        Metadata(),
        Metadata(classifiers=("Development Status :: 5 - Production/Stable",)),
        # A licence text headed MIT beside a forbidden claim the gate can read.
        Metadata(
            declared=TIKTOKEN_LICENCE,
            classifiers=("License :: OSI Approved :: GNU General Public License v3 (GPLv3)",),
        ),
        Metadata(expression="GPL-3.0-only", declared=TIKTOKEN_LICENCE),
    ],
)
def test_a_licence_text_row_never_reaches_a_forbidden_finding(policy, metadata):
    finding = assess(metadata, policy)
    assert finding.verdict is Verdict.FORBIDDEN
    passed, reason = decide(_package("tiktoken", "0.14.0"), False, finding, policy)
    assert passed is False
    assert "forbidden" in reason


def test_a_licence_text_row_never_reaches_a_restricted_finding(policy):
    finding = assess(Metadata(expression="MPL-2.0", declared=TIKTOKEN_LICENCE), policy)
    assert finding.verdict is Verdict.RESTRICTED
    passed, reason = decide(_package("tiktoken", "0.14.0"), False, finding, policy)
    assert passed is False
    assert "Restricted dependencies in use" in reason


def test_the_development_only_table_does_not_cover_a_licence_text(policy):
    # colorama's row is for a family; a text headed MIT is not one, however
    # readable its first line.
    passed, reason = decide(_package("colorama", "0.4.6"), True, _tiktoken(policy=policy), policy)
    assert passed is False
    assert "family" in reason


def test_a_licence_text_with_no_row_does_not_pass(policy):
    passed, reason = decide(_package("mystery", "1.0"), False, _tiktoken(policy=policy), policy)
    assert passed is False
    assert "does not pass unclassified" in reason


@pytest.mark.parametrize(
    ("text", "named"),
    [
        # A clause further down that puts part of it under something else.
        ("MIT License\n\nPortions: GPL-3.0-only\n", "GPL-3.0"),
        (TIKTOKEN_LICENCE + "\nThe bundled data is under the MPL-2.0.\n", "MPL-2.0"),
        (TIKTOKEN_LICENCE + "\nMozilla Public License 2.0\n", "MPL-2.0"),
        # Allowed, and still a second licence the row did not answer for.
        (TIKTOKEN_LICENCE + "\nSome files (see vendor/) are BSD-3-Clause.\n", "BSD-3-Clause"),
        (TIKTOKEN_LICENCE + "\nnon-commercial use only\n", "non-commercial"),
        # Identifier-shaped, spelt nowhere in the gate, and forbidden all the
        # same by the pattern every forbidden identifier is held to.
        (TIKTOKEN_LICENCE + "\nData: CC-BY-NC-4.0\n", "CC-BY-NC-4.0"),
        (TIKTOKEN_LICENCE + "\nThe server part is AGPL-3.0-or-later.\n", "AGPL-3.0"),
        (TIKTOKEN_LICENCE + "\nThe GPL-licensed parts are not included.\n", "GPL-licensed"),
    ],
)
def test_a_licence_text_that_names_another_licence_fails_the_row(policy, text, named):
    finding = _tiktoken(text, policy)
    assert finding.verdict is Verdict.UNKNOWN
    assert finding.title == "MIT"
    assert named in finding.text_names
    passed, reason = decide(_package("tiktoken", "0.14.0"), False, finding, policy)
    assert passed is False
    assert named in reason
    assert "also names" in reason


def test_an_unspelt_forbidden_identifier_in_the_text_is_called_forbidden(policy):
    finding = _tiktoken(TIKTOKEN_LICENCE + "\nData: CC-BY-NC-4.0\n", policy)
    passed, reason = decide(_package("tiktoken", "0.14.0"), False, finding, policy)
    assert passed is False
    assert "CC-BY-NC-4.0 (forbidden)" in reason


def test_the_tripwire_does_not_read_running_prose(policy):
    # Documented, not wished away: a licence named only inside a sentence is
    # not seen, and the person who signed the row is the safeguard for it.
    prose = "Parts are licensed under the GNU General Public License version 3."
    finding = _tiktoken(TIKTOKEN_LICENCE + "\n" + prose + "\n", policy)
    assert finding.text_names == frozenset({"MIT"})


# tiktoken 0.14.0's METADATA as the wheel carries it: the licence folded into
# the `License` header, every continuation line indented with eight spaces --
# the empty lines of the licence included, which are eight spaces and nothing
# else.
TIKTOKEN_METADATA = (
    "Metadata-Version: 2.4\n"
    "Name: tiktoken\n"
    "Version: 0.14.0\n"
    "Summary: tiktoken is a fast BPE tokeniser for use with OpenAI's models\n"
    "Author: Shantanu Jain\n"
    "License: "
    + "\n".join(
        line if index == 0 else "        " + line
        for index, line in enumerate(TIKTOKEN_LICENCE.rstrip("\n").split("\n"))
    )
    + "\n"
    "Requires-Python: >=3.9\n"
    "Description-Content-Type: text/markdown\n"
    "License-File: LICENSE\n"
    "Requires-Dist: regex>=2022.1.18\n"
    "Requires-Dist: requests>=2.26.0\n"
    "\n"
    "# tiktoken\n"
).encode()
assert b"\n        \n        Copyright (c) 2022" in TIKTOKEN_METADATA


@pytest.mark.parametrize("development_only", [False, True])
def test_tiktoken_as_its_wheel_states_it_passes_on_its_row(policy, development_only):
    metadata = _metadata_from_message(TIKTOKEN_METADATA, "PyPI")
    assert metadata.expression is None
    assert metadata.classifiers == ()
    finding = assess(metadata, policy)
    assert finding.verdict is Verdict.UNKNOWN
    assert finding.identifiers == frozenset()
    assert finding.title == "MIT"
    assert finding.text_names == frozenset({"MIT"})
    passed, reason = decide(_package("tiktoken", "0.14.0"), development_only, finding, policy)
    assert passed is True, reason


def test_a_forbidden_or_restricted_licence_in_the_text_is_said_to_be_one(policy):
    text = "MIT License\n\nPortions: GPL-3.0-only\nOthers: MPL-2.0\n"
    passed, reason = decide(_package("tiktoken", "0.14.0"), False, _tiktoken(text, policy), policy)
    assert passed is False
    assert "forbidden and restricted" in reason


@pytest.mark.parametrize(
    "prose",
    [
        # Words that spell a licence term, used as words.
        "Use in commercial products is permitted.",
        "Copyright (C) 2022-2026 The Authors, all of them.",
        'THE SOFTWARE IS PROVIDED "AS IS" (SEE ABOVE).',
        "The MIT licence applies to every file.",
    ],
)
def test_ordinary_prose_in_a_licence_text_names_nothing_more(policy, prose):
    finding = _tiktoken(TIKTOKEN_LICENCE + "\n" + prose + "\n", policy)
    assert finding.text_names == frozenset({"MIT"})
    assert decide(_package("tiktoken", "0.14.0"), False, finding, policy)[0] is True


def test_a_licence_text_row_for_a_package_not_locked_is_noted_as_ahead_of_adoption(policy):
    out = io.StringIO()
    _report([], policy, out)
    printed = out.getvalue()
    assert "excepts tiktoken, which is not locked (a row may name a package ahead" in printed
    assert "tiktoken, which is no longer locked" not in printed
    # The other tables keep saying what they always said.
    assert "excepts colorama, which is no longer locked" in printed
    assert "excepts pathspec, which is no longer locked" in printed


# ---------------------------------------------------------------------------
# Reading the locked set
# ---------------------------------------------------------------------------

SMALL_LOCK = """
version = 1

[[package]]
name = "robinauts"
version = "0.1.0"
source = { editable = "." }
dependencies = [{ name = "asyncpg" }]

[package.dev-dependencies]
dev = [{ name = "pytest" }]

[[package]]
name = "asyncpg"
version = "0.30.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "pytest"
version = "9.1.1"
source = { registry = "https://pypi.org/simple" }
dependencies = [{ name = "colorama" }, { name = "packaging" }]

[[package]]
name = "packaging"
version = "26.3"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "colorama"
version = "0.4.6"
source = { registry = "https://pypi.org/simple" }
"""


def test_the_whole_locked_set_is_read_and_development_told_from_runtime():
    packages, development_only = read_lock(tomllib.loads(SMALL_LOCK), "robinauts")

    assert [package.name for package in packages] == [
        "asyncpg",
        "colorama",
        "packaging",
        "pytest",
        "robinauts",
    ]
    assert development_only == {"pytest", "colorama", "packaging"}
    assert [package.name for package in packages if package.local] == ["robinauts"]


def test_a_dependency_of_both_kinds_is_not_development_only():
    lock = SMALL_LOCK.replace(
        'dependencies = [{ name = "asyncpg" }]',
        'dependencies = [{ name = "asyncpg" }, { name = "packaging" }]',
    )
    assert lock != SMALL_LOCK
    _, development_only = read_lock(tomllib.loads(lock), "robinauts")
    assert "packaging" not in development_only


@pytest.mark.parametrize(
    ("lock", "complaint"),
    [
        # A lock that parses and says nothing: the gate used to report on
        # nothing and call it a pass.
        ("version = 1\n", "names no package at all"),
        # The right shape, but not this project's lock.
        (
            'version = 1\n[[package]]\nname = "black"\nversion = "1"\n'
            'source = { registry = "https://pypi.org/simple" }\n',
            "does not contain the project itself",
        ),
        # A format we have not been taught, and a table name we do not know.
        ("version = 2\n", "version = 2"),
        ("revision = 3\n", "version = None"),
        ('[[packages]]\nname = "black"\n', "version = None"),
    ],
)
def test_a_lock_the_gate_cannot_believe_is_an_error(lock, complaint):
    parsed = tomllib.loads(lock)
    with pytest.raises(ValueError, match=re.escape(complaint)):
        read_lock(parsed, "robinauts")


@pytest.mark.io
def test_the_real_lock_is_a_version_the_gate_knows():
    lock = tomllib.loads((ROOT / "backend" / "uv.lock").read_text(encoding="utf-8"))
    assert lock["version"] in SUPPORTED_LOCK_VERSIONS


# An editable path dependency: somebody else's code, vendored into a
# directory of this repository and wired up with [tool.uv.sources]. It is
# spelled in the lock exactly as the project itself is.
VENDORED_LOCK = (
    SMALL_LOCK.replace(
        'dependencies = [{ name = "asyncpg" }]',
        'dependencies = [{ name = "asyncpg" }, { name = "evil" }]',
    )
    + """
[[package]]
name = "evil"
version = "1.0.0"
source = { editable = "vendor/evil" }
"""
)
assert "evil" in VENDORED_LOCK
assert VENDORED_LOCK != SMALL_LOCK


def test_an_editable_dependency_is_not_this_repository():
    packages, _ = read_lock(tomllib.loads(VENDORED_LOCK), "robinauts")
    evil = next(package for package in packages if package.name == "evil")

    # Were it local, the gate would stamp it Apache-2.0 without reading a
    # thing; were it a registry package, it would ask PyPI about a name that
    # has nothing to do with it.
    assert evil.local is False
    assert evil.index is None
    assert [package.name for package in packages if package.local] == ["robinauts"]


def test_a_vendored_gpl_package_is_judged_like_any_other(policy):
    packages, development_only = read_lock(tomllib.loads(VENDORED_LOCK), "robinauts")
    evil = next(package for package in packages if package.name == "evil")
    finding = assess(Metadata(expression="GPL-3.0-only"), policy)

    assert finding.verdict is Verdict.FORBIDDEN
    passed, reason = decide(evil, evil.name in development_only, finding, policy)
    assert passed is False
    assert "forbidden" in reason


def test_a_vendored_package_that_claims_the_project_root_is_not_ours():
    # `source = { editable = "." }` is how the project itself is locked. A
    # dependency that says the same thing is not thereby the project.
    lock = VENDORED_LOCK.replace(
        'source = { editable = "vendor/evil" }', 'source = { editable = "." }'
    )
    assert lock != VENDORED_LOCK
    packages, _ = read_lock(tomllib.loads(lock), "robinauts")

    assert [package.name for package in packages if package.local] == ["robinauts"]


EXTRA_WORKER_LOCK = """
version = 1

[[package]]
name = "worker"
version = "0.1.0"
source = { editable = "." }
dependencies = [{ name = "robinauts" }]

[[package]]
name = "robinauts"
version = "0.1.0"
source = { editable = "../../backend" }
"""


BACKEND = ("robinauts", "../../backend")
COMMON = ("robinauts-worker-common", "../worker-common")


@pytest.mark.parametrize(
    ("beside", "editable", "ours"),
    [
        ({BACKEND}, "../../backend", ["robinauts", "worker"]),
        ((), "../../backend", ["worker"]),
        ({BACKEND}, "../../vendor/robinauts", ["worker"]),
        ({BACKEND}, "../../backend/", ["worker"]),
        ({("other", "../../backend")}, "../../backend", ["worker"]),
    ],
)
def test_an_extra_worker_lock_owns_the_backend_at_its_exact_path(beside, editable, ours):
    lock = EXTRA_WORKER_LOCK.replace('"../../backend"', f'"{editable}"')
    packages, _ = read_lock(tomllib.loads(lock), "worker", beside)
    assert sorted(package.name for package in packages if package.local) == ours


@pytest.mark.parametrize(
    ("editable", "ours"),
    [
        ("../worker-common", ["robinauts", "robinauts-worker-common", "worker"]),
        ("../../vendor/worker-common", ["robinauts", "worker"]),
        ("../worker-common/", ["robinauts", "worker"]),
    ],
)
def test_an_extra_worker_lock_owns_a_project_beside_it_at_its_exact_path(editable, ours):
    lock = EXTRA_WORKER_LOCK + f"""
[[package]]
name = "robinauts-worker-common"
version = "0.1.0"
source = {{ editable = "{editable}" }}
"""
    packages, _ = read_lock(tomllib.loads(lock), "worker", {BACKEND, COMMON})
    assert sorted(package.name for package in packages if package.local) == ours


def test_each_mcp_server_is_checked_against_its_own_lock_and_environment():
    assert MCP_SERVERS["to-do-list"] == (
        ROOT / "mcp" / "to-do-list" / "uv.lock",
        ROOT / "mcp" / "to-do-list" / ".venv",
    )


def test_one_lock_at_a_time(capsys):
    with pytest.raises(SystemExit) as refused:
        main(["--extra-worker", "base-worker", "--mcp-server", "to-do-list"])
    assert refused.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


def test_the_index_a_package_came_from_is_remembered():
    lock = SMALL_LOCK.replace(
        'name = "asyncpg"\nversion = "0.30.0"\nsource = { registry = "https://pypi.org/simple" }',
        'name = "asyncpg"\nversion = "0.30.0"\n'
        'source = { registry = "https://packages.example.com/simple/" }',
    )
    assert lock != SMALL_LOCK
    packages, _ = read_lock(tomllib.loads(lock), "robinauts")
    by_name = {package.name: package for package in packages}

    assert by_name["pytest"].index == "https://pypi.org/simple"
    # Trailing slash and case are not a difference; the host is.
    assert by_name["asyncpg"].index == "https://packages.example.com/simple"


def test_the_artefacts_the_lock_pins_are_remembered():
    lock = SMALL_LOCK.replace(
        'name = "colorama"\nversion = "0.4.6"\nsource = { registry = "https://pypi.org/simple" }',
        'name = "colorama"\nversion = "0.4.6"\nsource = { registry = "https://pypi.org/simple" }\n'
        'sdist = { url = "https://example/colorama.tar.gz", hash = "sha256:bbb" }\n'
        'wheels = [{ url = "https://example/colorama.whl", hash = "sha256:aaa" }]',
    )
    assert lock != SMALL_LOCK
    packages, _ = read_lock(tomllib.loads(lock), "robinauts")
    colorama = next(package for package in packages if package.name == "colorama")

    assert colorama.artefacts == frozenset({"aaa", "bbb"})


@pytest.mark.parametrize(
    ("lock", "complaint"),
    [
        ('version = 1\npackage = "oops"\n', "not a list of packages"),
        ('version = 1\n[[package]]\nversion = "1"\n', "no name"),
        ("version = 1\n[[package]]\nname = 3\n", "no name"),
    ],
)
def test_a_lock_of_the_wrong_shape_is_an_error_not_a_traceback(lock, complaint):
    parsed = tomllib.loads(lock)
    with pytest.raises(ValueError, match=complaint):
        read_lock(parsed, "robinauts")


def test_a_workspace_member_is_this_repository():
    # The manifest goes after the package tables: in TOML, what follows a
    # table heading belongs to it.
    lock = VENDORED_LOCK + '\n[manifest]\nmembers = ["robinauts", "evil"]\n'
    packages, _ = read_lock(tomllib.loads(lock), "robinauts")
    assert sorted(package.name for package in packages if package.local) == ["evil", "robinauts"]


# ---------------------------------------------------------------------------
# Metadata is read from PyPI only when its digest checks out
# ---------------------------------------------------------------------------

METADATA = b"Metadata-Version: 2.4\nName: colorama\nVersion: 0.4.6\nLicense-Expression: MIT\n"
DIGEST = hashlib.sha256(METADATA).hexdigest()


ARTEFACT = "a" * 64  # the sha256 the lockfile pins for the wheel


def _published(digest, filename="colorama-0.4.6-py2.py3-none-any.whl", artefact=ARTEFACT):
    return {
        "filename": filename,
        "packagetype": "bdist_wheel",
        "url": f"https://files.pythonhosted.org/{filename}",
        "core-metadata": digest,
        "digests": {"sha256": artefact},
    }


def _source(served=METADATA):
    source = MetadataSource(None)
    source._fetch = lambda url: served  # noqa: E731 -- a stand-in for the network
    return source


def test_metadata_whose_digest_matches_is_read():
    lookup = _source()._from_published_files(
        [_published({"sha256": DIGEST})], frozenset({ARTEFACT})
    )
    assert lookup.metadata.expression == "MIT"


def test_metadata_whose_digest_does_not_match_is_a_failure_not_a_fallback():
    lookup = _source(b"License-Expression: MIT\n")._from_published_files(
        [_published({"sha256": DIGEST})], frozenset({ARTEFACT})
    )
    assert lookup.metadata is None
    assert "does not match" in lookup.problem


def test_metadata_published_without_a_digest_is_not_read():
    # PyPI answers the same either way; what differs is whether we can check
    # what we were given. Unchecked, it is not evidence of anything.
    lookup = _source()._from_published_files(
        [_published(True), _published(False, "old.tar.gz")], frozenset({ARTEFACT})
    )
    assert lookup.metadata is None
    assert "no hashed metadata" in lookup.problem


@pytest.mark.io
@pytest.mark.parametrize(
    "layout",
    ["lib/python3.12/site-packages", "Lib/site-packages"],  # POSIX, then Windows
)
def test_an_environment_is_read_whichever_way_the_platform_lays_it_out(tmp_path, layout):
    distribution = tmp_path / "environment" / layout / "colorama-0.4.6.dist-info"
    distribution.mkdir(parents=True)
    (distribution / "METADATA").write_bytes(METADATA)

    lookup = MetadataSource(tmp_path / "environment").get(LockedPackage("colorama", "0.4.6"))

    assert lookup.metadata is not None
    assert lookup.metadata.expression == "MIT"


def test_metadata_for_a_file_the_lock_does_not_pin_is_not_read():
    # The index serves what it likes under a name; the lock pins the file we
    # will actually install, and that is the one whose licence counts.
    lookup = _source()._from_published_files(
        [_published({"sha256": DIGEST}, artefact="b" * 64)], frozenset({ARTEFACT})
    )
    assert lookup.metadata is None
    assert "no file this lockfile pins" in lookup.problem


def test_a_package_from_another_index_is_not_looked_up_on_pypi():
    elsewhere = LockedPackage("black", "26.5.1", index="https://packages.example.com/simple")
    lookup = _source().get(elsewhere)

    assert lookup.metadata is None
    assert "packages.example.com" in lookup.problem


def test_a_package_locked_outside_an_index_is_not_looked_up():
    lookup = _source().get(LockedPackage("evil", "1.0.0", index=None))
    assert lookup.metadata is None
    assert "outside any package index" in lookup.problem


# ---------------------------------------------------------------------------
# The document and the gate, kept together
# ---------------------------------------------------------------------------


@pytest.mark.io
def test_the_real_document_parses_and_says_what_the_gate_expects():
    policy = parse_policy((ROOT / "DEPENDENCIES.md").read_text(encoding="utf-8"))

    assert {"apache-2.0", "mit", "bsd-2-clause", "bsd-3-clause", "0bsd", "isc"} <= policy.allowed
    assert {"zlib", "postgresql", "psf-2.0", "cc0-1.0", "unlicense"} <= policy.allowed
    assert {"cnri-python", "mit-0"} <= policy.allowed
    assert policy.restricted == {"mpl-2.0", "epl-2.0", "cddl-1.0", "cddl-1.1"}
    assert "pathspec" in policy.restricted_packages
    assert policy.licence_text_exceptions["tiktoken"] == NamedException("MIT", "0.14.0")


@pytest.mark.io
@pytest.mark.parametrize("category", ["Allowed", "Restricted"])
def test_the_real_document_cannot_be_edited_into_allowing_the_gpl(category):
    # The document is the policy, and anyone can edit it. Adding a forbidden
    # licence to a category is not a decision the gate carries out.
    document = (ROOT / "DEPENDENCIES.md").read_text(encoding="utf-8")
    doctored = document.replace(f"### {category}\n\n", f"### {category}\n\nGPL-3.0, ", 1)
    assert doctored != document

    with pytest.raises(ValueError, match="GPL-3.0"):
        parse_policy(doctored)


@pytest.mark.io
def test_every_package_the_document_excepts_is_really_in_the_locked_set():
    policy = parse_policy((ROOT / "DEPENDENCIES.md").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "backend" / "uv.lock").read_text(encoding="utf-8"))
    packages, development_only = read_lock(lock, "robinauts")
    locked = {package.name for package in packages}

    assert set(policy.restricted_packages) <= locked
    assert set(policy.development_exceptions) <= locked
    # An exception for a development-only dependency is worth nothing if the
    # dependency is not development-only.
    assert set(policy.development_exceptions) <= development_only
    # "Excepted licence texts" is deliberately not held to the lock. A row
    # there says which licence a text is, and DEPENDENCIES.md lets it name a
    # package ahead of its adoption -- as tiktoken's was before the OpenAI
    # clients brought it -- so the gate notes such a row as not locked rather
    # than failing on it. What this test does insist on is that
    # a row there is not also a row of a table that is held to the lock.
    assert not set(policy.licence_text_exceptions) & (
        set(policy.restricted_packages) | set(policy.development_exceptions)
    )


@pytest.mark.io
def test_the_two_gates_forbid_the_same_licences():
    """One policy, two gates, one list of what it forbids.

    `frontend/scripts/check-licences.mjs` judges the same policy over npm's
    metadata, which never reaches this gate as PyPI's never reaches that one.
    Two separately maintained notions of "forbidden" drift. Both test suites
    read the fixtures below, so the day they disagree one of them goes red --
    this one for Python, `frontend/src/test/licence-gate.test.ts` for npm.

    The two checks are not identical, and the fixture file says so. This gate
    resolves a spelling through `_SPELLINGS` before judging it; npm has no such
    table, so that gate matches fragments instead and catches `GPLv3` and
    `Commons Clause`, which the regular expression here reads as unknown. Those
    live under `npm_only`, and all this suite asks of them is that they never
    come out ALLOWED here -- unknown is refused, ALLOWED would be a hole.
    """
    fixtures = json.loads((ROOT / "scripts" / "licence-fixtures.json").read_text(encoding="utf-8"))
    shared = fixtures["shared"]
    assert shared, "the shared fixture list is empty, so it proves nothing"

    for identifier, verdict in shared:
        forbidden = _FORBIDDEN_IDENTIFIER.match(identifier) is not None
        assert forbidden == (verdict == "forbidden"), (
            f"{identifier}: this gate says "
            f"{'forbidden' if forbidden else 'not-forbidden'}, the fixtures say {verdict}"
        )

    # Both verdicts really occur, so that a fixture file of one kind cannot
    # pass by saying nothing.
    verdicts = {verdict for _, verdict in shared}
    assert verdicts == {"forbidden", "not-forbidden"}

    # The npm-only spellings are npm's, and this gate is not held to them --
    # but none of them may be something this gate would wave through as
    # allowed, which is the only way the split could hide anything.
    policy = parse_policy((ROOT / "DEPENDENCIES.md").read_text(encoding="utf-8"))
    assert fixtures["npm_only"], "the npm-only list is empty, so it proves nothing"
    for identifier, verdict in fixtures["npm_only"]:
        if verdict == "forbidden":
            assert policy.category(identifier) is not Verdict.ALLOWED, identifier

    # And every identifier the document itself allows is one neither gate
    # forbids: a category that contradicted the forbidden list would be a
    # document nobody could satisfy.
    for identifier in policy.allowed:
        assert _FORBIDDEN_IDENTIFIER.match(identifier) is None, identifier

    # The restricted family, however it is spelt. The npm gate has to call
    # every one of these restricted, because only a development-only row may
    # carry one; all this gate is asked is that it never calls one allowed.
    assert fixtures["restricted"], "the restricted list is empty, so it proves nothing"
    for identifier, verdict in fixtures["restricted"]:
        assert verdict == "restricted", identifier
        assert policy.category(identifier) is not Verdict.ALLOWED, identifier

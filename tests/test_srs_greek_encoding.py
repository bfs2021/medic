"""FDA SRS writes Greek letters between periods; ontologies spell them out (I-8).

An SPL's active moiety is `.ALPHA.-TOCOPHEROL` where ChEBI's label is `alpha-tocopherol`.
That is an encoding difference — the periods are delimiters carrying no meaning — so the
strings should match, and before `srs_greek_encoding` they did not. 89 moiety-name
occurrences across 11 distinct names use the convention in the current SPL set; the rule
recovers 51, four of them onto an exact ontology label.

This matters more than 51 rows: a moiety that does not ground makes the ingest loop skip the
label *before* extraction, so every indication on that SPL is discarded.

Done as a `PreprocessingRuleEnum` rule rather than as a fix-up in the caller. An earlier
attempt at a different grounding gap mangled the string inside `_ground_drug` and re-asked,
which published an identifier with no recorded rule and no replayable chain from the label's
own literal — exactly what I-8 forbids. The rule id lands in SSSOM `subject_preprocessing`,
so the transform is auditable.
"""

from __future__ import annotations

import pytest

from medic.grounding.lexical.preprocess import (
    RULE_CERTAINTY,
    RULE_PREDICATE,
    generate_variants,
)


def _applied(s: str) -> dict[str, list[str]]:
    return {v.string: v.applied for v in generate_variants(s)}


@pytest.mark.parametrize("encoded,expected", [
    (".alpha.-tocopherol", "alpha-tocopherol"),
    (".gamma.-aminobutyric acid", "gamma-aminobutyric acid"),
    (".beta.-carotene", "beta-carotene"),
    (".alpha.-lipoic acid", "alpha-lipoic acid"),
    (".alpha.-ketoglutaric acid", "alpha-ketoglutaric acid"),
    # Not only at the start of the string.
    ("dihydro-.alpha.-ergocryptine", "dihydro-alpha-ergocryptine"),
    # Multiple letters in one name.
    (".alpha.,.beta.-dihydroxy", "alpha,beta-dihydroxy"),
])
def test_the_encoding_is_rewritten(encoded, expected):
    assert _applied(encoded).get(expected) == ["srs_greek_encoding"]


@pytest.mark.parametrize("untouched", [
    # Not a Greek letter: the closed list is what stops this firing on any abbreviation
    # that happens to sit between periods.
    "e.coli. infection",
    "u.s.p. grade",
    "vitamin d.3. analogue",
    "plain substance name",
    # Already spelled out — nothing to do, and no variant should be produced.
    "alpha-tocopherol",
])
def test_a_non_greek_period_group_is_left_alone(untouched):
    assert "srs_greek_encoding" not in [
        r for applied in _applied(untouched).values() for r in applied
    ]


def test_the_rule_is_registered_in_both_code_maps():
    """A code-vs-schema test enforces these agree with `PreprocessingRuleEnum`; this pins
    that the rule was added to both rather than only to the generator."""
    assert RULE_CERTAINTY["srs_greek_encoding"] == 0.98
    assert RULE_PREDICATE["srs_greek_encoding"] == "skos:exactMatch"


def test_it_is_an_exact_match_not_a_close_one():
    """The distinction is load-bearing: `salt_ester_strip` is a closeMatch because dropping
    a salt changes the substance, whereas rewriting a delimiter does not change anything at
    all, so an exactMatch is honest here and a closeMatch would understate it."""
    assert RULE_PREDICATE["srs_greek_encoding"] == "skos:exactMatch"
    assert RULE_PREDICATE["salt_ester_strip"] == "skos:closeMatch"

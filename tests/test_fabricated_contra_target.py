"""A restriction with no condition in it must not become a target (#97).

The structured contraindication prompt asks for one record per contraindication. A clause
that restricts only a population ("contraindicated in children younger than 12 years") or
only a co-administration ("contraindicated with MAOIs") has no condition to put in
``target``, and the smoke run over real SPL labels showed the model filling the hole by
inventing one: ``contraindication in pediatric population``, ``contraindication with
concurrent MAOI use``.

Why this needs a deterministic guard and not just a prompt rule: the failure is silent.
The disease lexical index holds 471 strings containing "contraindicat" — SNOMED procedure
concepts like "Procedure contraindicated" — so an invented target does not fail grounding
and get noticed. It matches one of those and ships as a plausible drug-disease edge.

The strings below are verbatim from that smoke run, not invented for the test.
"""

from __future__ import annotations

import pytest

from medic.ingest.common import looks_like_disease_name
from medic.ingest.dailymed.__main__ import parse_structured_claims


@pytest.mark.parametrize("fabricated", [
    "contraindication in pediatric population",
    "contraindication with concurrent MAOI use",
    "Contraindication in post-operative tonsillectomy and/or adenoidectomy",
    "contraindicated in pregnancy",
])
def test_a_target_naming_the_restriction_is_rejected(fabricated):
    assert not looks_like_disease_name(fabricated)


@pytest.mark.parametrize("real", [
    # All four came back from the same smoke run and are correct extractions.
    "NSAID-induced asthma",
    "significant respiratory depression",
    "acute or severe bronchial asthma in an unmonitored setting",
    "known or suspected gastrointestinal obstruction, including paralytic ileus",
    "porphyria",
    "angioedema",
])
def test_a_real_contraindicated_condition_still_passes(real):
    assert looks_like_disease_name(real)


def test_the_fabricated_record_is_dropped_by_the_parser():
    """End to end: the guard runs inside the parser, so a model that ignores rule 7 still
    cannot put the string on a row."""
    claims = parse_structured_claims(
        "target: contraindication in pediatric population :: younger than 12 years\n"
        "predicate: treats\n"
        "population: children younger than 12 years\n"
        "---\n"
        "target: porphyria :: porphyria\n"
        "predicate: treats\n"
    )
    assert [c.target for c in claims] == ["porphyria"]


def test_a_co_administration_only_clause_keeps_its_drug_but_emits_no_target():
    """The drug is not lost — it has nowhere to be an edge. Dropping the fabricated target
    drops a non-edge, not a claim: MeDIC's product is drug-disease pairs, and a pure
    drug-drug restriction has no disease to pair with."""
    claims = parse_structured_claims(
        "target: contraindication with concurrent MAOI use :: monoamine oxidase inhibitors\n"
        "predicate: treats\n"
        "co_administered_with: monoamine oxidase inhibitors (MAOIs)\n"
    )
    assert claims == []

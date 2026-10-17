"""The label's own relation chooses the predicate (#99).

All 12,448 indication edges shipped as `biolink:treats`, so a graph asked "what treats
pertussis?" answered with the vaccine and one asked "what prevents stroke in AF?" got
nothing, because rivaroxaban -> stroke was `treats`. The extraction now reports the relation
it read, and that drives the predicate.

Phase 1's vaccine name-match stays as the fallback for rows with no recorded relation —
which is every row until the re-extraction lands. Dropping it would regress the 37 vaccine
edges it already fixed.

Where the mapping collapses two relations onto one predicate, `medic_claim_relation`
preserves the distinction, so nothing the label said is lost even when Biolink has no
separate slot for it.
"""

from __future__ import annotations

import pytest

from medic.export.kgx import biolink as bl
from medic.export.kgx import edges


def _assertion(**evidence_extra):
    from tests.test_kgx_export import _assertion as base
    a = base()
    a.setdefault("evidence", {}).update(evidence_extra)
    for key in ("drug", "disease"):
        mention = a.get(key) or {}
        for step in ((mention.get("resolution") or {}).get("pipeline") or []):
            if step.get("category") == "GROUNDING":
                step["output_value"] = mention.get("resolved_id") or "MONDO:0000240"
                step.setdefault("confidence", 0.99)
                step.setdefault("confidence_basis", "MEASURED")
    return a


def _edge(drug_label="rivaroxaban", **evidence_extra):
    pair = {"drug_id": "CHEBI:1", "drug_label": drug_label,
            "disease_id": "MONDO:1", "disease_label": "stroke",
            "relationship_type": "INDICATION", "reliability": "HIGH",
            "assertions": [_assertion(**evidence_extra)]}
    return edges.association_edges(pair)[0]


@pytest.mark.parametrize("relation,predicate", [
    ("treats", bl.INDICATION_PREDICATE),
    ("prevents", bl.PREVENTION_PREDICATE),
    ("reduces_risk_of", bl.PREVENTION_PREDICATE),
    ("maintenance", bl.INDICATION_PREDICATE),
    ("diagnoses", bl.DIAGNOSTIC_PREDICATE),
])
def test_the_recorded_relation_chooses_the_predicate(relation, predicate):
    assert _edge(claim_predicate=relation)["predicate"] == predicate


def test_the_relation_is_preserved_where_the_predicate_collapses():
    """reduces_risk_of and prevents share a predicate; maintenance and treats share one.
    The finer distinction the label drew must survive."""
    e = _edge(claim_predicate="reduces_risk_of")
    assert e["medic_claim_relation"] == "reduces_risk_of"
    assert _edge(claim_predicate="maintenance")["medic_claim_relation"] == "maintenance"


def test_a_plain_treats_claim_records_no_relation_slot():
    """The default adds nothing, so the export does not grow a column of constants."""
    assert "medic_claim_relation" not in _edge(claim_predicate="treats")


def test_the_vaccine_fallback_still_applies_without_a_recorded_relation():
    """Every row has no claim_predicate until the re-extraction runs; dropping the
    name-match would regress the 37 vaccine edges Phase 1 fixed."""
    assert _edge(drug_label="pertussis vaccine")["predicate"] == bl.PREVENTION_PREDICATE


def test_a_recorded_relation_overrides_the_name_match():
    """A vaccine label that genuinely states treatment is believed over its own name."""
    assert _edge(drug_label="pertussis vaccine",
                 claim_predicate="treats")["predicate"] == bl.INDICATION_PREDICATE


def test_approval_status_still_takes_precedence():
    """Biolink reserves an asserted predicate for approved use. Whether the relation is
    treatment or prevention is orthogonal to whether the regulator approved it."""
    pair = {"drug_id": "CHEBI:1", "drug_label": "x", "disease_id": "MONDO:1",
            "disease_label": "y", "relationship_type": "INDICATION",
            "assertions": [_assertion(claim_predicate="prevents",
                                      approval_status="INVESTIGATIONAL")]}
    a = pair["assertions"][0]
    a.get("regulatory_status", {}).update({"status": "INVESTIGATIONAL"})
    assert edges.association_edges(pair)[0]["predicate"] == \
        bl.INDICATION_UNAPPROVED_PREDICATE


def test_every_mapped_predicate_is_valid_biolink_with_a_knowledge_level():
    for predicate in (bl.INDICATION_PREDICATE, bl.PREVENTION_PREDICATE,
                      bl.DIAGNOSTIC_PREDICATE):
        assert bl.is_predicate(predicate)
        assert predicate in bl.KNOWLEDGE_LEVELS

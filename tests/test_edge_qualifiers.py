"""The label's context ships as Biolink qualifiers (#90).

Every indication edge was a bare `(drug, treats, disease)` triple, so the context a label
attaches to a claim went one of two ways: promoted to the object — the 13.1% setting-as-target
error of #89 — or dropped, which the audit scores as *correct* and so never counted at all.

Biolink already has the slots on the `treats` association, and DAKP populates them on 5,686
of its 16,655 `treats` edges, so there is consumer precedent in the same graph MeDIC sits
next to. All five used here are in the pinned 4.4.6 model, which the conformance gate
checks independently: an un-namespaced edge property that is not a Biolink slot is an error.

`disease_context_qualifier` is the one no source in the graph fills yet.
"""

from __future__ import annotations

import pytest

from medic.export.kgx import edges, validate


def _assertion(**evidence_extra):
    from tests.test_kgx_export import _assertion as base
    a = base()
    ev = a.setdefault("evidence", {})
    ev.update(evidence_extra)
    for key in ("drug", "disease"):
        mention = a.get(key) or {}
        for step in ((mention.get("resolution") or {}).get("pipeline") or []):
            if step.get("category") == "GROUNDING":
                step["output_value"] = mention.get("resolved_id") or "MONDO:0000240"
                step.setdefault("confidence", 0.99)
                step.setdefault("confidence_basis", "MEASURED")
    return a


def _edge(**evidence_extra):
    pair = {"drug_id": "CHEBI:10023", "drug_label": "bosentan",
            "disease_id": "MONDO:0000240", "disease_label": "PAH",
            "relationship_type": "INDICATION", "reliability": "HIGH",
            "assertions": [_assertion(**evidence_extra)]}
    built = edges.association_edges(pair)
    assert built, "expected one edge"
    return built[0]


def test_a_grounded_disease_context_becomes_a_qualifier():
    """bosentan -> PAH *associated with* congenital heart disease. Previously the CHD was
    either the object or gone."""
    e = _edge(disease_context=["congenital heart disease"],
              disease_context_ids=["MONDO:0005453"])
    assert e["disease_context_qualifier"] == "MONDO:0005453"


def test_a_population_becomes_a_qualifier():
    e = _edge(population_context="adult and pediatric patients")
    assert e["population_context_qualifier"] == "adult and pediatric patients"


def test_a_temporal_context_becomes_a_qualifier():
    e = _edge(temporal_context="following myocardial infarction")
    assert e["temporal_context_qualifier"] == "following myocardial infarction"


def test_a_sex_restriction_is_routed_to_the_sex_qualifier():
    """Biolink has a dedicated slot, and DAKP fills it on 1,991 edges. A population that is
    only a sex belongs there rather than in the general population slot."""
    e = _edge(population_context="postmenopausal women")
    assert e["sex_qualifier"] == "PATO:0000383"       # female: the slot's range is a class
    assert e["population_context_qualifier"] == "postmenopausal women"


def test_a_population_without_a_sex_sets_no_sex_qualifier():
    e = _edge(population_context="adults")
    assert "sex_qualifier" not in e


def test_several_disease_contexts_put_one_term_on_the_slot():
    """Biolink defines `disease_context_qualifier` as single-valued, so a list would be the
    same discipline violation the export already fixed for primary_knowledge_source. The
    rest stay reachable on the namespaced list."""
    e = _edge(disease_context=["psoriasis", "Crohn disease"],
              disease_context_ids=["MONDO:0005083", "MONDO:0005011"])
    assert e["disease_context_qualifier"] == "MONDO:0005083"
    assert isinstance(e["disease_context_qualifier"], str)
    assert e["medic_disease_context_ids"] == ["MONDO:0005083", "MONDO:0005011"]


# ---------------------------------------------------------------------------
# The slot's range is `disease`, so only MONDO may ride on it (#110)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("context_id", [
    # Both are on the `disease` class's `id_prefixes`, so the pydantic constructor accepts
    # them and this is the only guard that does not. 3,702 UMLS and 1,618 HP values reached
    # the slot on the 95,170-edge build.
    "UMLS:C0018802",
    "HP:0001627",
])
def test_a_grounded_but_non_mondo_context_stays_off_the_slot(context_id):
    e = _edge(disease_context=["congestive heart failure"],
              disease_context_ids=[context_id])
    assert "disease_context_qualifier" not in e
    assert e["medic_disease_context_ids"] == [context_id], "the id itself is not discarded"


def test_an_ungrounded_context_stays_off_the_slot_but_keeps_its_wording():
    """4,708 edges carried free text on a class-ranged slot, over 1,022 distinct strings —
    `non-neutropenic patients`, `radiological examination of the stomach`. The slot is not
    the place for them, but the export is not allowed to forget them either: nothing else on
    the record holds the context once the slot is MONDO-only, because a context is never
    minted a MEDICNE id."""
    e = _edge(disease_context=["non-neutropenic patients"])
    assert "disease_context_qualifier" not in e
    assert e["medic_disease_context"] == ["non-neutropenic patients"]
    assert "medic_disease_context_ids" not in e


def test_the_names_and_the_ids_stay_positionally_aligned():
    """`record_claim_context` writes the ids positional with the names, `""` where one did
    not ground, and the products' tests pin that. The edge used to compact the list, which
    was harmless while the names were not on the edge — with both there, compacting re-pairs
    every name after an ungrounded one with the wrong CURIE."""
    e = _edge(disease_context=["something unmappable", "hepatic cirrhosis"],
              disease_context_ids=["", "MONDO:0005155"])
    assert e["medic_disease_context"] == ["something unmappable", "hepatic cirrhosis"]
    assert e["medic_disease_context_ids"] == ["", "MONDO:0005155"]
    assert e["disease_context_qualifier"] == "MONDO:0005155"


def test_the_first_mondo_term_is_taken_not_the_first_id():
    """The ids arrive in the order the label named the contexts, which carries no priority.
    Reading position 0 as the preferred context would blank the slot on 481 edges of the
    current build that do have a MONDO context sitting behind a CUI."""
    e = _edge(disease_context=["some syndrome", "hepatic cirrhosis"],
              disease_context_ids=["UMLS:C0018802", "MONDO:0005155"])
    assert e["disease_context_qualifier"] == "MONDO:0005155"


def test_the_gate_rejects_a_non_mondo_context_qualifier():
    """The builder cannot emit one any more, so the gate is what holds the rule for a file
    written by an older build or edited by hand — the same reason the conformance gate
    re-validates every record through its class rather than trusting the builders."""
    e = _edge(disease_context=["congenital heart disease"],
              disease_context_ids=["MONDO:0005453"])
    report = validate.check([], [{**e, "disease_context_qualifier": "UMLS:C0018802"}])
    assert [p for p in report.errors if "not a MONDO term" in str(p)]


def test_the_gate_rejects_free_text_on_the_context_qualifier():
    e = _edge(disease_context=["congenital heart disease"],
              disease_context_ids=["MONDO:0005453"])
    report = validate.check([], [{**e, "disease_context_qualifier": "non-neutropenic"}])
    assert [p for p in report.errors if "not a MONDO term" in str(p)]


def test_a_mondo_context_qualifier_passes_the_gate():
    e = _edge(disease_context=["congenital heart disease"],
              disease_context_ids=["MONDO:0005453"])
    report = validate.check([], [e])
    assert not [p for p in report.errors if "disease_context_qualifier" in str(p)]


def test_co_administration_is_namespaced_not_a_biolink_slot():
    """Biolink has no co-administration slot on this association, so it goes in the
    medic_ extension layer rather than being invented as a bare property — the class
    constructor would reject it."""
    e = _edge(co_administered_with=["methotrexate"])
    assert e["medic_co_administered_with"] == ["methotrexate"]
    assert "co_administered_with" not in e


def test_an_edge_without_context_carries_no_qualifier_slots():
    e = _edge()
    for slot in ("disease_context_qualifier", "population_context_qualifier",
                 "temporal_context_qualifier", "sex_qualifier"):
        assert slot not in e


def test_the_qualified_edge_passes_the_conformance_gate():
    """The gate errors on any un-namespaced property that is not a Biolink slot, so this
    proves the slot names are real rather than plausible."""
    e = _edge(disease_context=["congenital heart disease"],
              disease_context_ids=["MONDO:0005453"],
              population_context="postmenopausal women",
              temporal_context="post-MI",
              co_administered_with=["methotrexate"])
    report = validate.check([], [e])
    unknown = [p for p in report.errors if "not a Biolink slot" in str(p)]
    assert not unknown, unknown


# ---------------------------------------------------------------------------
# Sex detection must not match inside another word
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("population", [
    # "men" is a substring of both of these. A naive `in` test tags them male.
    "postmenopausal patients",
    "patients requiring management of pain",
    "adults",
    "pediatric patients aged 3 months to 12 years",
    "renal transplant recipients",
])
def test_a_word_containing_men_is_not_a_sex_restriction(population):
    e = _edge(population_context=population)
    assert "sex_qualifier" not in e, f"{population!r} wrongly read as sex-restricted"


@pytest.mark.parametrize("population,expected", [
    ("postmenopausal women", "PATO:0000383"),
    ("pregnant women", "PATO:0000383"),
    ("adult males", "PATO:0000384"),
    ("female patients of childbearing potential", "PATO:0000383"),
])
def test_a_real_sex_restriction_is_detected(population, expected):
    """`sex_qualifier` has range `biological sex`, so the value is the PATO term."""
    assert _edge(population_context=population)["sex_qualifier"] == expected

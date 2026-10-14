"""A disease named only as the population or setting is not what the drug treats (#89).

38 of 291 audited edges (13.1%) name the patient population, background condition or
setting rather than the treated condition — half of all MeDIC errors, and the largest single
class. eplerenone to acute MI ("heart failure *post-MI*"), posaconazole to AML ("antifungal
prophylaxis *in patients with* AML"), bosentan to congenital heart disease ("PAH
*associated with* CHD"), basiliximab to kidney transplantation ("prophylaxis of rejection
*in patients receiving* renal transplantation").

The interim rule uses evidence the ingest already holds and the merge does not: the *other*
diseases extracted from the same section. A disease whose every occurrence sits behind a
context preposition, while a sibling disease occurs as a plain target, is the setting and
the sibling is the claim. Where no sibling is a plain target there is nothing to compare
against and no verdict is returned — a lone "in patients with X" may well be the only thing
the label says.

Reported, never dropped. #66's lesson: a destructive verdict needs stronger evidence than a
preposition, and the reliability gate can express "probably the setting" without deleting
the row.
"""

from __future__ import annotations

from medic.validation.extraction_fidelity import context_only_diseases

# The real label says "invasive Aspergillus and Candida infections"; spelled as the
# extracted name here so the sibling is locatable, which is what the comparison needs.
POSA = ("POSACONAZOLE is indicated for the prophylaxis of invasive aspergillosis and "
        "candidiasis in patients with acute myeloid leukemia.")
EPLE = ("EPLERENONE is indicated to improve survival of stable patients with heart failure "
        "post myocardial infarction.")
BOSE = ("BOSENTAN is indicated for the treatment of pulmonary arterial hypertension "
        "associated with congenital heart disease.")


def test_a_population_disease_is_reported():
    out = context_only_diseases(
        ["invasive aspergillosis", "acute myeloid leukemia"], POSA)
    assert "acute myeloid leukemia" in out
    assert "invasive aspergillosis" not in out


def test_a_post_event_setting_is_reported():
    out = context_only_diseases(["heart failure", "myocardial infarction"], EPLE)
    assert "myocardial infarction" in out
    assert "heart failure" not in out


def test_an_associated_with_background_is_reported():
    out = context_only_diseases(
        ["pulmonary arterial hypertension", "congenital heart disease"], BOSE)
    assert "congenital heart disease" in out
    assert "pulmonary arterial hypertension" not in out


def test_nothing_is_reported_without_a_plain_sibling():
    """No sibling occurs as a plain target, so there is nothing to compare against and the
    "in patients with" phrase may be all the label says."""
    assert context_only_diseases(["acute myeloid leukemia"], POSA) == []


def test_a_plain_indication_is_untouched():
    text = "PRODUCT is indicated for the treatment of epilepsy and migraine."
    assert context_only_diseases(["epilepsy", "migraine"], text) == []


def test_an_unlocatable_disease_is_not_reported():
    """No occurrence means no judgement, as everywhere else in this module."""
    assert context_only_diseases(
        ["type 2 diabetes mellitus", "heart failure"], EPLE) == []


def test_a_disease_both_context_and_target_is_untouched():
    """"indicated for X ... in patients with X" — a plain occurrence wins."""
    text = ("PRODUCT is indicated for the treatment of anemia in patients with "
            "chronic kidney disease, and for anemia generally.")
    assert "anemia" not in context_only_diseases(["anemia", "chronic kidney disease"], text)


def test_the_background_half_is_still_reported_in_that_case():
    text = ("PRODUCT is indicated for the treatment of anemia in patients with "
            "chronic kidney disease, and for anemia generally.")
    assert "chronic kidney disease" in context_only_diseases(
        ["anemia", "chronic kidney disease"], text)


# ---------------------------------------------------------------------------
# The reliability gate that reads the verdict
# ---------------------------------------------------------------------------
def _pair(context_only):
    ev = {"snippet": "x", "reference": "https://example.invalid/a",
          "label_ingredient_count": 1}
    if context_only:
        ev["context_only_disease"] = True
    return {"drug_id": "CHEBI:1", "disease_id": "MONDO:1",
            "relationship_type": "INDICATION", "assertions": [{
                "source": "DAILYMED", "document": "d", "evidence": ev,
                "assertion": {"confidence": {"overall": 0.9}},
                "disease": {"resolved_id": "MONDO:1", "resolution": {"pipeline": [
                    {"category": "GROUNDING", "quality": "lexical_exact",
                     "output_value": "MONDO:1", "confidence": 0.99,
                     "confidence_basis": "MEASURED"}]}}}]}


def test_the_gate_caps_a_context_object_at_low():
    from medic.reliability import ReliabilityTier, StatementType, score_reliability
    assert score_reliability(_pair(True), StatementType.INDICATION) is ReliabilityTier.LOW


def test_the_gate_leaves_a_plain_target_alone():
    from medic.reliability import ReliabilityTier, StatementType, score_reliability
    assert score_reliability(_pair(False), StatementType.INDICATION) is ReliabilityTier.HIGH

"""A claim supported only by combination labels is not HIGH (#88, #91).

For a multi-ingredient SPL the extraction runs once per ingredient over the same section
text and the prompt never names the ingredient, so every component carries the whole
product's indications: esomeprazole to osteoarthritis (from "the naproxen component …"),
caffeine to migraine, every amino acid in a parenteral-nutrition bag to ileus.

The audit measured the consequence: pairs supported *only* by combination labels are **65%
wrong** (40 judged), against 19.5% for pairs with no combination-label support. 1,101 pairs
(14%) are in that slice, and essentially all of them are HIGH, because nothing said
otherwise (#60) and the lexical gates all pass — the disease name really is in the text.

The gate is per assertion, so the "unless another label or regulator supports it" clause
falls out of #61's max-across-assertions fold for free: a combination assertion scores LOW,
a single-ingredient or second-regulator assertion scores on its merits, and the pair takes
the better of them.
"""

from __future__ import annotations

from medic.reliability import ReliabilityTier, StatementType, score_reliability


def _assertion(ingredients=1, source="DAILYMED", quality="lexical_exact"):
    ev = {"snippet": "is indicated for invasive aspergillosis",
          "reference": f"https://example.invalid/{source}"}
    if ingredients:
        ev["label_ingredient_count"] = ingredients
    return {
        "source": source,
        "document": f"{source}:doc",
        "evidence": ev,
        "assertion": {"confidence": {"overall": 0.9}},
        "disease": {"resolved_id": "MONDO:0000240", "resolution": {"pipeline": [
            {"category": "GROUNDING", "quality": quality,
             "output_value": "MONDO:0000240",
             "confidence": 0.99, "confidence_basis": "MEASURED"}]}},
    }


def _pair(*assertions):
    return {"drug_id": "CHEBI:10023", "disease_id": "MONDO:0000240",
            "relationship_type": "INDICATION", "assertions": list(assertions)}


def _tier(*assertions):
    return score_reliability(_pair(*assertions), StatementType.INDICATION)


def test_a_single_ingredient_label_is_unaffected():
    assert _tier(_assertion(ingredients=1)) is ReliabilityTier.HIGH


def test_a_combination_only_claim_is_capped_at_low():
    assert _tier(_assertion(ingredients=4)) is ReliabilityTier.LOW


def test_a_single_ingredient_label_rescues_the_pair():
    """The issue's "unless a single-ingredient label supports it"."""
    assert _tier(_assertion(ingredients=4),
                 _assertion(ingredients=1, source="DAILYMED")) is ReliabilityTier.HIGH


def test_a_second_regulator_rescues_the_pair():
    """EMA/PMDA/CDSCO carry no per-label ingredient count, so their attestation is judged
    on its own merits — and ≥2 regulators is 7.2% wrong against 32% for one."""
    assert _tier(_assertion(ingredients=4),
                 _assertion(ingredients=0, source="EMA")) is ReliabilityTier.HIGH


def test_every_assertion_from_a_combination_label_stays_low():
    assert _tier(_assertion(ingredients=4),
                 _assertion(ingredients=7, source="DAILYMED")) is ReliabilityTier.LOW


def test_an_absent_count_is_not_treated_as_a_combination():
    """Records merged before the count was recorded must not all be demoted."""
    assert _tier(_assertion(ingredients=0)) is ReliabilityTier.HIGH


def test_the_cap_does_not_promote_a_worse_verdict():
    """A combination label whose grounding is unresolved stays EXCLUDED, not LOW."""
    assert _tier(_assertion(ingredients=4, quality="unresolved")) is ReliabilityTier.EXCLUDED

"""Two claims that differ only in context are two pairs, not one (#90.3).

The merge key was `drug|disease|relationship`, so every claim about one drug-disease pair
collapsed into a single association however differently the label qualified them. Two
consequences the issues name:

* **The clash.** formoterol is indicated for asthma *maintenance* and contraindicated for
  *acute attacks*. With context out of the key those are one indication and one
  contraindication on the same pair — 1 of the 34 self-clashes in #93, and the one that is
  genuinely both. A clash gate would have demoted a correct pair; a key that carries the
  context keeps them apart instead, which is why I declined that gate.
* **The silent merge.** "treatment of anemia in CKD" and "treatment of anemia in
  chemotherapy-induced anemia" became one row whose context was whichever record merged
  first. #24 describes this; nothing measured it.

The key stays source-free: two regulators stating the *same* qualified claim must still
merge into one pair with two assertions, which is what #61's corroboration logic rests on.
"""

from __future__ import annotations

from medic.merge.on_label_merge import _pair_key


def _record(disease="MONDO:1", rel="INDICATION", **evidence):
    return {
        "final_normalized_drug_id": "CHEBI:1",
        "final_normalized_disease_id": disease,
        "relationship_type": rel,
        "source": "DAILYMED",
        "evidence": [{"snippet": "x", **evidence}],
    }


def test_the_same_claim_with_no_context_still_merges():
    assert _pair_key(_record()) == _pair_key(_record())


def test_two_regulators_stating_the_same_qualified_claim_merge():
    """The key must stay source-free, or corroboration stops working (#61)."""
    a = _record(disease_context=["chronic kidney disease"])
    b = _record(disease_context=["chronic kidney disease"])
    b["source"] = "EMA"
    assert _pair_key(a) == _pair_key(b)


def test_different_disease_contexts_do_not_merge():
    a = _record(disease_context=["chronic kidney disease"])
    b = _record(disease_context=["chemotherapy-induced anemia"])
    assert _pair_key(a) != _pair_key(b)


def test_a_context_and_no_context_do_not_merge():
    assert _pair_key(_record(disease_context=["x"])) != _pair_key(_record())


def test_different_temporal_contexts_do_not_merge():
    a = _record(temporal_context="maintenance")
    b = _record(temporal_context="acute attacks")
    assert _pair_key(a) != _pair_key(b)


def test_different_relations_do_not_merge():
    a = _record(claim_predicate="prevents")
    b = _record(claim_predicate="treats")
    assert _pair_key(a) != _pair_key(b)


def test_different_populations_do_not_merge():
    a = _record(population_context="adults")
    b = _record(population_context="pediatric patients")
    assert _pair_key(a) != _pair_key(b)


def test_context_order_does_not_change_the_key():
    """Two records listing the same contexts in a different order are the same claim, so
    the key must not depend on list order."""
    a = _record(disease_context=["psoriasis", "Crohn disease"])
    b = _record(disease_context=["Crohn disease", "psoriasis"])
    assert _pair_key(a) == _pair_key(b)


def test_the_formoterol_clash_becomes_two_pairs():
    """The case that makes a clash gate the wrong instrument."""
    indication = _record(rel="INDICATION", temporal_context="maintenance treatment")
    contra = _record(rel="CONTRAINDICATION", temporal_context="acute attacks")
    assert _pair_key(indication) != _pair_key(contra)


def test_a_record_with_no_ids_is_still_rejected():
    assert _pair_key({"relationship_type": "INDICATION"}) is None

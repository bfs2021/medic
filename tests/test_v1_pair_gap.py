"""A pair asserted at a neighbouring specificity is not a missing pair (Lever 3).

A strict id-or-label join counts a v1 pair as absent whenever the two releases named the
disease at a different level of specificity. Measured, that is 20.2% of the apparent gap:

    v1 "urinary tract infection"      ours "Recurrent urinary tract infections"
    v1 "Acute insomnia"               ours "Insomnia"
    v1 "metastatic colorectal cancer" ours "Colorectal cancer"

Two of the classes are not losses at all. **ancestor**: we hold a parent of v1's disease for
the same drug, because v1 propagated indications *down* the Mondo hierarchy and
`docs/architecture.md` §5.7 records that this was removed deliberately — annotations travel
up a hierarchy, not down. **descendant**: we assert something more specific than v1 did, and
scoring that as a loss punishes an improvement.

This matters beyond bookkeeping: reporting the strict figure alone overstated the regression
repeatedly, and the same looser-matcher-than-you-report-against mistake also produced an
"apparent 41% recall loss" (really 25%), a "41.2% drug coverage" (really 64.9%) and a sample
probe predicting 60.3% pair recovery where the strict diff then measured 0%.
"""

from __future__ import annotations

from scripts.v1_pair_gap import RELATED, ancestors, classify, norm

# A small slice of Mondo: UTI is a kind of urinary tract disease; recurrent UTI is a kind
# of UTI. Shapes the real hierarchy, small enough to reason about.
PARENTS = {
    "MONDO:UTI": {"MONDO:URINARY"},
    "MONDO:RECURRENT_UTI": {"MONDO:UTI"},
    "MONDO:URINARY": {"MONDO:DISEASE"},
    "MONDO:INSOMNIA_ACUTE": {"MONDO:INSOMNIA"},
}


def test_ancestors_walks_up_and_stops():
    assert ancestors("MONDO:RECURRENT_UTI", PARENTS) == {
        "MONDO:UTI", "MONDO:URINARY", "MONDO:DISEASE"}
    assert ancestors("MONDO:DISEASE", PARENTS) == set()


def test_depth_is_bounded():
    """Mondo is deep; past a few steps an 'ancestor' is too general to mean the releases
    agreed about anything."""
    assert ancestors("MONDO:RECURRENT_UTI", PARENTS, depth=1) == {"MONDO:UTI"}


def _v1(dis_id, dis, drug="ceftizoxime", drug_id="CHEBI:1"):
    return [{"drug": drug, "drug_id": drug_id, "dis": dis, "dis_id": dis_id}]


def _now(dis_id, dis, drug="ceftizoxime", drug_id="CHEBI:1", tier="HIGH"):
    return [{"drug": drug, "drug_id": drug_id, "dis": dis, "dis_id": dis_id, "tier": tier}]


def test_an_identical_pair_is_exact():
    counts, _ = classify(_v1("MONDO:UTI", "UTI"), _now("MONDO:UTI", "UTI"), PARENTS)
    assert counts["exact"] == 1


def test_the_same_pair_under_a_different_id_is_requalified():
    counts, _ = classify(_v1("MONDO:UTI", "urinary tract infection"),
                         _now("UMLS:C1", "Urinary Tract Infection"), PARENTS)
    assert counts["requalified"] == 1, dict(counts)


def test_we_hold_a_parent_so_v1_had_downfilled():
    """v1 asserted the subtype; we assert the parent it was derived from."""
    counts, _ = classify(_v1("MONDO:RECURRENT_UTI", "recurrent UTI"),
                         _now("MONDO:UTI", "urinary tract infection"), PARENTS)
    assert counts["ancestor"] == 1, dict(counts)
    assert counts["absent"] == 0


def test_we_hold_a_child_so_we_are_more_specific():
    counts, _ = classify(_v1("MONDO:UTI", "urinary tract infection"),
                         _now("MONDO:RECURRENT_UTI", "recurrent UTI"), PARENTS)
    assert counts["descendant"] == 1, dict(counts)


def test_a_wording_difference_outside_the_hierarchy_is_its_own_class():
    """Not every near-match is an is_a edge — "Nutritional anemia" against "anemia,
    nutritional" has no hierarchy relation to find, so it needs a separate bucket rather
    than being counted as a loss or silently folded into one of the others."""
    counts, examples = classify(
        _v1("MONDO:X", "Nutritional anemia"),
        _now("MONDO:Y", "nutritional anemia, unspecified"), PARENTS)
    assert counts["wording"] == 1, dict(counts)
    assert examples and examples[0][0] == "wording"


def test_an_unrelated_disease_is_genuinely_absent():
    counts, _ = classify(_v1("MONDO:UTI", "urinary tract infection"),
                         _now("MONDO:ASTHMA", "asthma"), PARENTS)
    assert counts["absent"] == 1, dict(counts)


def test_a_drug_we_never_mention_is_its_own_class():
    """Distinct from 'absent': nothing to compare, rather than a disagreement about which
    disease. The two have different remedies — acquisition versus extraction."""
    counts, _ = classify(
        _v1("MONDO:UTI", "UTI", drug="trovafloxacin", drug_id="CHEBI:TROVA"),
        _now("MONDO:UTI", "UTI", drug="aspirin", drug_id="CHEBI:ASA"), PARENTS)
    assert counts["drug_absent"] == 1, dict(counts)


def test_the_same_drug_id_is_the_same_drug_whatever_the_label():
    """Guard on the exact check: it keys on (drug_id, disease_id), so a label difference
    must not break an id match. The first version of the test above gave two different drugs
    the same CHEBI id and was therefore meaningless."""
    counts, _ = classify(_v1("MONDO:UTI", "UTI", drug="cortisol"),
                         _now("MONDO:UTI", "UTI", drug="hydrocortisone"), PARENTS)
    assert counts["exact"] == 1, dict(counts)


def test_the_related_classes_are_the_ones_excluded_from_loss():
    assert set(RELATED) == {"ancestor", "descendant", "wording"}


def test_norm_ignores_case_and_punctuation():
    assert norm("Urinary Tract Infection") == norm("urinary-tract infection")
    assert norm(None) == ""

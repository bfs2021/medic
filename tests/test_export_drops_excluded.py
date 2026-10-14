"""The export does not ship claims the pipeline has already judged wrong (#93).

14 `biolink:treats` edges in the 2026-10-13 export carried
`medic_negation_detected: true` / `medic_assertion_flags: [negated_inversion]`, and 13 of
them `medic_pair_reliability: EXCLUDED` — acetylsalicylic acid to acute MI, six insulins
and oral antidiabetics to diabetic ketoacidosis ("not for the treatment of diabetic
ketoacidosis"), roflumilast to acute bronchospasm. The pipeline had decided these were
inversions and the exporter emitted them with the same predicate as everything else.

Dropped rather than flagged. Biolink's `negated` slot is valid, but a consumer that ignores
it sees a positive `treats` edge, which is the same failure as leaving it to them to read
`medic_pair_reliability`. The claims stay in `kb/` and in the products with their reason
attached — that is what #66 traded the ingest-time drop for — so nothing becomes
unauditable, and a curator can still overturn the verdict through the review store.

The gate is per assertion, not per pair: 1 of the 14 had a non-EXCLUDED pair tier.
"""

from __future__ import annotations

import pytest

from medic.export.kgx import edges


def _assertion(flags=(), excluded=False, source="EMA"):
    """A well-formed attestation that scores HIGH unless asked not to.

    The shared KGX fixture leaves `output_value` off its GROUNDING steps, which
    `_grounding_tier_from_step` reads as an unresolved grounding and scores EXCLUDED. That
    is right for the scorer and wrong for a fixture meant to represent a good edge, so the
    steps are completed here. `excluded=True` then re-creates the bad case deliberately.
    """
    from tests.test_kgx_export import _assertion as base
    a = base(source=source)
    a.setdefault("assertion", {})["flags"] = list(flags)
    for key in ("drug", "disease"):
        mention = a.get(key) or {}
        for step in ((mention.get("resolution") or {}).get("pipeline") or []):
            if step.get("category") == "GROUNDING":
                step["output_value"] = (
                    "" if excluded else (mention.get("resolved_id") or "MONDO:0000240"))
                step.setdefault("confidence", 0.99)
                step.setdefault("confidence_basis", "MEASURED")
    return a


def _pair(assertions, reliability="HIGH"):
    return {"drug_id": "CHEBI:15365", "drug_label": "acetylsalicylic acid",
            "disease_id": "MONDO:0005068", "disease_label": "myocardial infarction",
            "relationship_type": "INDICATION", "reliability": reliability,
            "assertions": list(assertions)}


def test_a_negated_inversion_assertion_is_not_exported():
    built, _ = edges.build_edges(
        [_pair([_assertion(flags=["negated_inversion"])], reliability="EXCLUDED")],
        [], [], [])
    assert built == []


def test_the_gate_is_per_assertion_not_per_pair():
    """1 of the 14 had a pair tier other than EXCLUDED, so a pair-level gate misses it."""
    built, _ = edges.build_edges(
        [_pair([_assertion(flags=["negated_inversion"])], reliability="HIGH")],
        [], [], [])
    assert built == []


def test_a_clean_assertion_on_the_same_pair_still_exports():
    """One source inverting the claim must not delete another source's good attestation."""
    pair = _pair([_assertion(flags=["negated_inversion"], source="EMA"),
                  _assertion(source="PMDA")], reliability="HIGH")
    built, _ = edges.build_edges([pair], [], [], [])
    assert len(built) == 1
    assert built[0]["primary_knowledge_source"] == "infores:pmda"


def test_an_ordinary_pair_is_untouched():
    built, _ = edges.build_edges([_pair([_assertion()])], [], [], [])
    assert len(built) == 1
    assert built[0]["predicate"] == "biolink:treats"


@pytest.mark.parametrize("flag", ["over_extraction", "wrong_section", "polarity_unverified"])
def test_other_flags_do_not_drop_the_edge(flag):
    """Only a verdict that the claim is *inverted* is grounds for withholding it. The
    others mean weak or unchecked, which the tier already expresses."""
    built, _ = edges.build_edges([_pair([_assertion(flags=[flag])])], [], [], [])
    assert len(built) == 1


def test_an_excluded_tier_alone_does_not_withhold_the_edge():
    """Deliberately narrower than "drop EXCLUDED pairs". EXCLUDED is reachable by an
    unresolved grounding and by a curator REJECT, and #62 decided a REJECT should mark the
    edge rather than delete it so the verdict stays observable. Over the current products
    the two sets are the same 14 assertions, so the narrow rule loses nothing."""
    built, _ = edges.build_edges([_pair([_assertion(excluded=True)])], [], [], [])
    assert len(built) == 1
    assert built[0]["medic_reliability"] == "EXCLUDED"

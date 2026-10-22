"""Tests for statement typing + the source-uniform reliability score."""

from __future__ import annotations

from medic.reliability import (
    CORE_TYPES,
    ReliabilityTier,
    StatementReviewStore,
    StatementType,
    classify_statement,
    is_reliable,
    score_reliability,
    statement_key,
)


# ---------------------------------------------------------------------------
# classify_statement
# ---------------------------------------------------------------------------
def test_classify_indication_and_contraindication():
    assert classify_statement({"relationship_type": "INDICATION"}) == StatementType.INDICATION
    assert classify_statement({"relationship_type": "CONTRAINDICATION"}) == StatementType.CONTRAINDICATION


def test_classify_drug_approval():
    rec = {
        "identity": {"resolved_id": "CHEBI:100147"},
        "approvals": [{"authority": "FDA", "status": "APPROVED"}],
    }
    assert classify_statement(rec) == StatementType.DRUG_APPROVAL


def test_classify_research_association():
    assert classify_statement({"drug_id": "CHEBI:1", "disease_id": "MONDO:1"}) == \
        StatementType.RESEARCH_ASSOCIATION
    assert classify_statement({"deep_research_used": True}) == StatementType.RESEARCH_ASSOCIATION


def test_classify_adverse_event():
    assert classify_statement({"meddra_id": "MEDDRA:1", "drug_id": "CHEBI:1"}) == \
        StatementType.ADVERSE_EVENT


def test_core_types_membership():
    assert StatementType.DRUG_APPROVAL in CORE_TYPES
    assert StatementType.RESEARCH_ASSOCIATION not in CORE_TYPES


# ---------------------------------------------------------------------------
# reliability gates
# ---------------------------------------------------------------------------
def _indication(disease="epilepsy", snippet="indicated for epilepsy", *, quality="lexical_exact",
                conf=1.0, url="http://doc.pdf", translation=None):
    rec = {
        "relationship_type": "INDICATION",
        "disease_grounding": {"grounding_quality": quality, "grounded_id": "MONDO:1", "confidence": conf},
        "evidence": [{
            "original_disease_label": disease, "snippet": snippet,
            "source_document_url": url, "reference": "DailyMed:x",
        }],
    }
    if translation is not None:
        rec["translation"] = translation
    return rec


def test_clean_indication_is_high():
    assert score_reliability(_indication()) == ReliabilityTier.HIGH


def test_unresolved_grounding_is_excluded():
    rec = _indication(quality="unresolved")
    rec["disease_grounding"]["grounded_id"] = None
    assert score_reliability(rec) == ReliabilityTier.EXCLUDED


def test_fuzzy_grounding_caps_at_low():
    # inexact grounding at fuzzy confidence (~0.6) drags the whole statement to LOW.
    assert score_reliability(_indication(quality="lexical_exact_surgery", conf=0.6)) == \
        ReliabilityTier.LOW


def test_negated_indication_is_excluded():
    rec = _indication(disease="asthma", snippet="not indicated for asthma")
    assert score_reliability(rec) == ReliabilityTier.EXCLUDED


def test_hallucinated_disease_caps_at_low():
    rec = _indication(disease="lung cancer", snippet="indicated for epilepsy")
    assert score_reliability(rec) == ReliabilityTier.LOW  # entailment 0 -> LOW gate


def test_machine_translation_caps_at_medium():
    rec = _indication(translation={"translation_status": "CANDIDATE", "translation_value": "x"})
    assert score_reliability(rec) == ReliabilityTier.MEDIUM


def test_official_translation_stays_high():
    rec = _indication(translation={"translation_status": "OFFICIAL", "translation_value": "x"})
    assert score_reliability(rec) == ReliabilityTier.HIGH


def test_china_only_drug_is_translation_capped_medium():
    rec = {
        "curie": "CHEBI:1", "approved_china": True,
        "grounding": {"grounding_quality": "lexical_exact", "grounded_id": "CHEBI:1"},
        "translation": {"translation_status": "CANDIDATE", "translation_value": "x"},
        "evidence": [{"approval_status": "APPROVED", "reference": "cde"}],
    }
    assert score_reliability(rec, StatementType.DRUG_APPROVAL) == ReliabilityTier.MEDIUM


def test_multijurisdiction_drug_not_translation_capped():
    # A US drug also registered in China borrows a machine-translation block at merge; its
    # identity is anchored by Orange Book, so it must NOT be dragged to MEDIUM.
    #
    # The current product shape: approvals[].authority, not the flat `approved_usa` boolean
    # SPEC §9 records as removed. 4,021 of 4,323 drugs carry `approvals`; none carry the
    # booleans this test used to assert against.
    rec = {
        "curie": "CHEBI:1",
        "approvals": [{"authority": "FDA", "source": "ORANGEBOOK", "status": "APPROVED"},
                      {"authority": "NMPA_CHINA", "source": "CDE_CHINA", "status": "APPROVED"}],
        "grounding": {"grounding_quality": "lexical_exact", "grounded_id": "CHEBI:1"},
        "translation": {"translation_status": "CANDIDATE", "translation_value": "x"},
        "evidence": [{"approval_status": "APPROVED", "reference": "accessdata.fda.gov"}],
    }
    assert score_reliability(rec, StatementType.DRUG_APPROVAL) == ReliabilityTier.HIGH


def test_translation_step_is_not_capped_by_a_native_approval():
    """The live path: 758 drugs carry a TranslationStep on their identity trail.

    `_has_native_approval` used to read a pair-level `regulatory_status` key the provenance
    re-model deleted, so it returned False for every record and this escape hatch never
    opened.
    """
    rec = {
        "curie": "CHEBI:1",
        "approvals": [{"authority": "FDA", "source": "ORANGEBOOK", "status": "APPROVED"}],
        "identity": {
            "original_literal": "阿司匹林", "entity_type": "drug", "resolved_id": "CHEBI:1",
            "resolution": {"pipeline": [
                {"category": "TRANSLATION", "input_value": "阿司匹林",
                 "output_value": "aspirin", "status": "CANDIDATE"},
                {"category": "GROUNDING", "input_value": "aspirin", "output_value": "CHEBI:1",
                 "quality": "lexical_exact"},
            ]},
        },
        "evidence": [{"approval_status": "APPROVED", "reference": "accessdata.fda.gov"}],
    }
    assert score_reliability(rec, StatementType.DRUG_APPROVAL) == ReliabilityTier.HIGH


def test_an_association_native_authority_comes_from_its_assertions():
    """An FDA-backed indication must not be translation-capped.

    Authorities live on `assertions[].regulatory_status` now, so this only works by going
    through `product_view.assoc_authorities`.
    """
    from medic.reliability import _has_native_approval

    assoc = {"drug_id": "CHEBI:1", "disease_id": "MONDO:1", "assertions": [
        {"source": "DAILYMED", "document": "DailyMed:a",
         "regulatory_status": {"authority": "FDA", "source": "DAILYMED", "status": "APPROVED"}},
    ]}
    assert _has_native_approval(assoc) is True
    assert _has_native_approval({"assertions": [{"source": "GRLS", "document": "d"}]}) is False


def test_no_direct_url_is_not_penalised():
    # Fairness: absence of a deep link (a source publishing convention) must NOT cap the
    # tier — a reference/snippet is enough provenance.
    rec = _indication(url=None)
    rec["evidence"][0].pop("source_document_url", None)
    assert score_reliability(rec) == ReliabilityTier.HIGH


def test_no_provenance_at_all_is_low():
    rec = _indication()
    rec["evidence"] = []  # nothing to verify against
    assert score_reliability(rec) == ReliabilityTier.LOW


def test_human_confirmation_forces_high():
    # The review escape hatch: even fuzzy grounding is HIGH once a curator confirms it.
    rec = _indication(quality="lexical_exact_surgery", conf=0.6)
    rec["review_status"] = "CONFIRMED"
    assert score_reliability(rec) == ReliabilityTier.HIGH


def test_orange_book_style_approval_reaches_high_without_deeplink():
    # Fairness: a well-grounded registry approval with only a detail-page reference
    # (Orange Book has no per-NDA PDF) must be able to reach HIGH.
    rec = {
        "curie": "CHEBI:100147", "approved_usa": True,
        "grounding": {"grounding_quality": "lexical_exact", "grounded_id": "CHEBI:100147",
                      "confidence": 0.85},
        "evidence": [{"approval_status": "APPROVED",
                      "reference": "https://www.accessdata.fda.gov/scripts/cder/daf/..."}],
    }
    assert score_reliability(rec) == ReliabilityTier.HIGH


def test_unapproved_drug_entry_is_excluded():
    # A drug-list entry with no approval anywhere is not an approval statement.
    rec = {"curie": "CHEBI:10056", "approved_usa": False,
           "evidence": [{"reference": "x"}]}
    assert score_reliability(rec, StatementType.DRUG_APPROVAL) == ReliabilityTier.EXCLUDED


def test_review_reject_forces_excluded_over_clean_record():
    # A curator can KILL an otherwise-HIGH record.
    assert score_reliability(_indication(), review_status="REJECTED") == ReliabilityTier.EXCLUDED


# ---------------------------------------------------------------------------
# reliability gates reading the transformation-provenance trail (mention steps),
# with NO flat fields present (the post-rebuild shape)
# ---------------------------------------------------------------------------
def test_step_grounding_exact_drug_is_high():
    rec = {
        "approvals": [{"authority": "FDA", "source": "ORANGEBOOK", "status": "APPROVED"}],
        "identity": {"id": "MEDICNE:x", "original_literal": "ASPIRIN", "resolution": {"pipeline": [
            {"category": "GROUNDING", "input_value": "ASPIRIN", "output_value": "CHEBI:15365",
             "method": "LEXICAL_MATCH", "quality": "lexical_exact"}]}},
        "evidence": [{"approval_status": "APPROVED", "reference": "accessdata.fda.gov"}],
    }
    assert score_reliability(rec, StatementType.DRUG_APPROVAL) == ReliabilityTier.HIGH


def _step_grounded(*, quality, conf, predicate=None, rule=None):
    """A drug-approval record whose only grounding step is the one under test."""
    step = {"category": "GROUNDING", "input_value": "fentanyl citrate",
            "output_value": "CHEBI:119915", "method": "LEXICAL_MATCH",
            "quality": quality, "confidence": conf}
    if predicate:
        step["predicate_id"] = predicate
    if rule:
        step["applied_rules"] = [rule]
    return {
        "approvals": [{"authority": "FDA", "source": "ORANGEBOOK", "status": "APPROVED"}],
        "identity": {"id": "MEDICNE:z", "original_literal": "fentanyl citrate",
                     "resolution": {"pipeline": [step]}},
        "evidence": [{"approval_status": "APPROVED", "reference": "accessdata.fda.gov"}],
    }


def test_a_salt_stripped_to_its_parent_caps_at_medium():
    """`salt_ester_strip` is scored 0.90 and the HIGH threshold reads `conf >= 0.9`, so a
    closeMatch sat exactly on the boundary and reached the top tier although the comment in
    `_grounding_tier` says a salt match should be MEDIUM. Latent while the rule only fired
    for salts missing from the index (174 steps); #102's tier 0 fires it for the 617 that
    are present, so the boundary had to be settled before taking that change."""
    rec = _step_grounded(quality="lexical_exact_surgery", conf=0.90,
                         predicate="skos:closeMatch", rule="salt_ester_strip")
    assert score_reliability(rec, StatementType.DRUG_APPROVAL) == ReliabilityTier.MEDIUM


def test_a_high_certainty_exact_match_still_reaches_high():
    """The cap is on the *kind* of match, not on confidence: an exactMatch rule at the same
    certainty is unaffected, so this does not quietly demote the whole inexact tier."""
    rec = _step_grounded(quality="lexical_exact_surgery", conf=0.92,
                         predicate="skos:exactMatch", rule="combination_split")
    assert score_reliability(rec, StatementType.DRUG_APPROVAL) == ReliabilityTier.HIGH


def test_a_broad_match_caps_at_medium_too():
    rec = _step_grounded(quality="lexical_exact_surgery", conf=0.95,
                         predicate="skos:broadMatch", rule="qualifier_strip")
    assert score_reliability(rec, StatementType.DRUG_APPROVAL) == ReliabilityTier.MEDIUM


def test_a_step_with_no_predicate_is_left_to_the_thresholds():
    """A missing predicate means the record predates the field, not that the match was
    inexact — demoting on an absent value would rescore history on a guess."""
    rec = _step_grounded(quality="lexical_exact_surgery", conf=0.95)
    assert score_reliability(rec, StatementType.DRUG_APPROVAL) == ReliabilityTier.HIGH


def test_the_flat_grounding_path_caps_the_same_way():
    """Both readers have to agree, or a record scores differently depending on which shape
    it happens to be in mid-migration."""
    rec = _indication(quality="lexical_exact_surgery", conf=0.90)
    rec["disease_grounding"]["predicate_id"] = "skos:closeMatch"
    assert score_reliability(rec) == ReliabilityTier.MEDIUM


def test_step_translation_candidate_china_drug_is_medium():
    rec = {
        "approvals": [{"authority": "NMPA_CHINA", "source": "CDE_CHINA", "status": "APPROVED"}],
        "identity": {"id": "MEDICNE:y", "original_literal": "坎地沙坦酯片", "resolution": {"pipeline": [
            {"category": "TRANSLATION", "input_value": "坎地沙坦酯片", "output_value": "Candesartan",
             "method": "API", "status": "CANDIDATE", "flags": ["unreviewed_machine"]},
            {"category": "GROUNDING", "input_value": "Candesartan", "output_value": "CHEBI:3348",
             "method": "LEXICAL_MATCH", "quality": "lexical_exact"}]}},
        "evidence": [{"approval_status": "APPROVED", "reference": "cde"}],
    }
    assert score_reliability(rec, StatementType.DRUG_APPROVAL) == ReliabilityTier.MEDIUM


def _assoc_with(assertion: dict, *, ext_flags: list | None = None) -> dict:
    return {
        "relationship_type": "INDICATION",
        "drug": {"resolved_id": "CHEBI:1", "resolved_label": "d",
                 "original_literal": "d", "entity_type": "drug"},
        "disease": {"id": "MEDICNE:z", "original_literal": "gout", "resolution": {"pipeline": [
            {"category": "EXTRACTION", "input_value": "…gout…", "output_value": "gout",
             "method": "LLM", "confidence": 1.0, "flags": ext_flags or []},
            {"category": "GROUNDING", "input_value": "gout", "output_value": "MONDO:1",
             "method": "LEXICAL_MATCH", "quality": "lexical_exact"}]}},
        "assertion": assertion,
        "evidence": [{"snippet": "…gout…", "reference": "DailyMed:x"}],
    }


def test_assertion_negated_flag_excludes():
    # Negation is a CLAIM-level failure: it lives on the assertion, and it excludes.
    rec = _assoc_with({"confidence": 1.0, "flags": ["negated_inversion"]})
    assert score_reliability(rec) == ReliabilityTier.EXCLUDED


def test_assertion_over_extraction_caps_low():
    # The VITAMIN A case: the entity is recognised perfectly (confidence 1.0) but the
    # source does not assert THIS relation -> the claim gate drops it.
    rec = _assoc_with({"confidence": 1.0, "flags": ["over_extraction"]})
    assert score_reliability(rec) == ReliabilityTier.LOW


def test_clean_assertion_is_high():
    assert score_reliability(_assoc_with({"confidence": 1.0, "flags": []})) == ReliabilityTier.HIGH


def test_recognition_hallucination_excludes():
    # A hallucinated mention is unusable regardless of what the claim says.
    rec = _assoc_with({"confidence": 1.0, "flags": []}, ext_flags=["hallucination"])
    assert score_reliability(rec) == ReliabilityTier.EXCLUDED


# ---------------------------------------------------------------------------
# statement_key + review store
# ---------------------------------------------------------------------------
def test_statement_key_shapes():
    # v3.0: identity is at the pair level; the inlined Mentions live on assertions[].
    ind = {"relationship_type": "INDICATION", "drug_id": "CHEBI:1", "disease_id": "MONDO:1"}
    assert statement_key(ind) == "CHEBI:1|MONDO:1|INDICATION"
    appr = {"identity": {"resolved_id": "CHEBI:2"},
            "approvals": [{"authority": "FDA", "status": "APPROVED"}]}
    assert statement_key(appr) == "CHEBI:2|DRUG_APPROVAL"


def test_review_store_confirms_and_rejects(tmp_path):
    ind = _indication(quality="lexical_exact_surgery", conf=0.6)  # would be LOW
    ind["final_normalized_drug_id"] = "CHEBI:1"
    ind["final_normalized_disease_id"] = "MONDO:1"
    key = statement_key(ind)

    path = tmp_path / "statement_review.tsv"
    path.write_text(
        "statement_key\tstatement_type\tdrug_id\tdisease_id\treview_status\treviewer\tcomment\n"
        f"{key}\tINDICATION\tCHEBI:1\tMONDO:1\tCONFIRMED\tnico\tverified vs SmPC\n"
    )
    store = StatementReviewStore(str(path)).load()
    assert store.status(ind) == "CONFIRMED"
    assert score_reliability(ind, review_status=store.status(ind)) == ReliabilityTier.HIGH


def test_review_store_missing_file_is_noop(tmp_path):
    store = StatementReviewStore(str(tmp_path / "nope.tsv")).load()
    assert store.status(_indication()) == ""


# ---------------------------------------------------------------------------
# is_reliable
# ---------------------------------------------------------------------------
def test_is_reliable_core_high():
    assert is_reliable(_indication()) is True


def test_is_reliable_excludes_non_core_by_default():
    research = {"drug_id": "CHEBI:1", "disease_id": "MONDO:1", "evidence": [{"snippet": "s"}]}
    assert is_reliable(research) is False           # non-core filtered out
    assert is_reliable(research, core_only=False) in (True, False)  # scored on its own merits


def test_is_reliable_rejects_low():
    assert is_reliable(_indication(quality="lexical_exact_surgery", conf=0.6)) is False

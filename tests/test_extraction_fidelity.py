"""Tests for the extraction-fidelity validator (snippet-entailment)."""

from __future__ import annotations

import yaml

from medic.validation.extraction_fidelity import (
    assertion_negated,
    check_record,
    entailment_score,
    screen_indications,
    validate_file,
)


# ---------------------------------------------------------------------------
# entailment_score
# ---------------------------------------------------------------------------
def test_verbatim_disease_is_fully_entailed():
    assert entailment_score("epilepsy", "newly diagnosed epilepsy") == 1.0
    assert entailment_score("myoclonic seizures", "treatment of myoclonic seizures in adults") == 1.0


def test_llm_canonicalization_scores_partial_but_passes_default_threshold():
    # "type 2 diabetes mellitus" over a label saying "type 2 diabetes": 3/4 tokens present.
    score = entailment_score("type 2 diabetes mellitus", "indicated for type 2 diabetes")
    assert 0.5 <= score < 1.0


def test_hallucinated_disease_scores_zero():
    assert entailment_score("rheumatoid arthritis", "indicated for type 2 diabetes") == 0.0


def test_synonym_the_source_spells_differently_scores_zero():
    # Known false-positive class: flagged for review, not asserted wrong.
    assert entailment_score("high blood pressure", "treatment of hypertension") == 0.0


def test_empty_inputs_score_zero():
    assert entailment_score("", "some text") == 0.0
    assert entailment_score("epilepsy", "") == 0.0


def test_stopwords_do_not_inflate_score():
    # Only "cancer" is a content token; it is absent -> 0, not rescued by "of/the".
    assert entailment_score("cancer of the lung", "the treatment of the condition") == 0.0


# ---------------------------------------------------------------------------
# check_record
# ---------------------------------------------------------------------------
def _record(disease_label, snippet, relationship="INDICATION", section_text=None):
    rec = {
        "relationship_type": relationship,
        "evidence": [{
            "original_disease_label": disease_label,
            "original_drug_label": "somedrug",
            "snippet": snippet,
            "reference": "DailyMed:xyz",
        }],
    }
    if section_text is not None:
        rec["indications_text"] = section_text
    return rec


def test_check_record_scores_each_evidence_disease():
    rec = _record("epilepsy", "indicated for epilepsy in adults")
    findings = check_record(rec)
    assert len(findings) == 1
    assert findings[0]["score"] == 1.0
    assert findings[0]["relationship_type"] == "INDICATION"


def test_check_record_flags_hallucination():
    rec = _record("lung cancer", "indicated for the treatment of epilepsy")
    assert check_record(rec)[0]["score"] == 0.0


def test_check_record_folds_in_full_section_text_to_avoid_truncation_false_flag():
    # snippet is truncated and lacks the disease; the full section_text contains it.
    rec = _record("psoriasis", snippet="indicated for ...", section_text="approved for psoriasis")
    assert check_record(rec)[0]["score"] == 1.0


def test_check_record_skips_when_no_source_text():
    rec = _record("epilepsy", snippet="")
    assert check_record(rec) == []  # nothing to judge against


# ---------------------------------------------------------------------------
# assertion_negated (polarity)
# ---------------------------------------------------------------------------
def test_negation_direct():
    assert assertion_negated("asthma", "Not indicated for the treatment of asthma.")[:2] == (1, 1)


def test_negation_exception_clause():
    text = "Indicated for plaque psoriasis, except in patients with active tuberculosis."
    assert assertion_negated("tuberculosis", text)[:2] == (1, 1)      # excluded
    assert assertion_negated("plaque psoriasis", text)[:2] == (0, 1)  # the real indication


def test_negation_but_not():
    assert assertion_negated("hepatitis", "Indicated for cirrhosis but not hepatitis.")[:2] == (1, 1)


def test_positive_indication_not_negated():
    assert assertion_negated("epilepsy", "Keppra is indicated for epilepsy.")[:2] == (0, 1)


def test_negation_respects_sentence_boundary():
    # The negation in the second sentence must not taint the first.
    assert assertion_negated("copd", "Indicated for COPD. Not indicated for asthma.")[:2] == (0, 1)


def test_unlocatable_disease_is_not_evaluable():
    assert assertion_negated("hypertension", "treatment of high blood pressure") == (0, 0, "")


def test_check_record_flags_negated_indication():
    rec = _record("type 1 diabetes", "Should not be used in patients with type 1 diabetes.")
    finding = check_record(rec)[0]
    assert finding["negated"] is True
    assert finding["negation_reason"]  # a cue was recorded (e.g. "should not" / "not be used")


def test_check_record_does_not_flag_positive_indication():
    rec = _record("epilepsy", "Indicated for epilepsy.")
    assert check_record(rec)[0]["negated"] is False


def test_contraindication_polarity_not_evaluated():
    # A contraindication section is negative by nature; polarity check only guards indications.
    rec = _record("hepatic impairment", "Contraindicated in severe hepatic impairment.",
                  relationship="CONTRAINDICATION")
    assert check_record(rec)[0]["negated"] is False


# ---------------------------------------------------------------------------
# screen_indications (ingest-time drop)
# ---------------------------------------------------------------------------
def test_screen_drops_negated_keeps_positive():
    text = "Indicated for hypertension. Should not be used in patients with type 1 diabetes."
    result = screen_indications(["hypertension", "type 1 diabetes"], text)
    assert result.kept == ["hypertension"]
    assert [d["disease"] for d in result.dropped] == ["type 1 diabetes"]
    assert result.dropped[0]["reason"]  # a cue was recorded


def test_screen_keeps_unlocatable_disease():
    # A synonym the source spells differently can't be judged -> kept, not dropped. It is
    # reported in the `unlocatable` bucket so "not checked" stays distinguishable from
    # "checked and clean" (issue #59).
    result = screen_indications(["hypertension"], "treatment of high blood pressure")
    assert result.kept == ["hypertension"]
    assert result.dropped == []
    assert result.unlocatable == ["hypertension"]


def test_screen_does_not_drop_on_shared_head_word():
    # "vertebral, but not hip fractures": 'vertebral fractures' is a real indication; the
    # negation belongs to 'hip fractures', which merely shares the head word 'fractures'.
    # A destructive drop must not fire here (regression for the raloxifene over-drop).
    text = "A significant reduction in the incidence of vertebral, but not hip fractures."
    result = screen_indications(["vertebral fractures"], text)
    assert result.kept == ["vertebral fractures"]
    assert result.dropped == []


def test_screen_keeps_disease_mentioned_both_ways():
    text = "Indicated for epilepsy; not indicated for febrile epilepsy in some patients."
    # "epilepsy" appears positively (first clause) and inside a negation -> not all negated -> kept.
    assert screen_indications(["epilepsy"], text).kept == ["epilepsy"]


def test_extractor_drops_negated_indication_via_cache(monkeypatch):
    """extract_diseases_from_text screens negated diseases out of a cached extraction."""
    from medic.ingest.dailymed import __main__ as dm

    text = "Indicated for hypertension. Should not be used in type 1 diabetes."

    class _StubCache:
        def get(self, key):
            # Cache form since the structured contract: the model's raw answer, re-parsed
            # on read so the parser can improve without re-asking.
            return {"raw": ("target: hypertension :: hypertension\n"
                            "predicate: treats\n"
                            "---\n"
                            "target: type 1 diabetes :: type 1 diabetes\n"
                            "predicate: treats")}

        def put(self, key, value):
            pass

    monkeypatch.setattr(dm, "_get_disease_cache", lambda: _StubCache())
    kept = dm.extract_diseases_from_text(text)
    assert [c.target for c in kept] == ["hypertension"]


# ---------------------------------------------------------------------------
# validate_file
# ---------------------------------------------------------------------------
def test_validate_file_counts_flagged(tmp_path):
    records = [
        _record("epilepsy", "indicated for epilepsy"),          # entailed
        _record("rheumatoid arthritis", "indicated for asthma"),  # hallucinated
    ]
    path = tmp_path / "indications.yaml"
    path.write_text(yaml.safe_dump(records))

    result = validate_file(str(path), threshold=0.5)
    assert result["checked"] == 2
    assert len(result["flagged"]) == 1
    assert result["flagged"][0]["disease_label"] == "rheumatoid arthritis"
    assert result["flagged"][0]["score"] == 0.0


def test_validate_file_collects_negated(tmp_path):
    records = [
        _record("epilepsy", "indicated for epilepsy"),                       # positive
        _record("asthma", "not indicated for asthma", section_text="not indicated for asthma"),
    ]
    path = tmp_path / "indications.yaml"
    path.write_text(yaml.safe_dump(records))

    result = validate_file(str(path), threshold=0.5)
    assert [f["disease_label"] for f in result["negated"]] == ["asthma"]

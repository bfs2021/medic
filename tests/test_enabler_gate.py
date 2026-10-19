"""Enabler partners never own a treatment indication (#88, FAILURE_MODES 5.4).

A combination label split per ingredient credits every component with the product's
indication. For beta-lactamase inhibitors, cilastatin and cobicistat that credit is always
misplaced: "avibactam treats pyelonephritis" is ceftazidime's claim. The rule is a class
rule keyed on the drug name (``mappings/enabler_partners.tsv``), applied twice — as the
``wrong_pairing`` assertion flag at merge time, and as ``_enabler_gate`` at score time — and
it leaves contraindications alone, because those are real claims about the component.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from medic.enabler_partners import (
    ENABLER_CLASSES,
    ENABLER_PARTNERS_PATH,
    enabler_class,
    is_enabler_partner,
    load_enabler_partners,
)
from medic.merge.on_label_merge import _build_source_assertions
from medic.reliability import ReliabilityTier, StatementType, _enabler_gate, score_reliability


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------
def test_store_loads_and_every_row_has_a_known_class():
    assert ENABLER_PARTNERS_PATH == Path("mappings/enabler_partners.tsv").resolve()
    table = load_enabler_partners()
    assert table, "mappings/enabler_partners.tsv loaded empty"
    assert set(table.values()) <= ENABLER_CLASSES
    with open(ENABLER_PARTNERS_PATH, newline="") as fh:
        rows = list(csv.DictReader((ln for ln in fh if not ln.startswith("#")), delimiter="\t"))
    assert [r["name"].lower() for r in rows] == list(table)  # nothing dropped, no duplicates
    assert all(r["note"].strip() for r in rows)


def test_an_unknown_class_fails_loudly(tmp_path):
    store = tmp_path / "enabler_partners.tsv"
    store.write_text("name\tclass\tnote\nfoobactam\tbeta_lactamase_inhibitors\ttypo\n")
    with pytest.raises(ValueError, match="foobactam"):
        load_enabler_partners(store)


@pytest.mark.parametrize("label,cls", [
    ("avibactam", "beta_lactamase_inhibitor"),
    ("avibactam sodium", "beta_lactamase_inhibitor"),      # salt stripped
    ("Tazobactam Sodium", "beta_lactamase_inhibitor"),     # case-insensitive
    ("clavulanate potassium", "beta_lactamase_inhibitor"),
    ("potassium clavulanate", "beta_lactamase_inhibitor"),  # cation-first row
    ("clavulanic acid", "beta_lactamase_inhibitor"),
    ("cilastatin", "dehydropeptidase_inhibitor"),
    ("cobicistat", "pk_booster"),
])
def test_listed_names_and_their_salt_forms_match(label, cls):
    assert enabler_class(label) == cls


@pytest.mark.parametrize("label", [
    "ritonavir",                      # a booster AND a treatment: deliberately not listed
    "ceftazidime",                    # the partner that owns the indication
    "ceftazidime and avibactam",      # the product: whole-name match only
    "amoxicillin / clavulanate potassium",
    "", None,
])
def test_everything_else_is_not_an_enabler(label):
    assert not is_enabler_partner(label)


# ---------------------------------------------------------------------------
# the gate (score time)
# ---------------------------------------------------------------------------
def _pair(drug_label: str, disease_label: str, rel: str = "INDICATION", *, flags=()) -> dict:
    """A clean, otherwise-HIGH single-attestation pair."""
    return {
        "relationship_type": rel,
        "drug_id": "CHEBI:1", "drug_label": drug_label,
        "disease_id": "MONDO:1", "disease_label": disease_label,
        "assertions": [{
            "source": "DAILYMED", "document": "d",
            "drug": {"resolved_id": "CHEBI:1", "resolved_label": drug_label,
                     "original_literal": drug_label, "entity_type": "drug"},
            "disease": {"resolved_id": "MONDO:1", "original_literal": disease_label,
                        "resolution": {"pipeline": [
                            {"category": "GROUNDING", "input_value": disease_label,
                             "output_value": "MONDO:1", "method": "LEXICAL_MATCH",
                             "quality": "lexical_exact", "confidence": 1.0,
                             "confidence_basis": "MEASURED"}]}},
            "assertion": {"confidence": {"overall": 1.0}, "flags": list(flags)},
            "evidence": {"snippet": f"indicated for {disease_label}", "reference": "DailyMed:x"},
        }],
    }


def test_avibactam_indication_is_low():
    rec = _pair("avibactam", "pyelonephritis")
    assert _enabler_gate(rec, StatementType.INDICATION) is ReliabilityTier.LOW
    assert score_reliability(rec) is ReliabilityTier.LOW


def test_cobicistat_indication_is_low():
    assert score_reliability(_pair("cobicistat", "HIV infection")) is ReliabilityTier.LOW


def test_cobicistat_contraindication_is_untouched():
    rec = _pair("cobicistat", "pulmonary arterial hypertension", "CONTRAINDICATION")
    assert _enabler_gate(rec, StatementType.CONTRAINDICATION) is None
    assert score_reliability(rec) is ReliabilityTier.HIGH


def test_ritonavir_indication_is_untouched():
    rec = _pair("ritonavir", "HIV infection")
    assert _enabler_gate(rec, StatementType.INDICATION) is None
    assert score_reliability(rec) is ReliabilityTier.HIGH


def test_a_salt_form_label_matches():
    assert score_reliability(_pair("avibactam sodium", "pyelonephritis")) is ReliabilityTier.LOW


def test_a_drug_not_on_the_list_is_untouched():
    rec = _pair("ceftazidime", "pyelonephritis")
    assert _enabler_gate(rec, StatementType.INDICATION) is None
    assert score_reliability(rec) is ReliabilityTier.HIGH


def test_low_not_excluded():
    """The pair is right for the product and wrong for the component: a pairing fault."""
    assert score_reliability(_pair("tazobactam", "intra-abdominal infection")) is not \
        ReliabilityTier.EXCLUDED


def test_the_flag_alone_already_caps_low():
    """What the merge stamps is enough on its own: the assertion gate reads wrong_pairing."""
    rec = _pair("ceftazidime", "pyelonephritis", flags=["wrong_pairing"])
    assert score_reliability(rec) is ReliabilityTier.LOW


def test_a_pair_label_alone_is_enough():
    """Older products carry the drug only at pair level."""
    rec = {"relationship_type": "INDICATION", "drug_label": "cilastatin",
           "disease_id": "MONDO:1", "assertions": []}
    assert _enabler_gate(rec, StatementType.INDICATION) is ReliabilityTier.LOW


# ---------------------------------------------------------------------------
# the detector (merge time): the flag a reader sees on the assertion
# ---------------------------------------------------------------------------
def _record(drug_label: str, rel: str = "INDICATION") -> dict:
    if rel == "INDICATION":
        text = ("AVYCAZ is indicated for the treatment of complicated urinary tract "
                "infections including pyelonephritis.")
        disease = "pyelonephritis"
    else:
        text = "AVYCAZ is contraindicated in patients with known serious hypersensitivity."
        disease = "drug hypersensitivity"
    rec = {
        "source": "dailymed", "relationship_type": rel,
        "drug_id": "CHEBI:1", "final_normalized_drug_label": drug_label,
        "disease_id": "MONDO:1", "final_normalized_disease_label": disease,
        "evidence": [{"source": "dailymed", "original_disease_label": disease,
                      "original_drug_label": drug_label, "snippet": text,
                      "reference": "DailyMed:x", "source_document_url": "http://x"}],
    }
    rec["indications_text" if rel == "INDICATION" else "contraindications_text"] = text
    return rec


def test_merge_stamps_wrong_pairing_on_an_enabler_indication():
    (sa,) = _build_source_assertions(_record("avibactam"))
    assert "wrong_pairing" in sa["assertion"]["flags"]


def test_merge_leaves_the_partner_and_contraindications_alone():
    (sa,) = _build_source_assertions(_record("ceftazidime"))
    assert "wrong_pairing" not in sa["assertion"]["flags"]
    (sa,) = _build_source_assertions(_record("avibactam", "CONTRAINDICATION"))
    assert "wrong_pairing" not in sa["assertion"]["flags"]

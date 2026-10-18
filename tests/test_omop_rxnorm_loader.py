"""RxNorm ingredients enter the drug index; the bundle's other vocabularies do not.

The drug-coverage gap against v1.0.0 is 1,484 genuinely-absent drugs (`just v1-drug-gap`),
589 of them identified by RXCUI in v1. 276 resolve on an exact RxNorm ingredient name, and
they are the regional and older approvals a chemistry ontology misses — Levonordefrin,
Delavirdine, Pemirolast, Tertatolol, Bencyclane.

Two constraints the tests exist to hold:

* **Only `vocabulary_id == "RxNorm"` is read.** The ATHENA bundle carries SNOMED, MedDRA and
  others whose term text MeDIC may not republish (I-14 rule 2). Filtering at read time keeps
  restricted content out of the index rather than relying on a downstream check — the same
  fail-closed reasoning that turned the UMLS label blocklist into an allowlist.
* **Ingredient level only.** A clinical drug ("lansoprazole 30 MG oral capsule") denotes a
  dose form, not a substance; indexing those reintroduces exactly what #94 reports about DRON
  clinical-drug forms — the wrong kind of node for a drug-disease edge.
"""

from __future__ import annotations

import csv
import io
import zipfile

import pytest

from medic.grounding.lexical.index import VOCAB_ORDER
from medic.grounding.lexical.loaders.omop_rxnorm import load_omop_rxnorm

_COLS = ["concept_id", "concept_name", "domain_id", "vocabulary_id", "concept_class_id",
         "standard_concept", "concept_code", "valid_start_date", "valid_end_date",
         "invalid_reason"]


def _row(name, vocab="RxNorm", cls="Ingredient", code="123", domain="Drug", invalid=""):
    return {"concept_id": "1", "concept_name": name, "domain_id": domain,
            "vocabulary_id": vocab, "concept_class_id": cls, "standard_concept": "S",
            "concept_code": code, "valid_start_date": "19700101",
            "valid_end_date": "20991231", "invalid_reason": invalid}


def _zip(tmp_path, rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=_COLS, delimiter="\t", lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    path = tmp_path / "omop.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("CONCEPT.csv", buf.getvalue())
    return path


def test_an_ingredient_becomes_an_rxcui_label_row(tmp_path):
    rows = list(load_omop_rxnorm(_zip(tmp_path, [_row("delavirdine", code="83816")])))
    assert len(rows) == 1
    r = rows[0]
    assert r.object_id == "RXCUI:83816"
    assert r.object_label == "delavirdine"
    assert r.match_field == "label"
    assert r.source_prefix == "RXCUI"
    assert r.norm_value == "delavirdine"


def test_a_precise_ingredient_is_kept():
    """A specific salt or ester is still a substance."""
    assert "Precise Ingredient" in __import__(
        "medic.grounding.lexical.loaders.omop_rxnorm", fromlist=["x"]
    ).INGREDIENT_CLASSES


@pytest.mark.parametrize("vocab", ["SNOMED", "MedDRA", "ICD10CM", "CPT4", "HCPCS"])
def test_no_other_vocabulary_is_read(tmp_path, vocab):
    """The licence-critical one. Anything but RxNorm must not reach the index at all."""
    rows = list(load_omop_rxnorm(_zip(tmp_path, [_row("something", vocab=vocab)])))
    assert rows == []


@pytest.mark.parametrize("cls", [
    "Clinical Drug", "Branded Drug", "Quant Clinical Drug", "Clinical Pack",
    "Clinical Drug Comp", "Dose Form",
])
def test_dose_form_and_pack_concepts_are_skipped(tmp_path, cls):
    rows = list(load_omop_rxnorm(_zip(tmp_path, [_row("x 30 MG capsule", cls=cls)])))
    assert rows == []


def test_a_deprecated_concept_is_skipped(tmp_path):
    """It still resolves for a consumer holding the id, but MeDIC should not newly ground
    anything to it."""
    rows = list(load_omop_rxnorm(_zip(tmp_path, [_row("old thing", invalid="D")])))
    assert rows == []


def test_a_non_drug_domain_is_skipped(tmp_path):
    rows = list(load_omop_rxnorm(_zip(tmp_path, [_row("thing", domain="Device")])))
    assert rows == []


def test_a_missing_zip_warns_and_yields_nothing(tmp_path, caplog):
    """A checkout without the licence-gated download must still build an index."""
    rows = list(load_omop_rxnorm(tmp_path / "absent.zip"))
    assert rows == []
    assert any("not found" in m for m in caplog.messages)


def test_rxcui_is_ranked_after_the_chemistry_ontologies():
    """The matcher is vocabulary-major, so this ordering is what decides ties: CHEBI's
    chemistry wins wherever it matches, and RxNorm fills what the ontologies lack."""
    order = VOCAB_ORDER["drugs"]
    assert "RXCUI" in order
    assert order.index("CHEBI") < order.index("RXCUI")
    assert order.index("DRON") < order.index("RXCUI")

"""RxNorm ingredients enter the drug index; the bundle's other vocabularies do not.

The drug-coverage gap against v1.0.0 is 1,484 genuinely-absent drugs (`just v1-drug-gap`),
589 of them identified by RXCUI in v1. 276 resolve on an exact RxNorm ingredient name, and
they are the regional and older approvals a chemistry ontology misses — Levonordefrin,
Delavirdine, Pemirolast, Tertatolol, Bencyclane.

It reads a **committed table** (`data/vocab/rxnorm_ingredients.tsv`, 3 columns, 0.7MB), not
a vocabulary dump. That is the point: the grounding index has to be reproducible from a
checkout, and a loader pointing at a 1GB ATHENA download on one machine means nobody else can
build MeDIC. `scripts/build_rxnorm_table.py` regenerates it and is the only thing that needs
the dump.

So the licence and modelling filters happen at *extraction* time and the tests check the
shipped artifact rather than the filter:

* The table carries **RxNorm content only**. The bundle also holds SNOMED, MedDRA and others
  whose term text MeDIC may not republish (I-14 rule 2); excluding them when the table is
  built means the committed artifact can never carry them. RxNorm's own content is UMLS
  Appendix 1 Category 0, which is what makes this table redistributable at all.
* **Ingredient level only.** A clinical drug ("lansoprazole 30 MG oral capsule") denotes a
  dose form, not a substance; indexing those reintroduces exactly what #94 reports about DRON
  clinical-drug forms — the wrong kind of node for a drug-disease edge.
"""

from __future__ import annotations

from pathlib import Path

from medic.grounding.lexical.index import VOCAB_ORDER
from medic.grounding.lexical.loaders.omop_rxnorm import DEFAULT_PATH, load_omop_rxnorm

TABLE = Path(DEFAULT_PATH)


def _table(tmp_path, rows: list[tuple[str, str, str]]):
    path = tmp_path / "rxnorm.tsv"
    body = "omop_concept_id\trxcui\tlabel\n" + "".join(
        f"{c}\t{r}\t{label}\n" for c, r, label in rows)
    path.write_text(body)
    return path


def test_an_ingredient_becomes_an_rxcui_label_row(tmp_path):
    rows = list(load_omop_rxnorm(_table(tmp_path, [("1", "83816", "delavirdine")])))
    assert len(rows) == 1
    r = rows[0]
    assert r.object_id == "RXCUI:83816"
    assert r.object_label == "delavirdine"
    assert r.match_field == "label"
    assert r.source_prefix == "RXCUI"
    assert r.norm_value == "delavirdine"


def test_a_row_missing_an_id_or_label_is_skipped(tmp_path):
    rows = list(load_omop_rxnorm(_table(tmp_path, [("1", "", "nameless"), ("2", "9", "")])))
    assert rows == []


def test_a_missing_table_warns_and_yields_nothing(tmp_path, caplog):
    """A checkout must still build an index if the table is ever absent."""
    rows = list(load_omop_rxnorm(tmp_path / "absent.tsv"))
    assert rows == []
    assert any("not found" in m for m in caplog.messages)


def test_rxcui_is_ranked_after_the_chemistry_ontologies():
    """The matcher is vocabulary-major, so this ordering is what decides ties: CHEBI's
    chemistry wins wherever it matches, and RxNorm fills what the ontologies lack."""
    order = VOCAB_ORDER["drugs"]
    assert "RXCUI" in order
    assert order.index("CHEBI") < order.index("RXCUI")
    assert order.index("DRON") < order.index("RXCUI")


# ---------------------------------------------------------------------------
# The committed table itself — this is what ships, so assert on it directly
# ---------------------------------------------------------------------------
def test_the_committed_table_is_present_and_well_formed():
    """The loader reading a committed table rather than a 1GB local dump is the whole point:
    the grounding index has to be reproducible from a checkout."""
    assert TABLE.exists(), f"{TABLE} must be committed"
    head = TABLE.read_text().splitlines()[0].split("\t")
    assert head == ["omop_concept_id", "rxcui", "label"]


def test_the_committed_table_carries_no_dose_forms():
    """Dose forms would be the wrong *kind* of node for a drug-disease edge — the DRON
    problem in #94. Extraction excludes them; this checks the artifact, not the filter."""
    bad = [line for line in TABLE.read_text().splitlines()[1:]
           if any(tok in line.lower() for tok in (" mg oral", " mg/ml ", "oral tablet",
                                                  "oral capsule", "injectable solution"))]
    assert not bad[:5], f"dose-form-looking rows in the committed table: {bad[:5]}"


def test_the_committed_table_has_the_drugs_this_was_added_for():
    """The 1,484-drug gap is the reason RxNorm is here at all. These are v1 drugs absent from
    the CHEBI-only build that RxNorm supplies."""
    labels = {line.split("\t")[2].lower()
              for line in TABLE.read_text().splitlines()[1:] if line.count("\t") == 2}
    for name in ("delavirdine", "pemirolast", "levonordefrin", "tertatolol"):
        assert name in labels, name

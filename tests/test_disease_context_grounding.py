"""The disease context is a disease, not a sentence (Lever 2).

Two defects, both measured against v1.0.0 rather than guessed at. Of 7,217 v1 indication
pairs the current build does not assert, **2,266 (31.4%) name a disease that is already in
label text we hold** — the largest recoverable share of the gap, needing no new source.

**1. The prompt let the model summarise.** Polythiazide's SPL says "edema associated with
congestive heart failure, hepatic cirrhosis"; what came back was
`disease_context: ['pathologic causes']`. v1 published `polythiazide -> congestive heart
failure` as a bare `treats` edge, which is its own `FP1_setting` error — the drug treats the
*edema*. So the honest repair is not to re-assert v1's pair but to name the context, which
the label does and we discarded.

**2. The context was never grounded.** Biolink gives `disease_context_qualifier` the range
**`disease`** — a class, not a string — so a free-text value satisfies the slot's *name*,
which is all the conformance gate checks, and not its range. Grounding it makes the export
correct against the pinned model and makes the fact findable by CURIE.
"""

from __future__ import annotations

import pytest

from medic.ingest.dailymed.__main__ import (
    Claim,
    is_generic_context,
    parse_structured_claims,
    record_claim_context,
)


# ---------------------------------------------------------------------------
# The generic-context guard
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("generic", [
    "pathologic causes",          # the observed polythiazide value
    "Pathological Conditions",
    "various causes",
    "various forms",
    "underlying disease",
    "underlying conditions",
    "other causes",
    "certain conditions",
    "associated conditions",
    "unspecified",
    "miscellaneous",
])
def test_a_category_is_not_a_context(generic):
    assert is_generic_context(generic)


@pytest.mark.parametrize("real", [
    "congestive heart failure",
    "hepatic cirrhosis",
    "acute myeloid leukemia",
    "non-transfusion-dependent thalassemia",
    "congenital heart disease",
    # Contains a guard word but names a condition — must survive.
    "idiopathic pulmonary arterial hypertension",
    "other specified cardiac arrhythmia",
])
def test_a_named_condition_survives(real):
    assert not is_generic_context(real)


def test_a_generic_context_is_dropped_by_the_parser():
    """End to end: the guard runs inside the parser, so a model ignoring the prompt rule
    still cannot put a category in the slot."""
    claims = parse_structured_claims(
        "target: edema :: edema\n"
        "predicate: treats\n"
        "disease_context: pathologic causes :: pathologic causes\n"
    )
    assert len(claims) == 1
    assert claims[0].disease_context == ()


def test_the_named_conditions_are_kept_when_the_model_lists_them():
    """What the polythiazide label should have produced."""
    claims = parse_structured_claims(
        "target: edema :: edema\n"
        "predicate: treats\n"
        "disease_context: congestive heart failure :: congestive heart failure ; "
        "hepatic cirrhosis :: hepatic cirrhosis\n"
    )
    assert [n for n, _ in claims[0].disease_context] == [
        "congestive heart failure", "hepatic cirrhosis"]


def test_a_mixed_list_keeps_only_the_real_conditions():
    claims = parse_structured_claims(
        "target: edema :: edema\n"
        "disease_context: various causes :: various causes ; "
        "hepatic cirrhosis :: hepatic cirrhosis\n"
    )
    assert [n for n, _ in claims[0].disease_context] == ["hepatic cirrhosis"]


# ---------------------------------------------------------------------------
# Grounding the context onto the evidence row
# ---------------------------------------------------------------------------
def test_context_ids_are_recorded_alongside_the_names(monkeypatch):
    import medic.ingest.dailymed.__main__ as dm

    monkeypatch.setattr(dm, "_ground_disease", lambda name, backend, rec: {
        "congestive heart failure": ("MONDO:0005009", "congestive heart failure"),
        "hepatic cirrhosis": ("MONDO:0005155", "liver cirrhosis"),
    }.get(name, ("", "")))

    ev: dict = {}
    record_claim_context(ev, Claim(
        target="edema",
        disease_context=(("congestive heart failure", "congestive heart failure"),
                         ("hepatic cirrhosis", "hepatic cirrhosis")),
    ), grounding_backend="lexical")

    assert ev["disease_context"] == ["congestive heart failure", "hepatic cirrhosis"]
    assert ev["disease_context_ids"] == ["MONDO:0005009", "MONDO:0005155"]


def test_an_unresolved_context_keeps_its_name_and_its_position(monkeypatch):
    """A grounding failure must cost the CURIE, not the fact — and the two lists have to stay
    alignable, so a failure is "" rather than a gap."""
    import medic.ingest.dailymed.__main__ as dm
    monkeypatch.setattr(dm, "_ground_disease", lambda name, backend, rec:
                        ("MONDO:0005155", "x") if name == "hepatic cirrhosis" else ("", ""))

    ev: dict = {}
    record_claim_context(ev, Claim(
        target="edema",
        disease_context=(("something unmappable", ""), ("hepatic cirrhosis", "")),
    ), grounding_backend="lexical")

    assert ev["disease_context"] == ["something unmappable", "hepatic cirrhosis"]
    assert ev["disease_context_ids"] == ["", "MONDO:0005155"]


def test_no_backend_means_names_only(monkeypatch):
    """Callers without a grounding backend still record the names. Opt-in by parameter."""
    import medic.ingest.dailymed.__main__ as dm

    def _boom(*a, **k):
        raise AssertionError("must not ground without a backend")

    monkeypatch.setattr(dm, "_ground_disease", _boom)
    ev: dict = {}
    record_claim_context(ev, Claim(target="edema",
                                   disease_context=(("hepatic cirrhosis", ""),)))
    assert ev["disease_context"] == ["hepatic cirrhosis"]
    assert "disease_context_ids" not in ev


def test_a_context_that_grounds_to_nothing_sets_no_ids_key(monkeypatch):
    import medic.ingest.dailymed.__main__ as dm
    monkeypatch.setattr(dm, "_ground_disease", lambda *a, **k: ("", ""))
    ev: dict = {}
    record_claim_context(ev, Claim(target="edema", disease_context=(("nope", ""),)),
                         grounding_backend="lexical")
    assert "disease_context_ids" not in ev, "all-empty ids add nothing; omit the key"


# ---------------------------------------------------------------------------
# Export: the Biolink slot takes the CURIE
# ---------------------------------------------------------------------------
def test_the_biolink_slot_is_typed_as_a_disease_not_a_string():
    """Pins the premise of the whole change. If Biolink ever retypes this slot to a string,
    the CURIE emission below becomes wrong and this test says so first."""
    from medic.export.kgx.biolink import _model
    assert _model()["slots"]["disease context qualifier"]["range"] == "disease"


def _edge(**evidence_extra):
    from medic.export.kgx import edges
    from tests.test_edge_qualifiers import _assertion
    pair = {"drug_id": "CHEBI:8327", "drug_label": "polythiazide",
            "disease_id": "MONDO:0004784", "disease_label": "Edema",
            "relationship_type": "INDICATION", "reliability": "HIGH",
            "assertions": [_assertion(**evidence_extra)]}
    built = edges.association_edges(pair)
    assert built
    return built[0]


def test_the_grounded_curie_goes_on_the_biolink_slot():
    e = _edge(disease_context=["congestive heart failure", "hepatic cirrhosis"],
              disease_context_ids=["MONDO:0005009", "MONDO:0005155"])
    assert e["disease_context_qualifier"] == "MONDO:0005009"
    assert e["medic_disease_context_ids"] == ["MONDO:0005009", "MONDO:0005155"]
    assert "medic_disease_context_labels" not in e   # the ids name themselves


def test_an_ungrounded_context_still_ships_its_label_but_not_on_the_biolink_slot():
    """Dropping the context because it did not ground would lose more than it protects — so
    the label still ships, on the namespaced property. It used to ship on
    `disease_context_qualifier` itself, which was the lesser of two evils only until there
    was somewhere else to put it: the slot's range is `disease`, and a string satisfies the
    slot's name and nothing more (#110)."""
    e = _edge(disease_context=["congenital heart disease"])
    assert "disease_context_qualifier" not in e
    assert e["medic_disease_context"] == ["congenital heart disease"]
    assert "medic_disease_context_ids" not in e


def test_the_qualified_edge_still_passes_the_conformance_gate():
    from medic.export.kgx import validate
    e = _edge(disease_context=["congestive heart failure"],
              disease_context_ids=["MONDO:0005009"])
    report = validate.check([], [e])
    unknown = [p for p in report.errors if "not a Biolink slot" in str(p)]
    assert not unknown, unknown

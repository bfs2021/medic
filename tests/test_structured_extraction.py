"""The extraction returns a structured claim, not a flat list of disease names.

One prompt change answering four issues, because each asks the same thing of the extractor:

* **#88** which ingredient a claim is attributed to — combination-only pairs are 65% wrong
* **#89** target vs population/setting — 13.1% of edges, the largest single error class
* **#90** the context as Biolink qualifiers instead of dropped or promoted to the object
* **#99** treats vs prevents vs reduces_risk_of vs diagnoses — all 12,448 edges say `treats`

Doing them separately would mean four prompt migrations and four paid re-extractions.

The wire format is one labelled field per line, records separated by `---`. The previous
`name :: verbatim` form could not carry seven fields, and a pipe-separated positional
format breaks the moment a label contains a pipe — which drug names do.
"""

from __future__ import annotations

from medic.ingest.dailymed.__main__ import Claim, parse_structured_claims

POSA = """target: invasive aspergillosis :: invasive aspergillosis
predicate: prevents
attributed_to: product
basis: product_level
disease_context: acute myeloid leukemia :: acute myeloid leukemia
population: patients at high risk
---
target: candidiasis :: candidiasis
predicate: prevents
attributed_to: product
basis: product_level
disease_context: acute myeloid leukemia :: acute myeloid leukemia"""

ESOME = """target: osteoarthritis :: osteoarthritis
predicate: treats
attributed_to: naproxen
basis: component_phrase"""


def test_a_single_claim_parses_every_field():
    claims = parse_structured_claims(POSA)
    assert len(claims) == 2
    c = claims[0]
    assert c.target == "invasive aspergillosis"
    assert c.target_verbatim == "invasive aspergillosis"
    assert c.predicate == "prevents"
    assert c.attributed_to == "product"
    assert c.basis == "product_level"
    assert c.disease_context == (("acute myeloid leukemia", "acute myeloid leukemia"),)
    assert c.population == "patients at high risk"


def test_records_are_split_on_the_separator():
    assert [c.target for c in parse_structured_claims(POSA)] == [
        "invasive aspergillosis", "candidiasis"]


def test_an_ingredient_attribution_is_captured():
    """#88's central case: "The naproxen component ... is indicated for ..." on a
    combination label must not credit esomeprazole."""
    c = parse_structured_claims(ESOME)[0]
    assert c.attributed_to == "naproxen"
    assert c.basis == "component_phrase"


def test_missing_optional_fields_default_cleanly():
    c = parse_structured_claims("target: epilepsy :: epilepsy\npredicate: treats")[0]
    # Tuples, not lists: Claim is an immutable record, so a mutable default is a bug
    # waiting to be shared between claims.
    assert c.disease_context == ()
    assert c.population == ""
    assert c.co_administered_with == ()


def test_a_record_without_a_target_is_dropped():
    assert parse_structured_claims("predicate: treats\npopulation: adults") == []


def test_none_and_empty_are_empty():
    assert parse_structured_claims("None") == []
    assert parse_structured_claims("") == []


def test_an_unknown_predicate_falls_back_to_treats():
    """A model that invents a relation must not invent a Biolink predicate with it."""
    c = parse_structured_claims("target: epilepsy :: epilepsy\npredicate: cures")[0]
    assert c.predicate == "treats"


def test_the_disease_name_guard_applies_to_the_target():
    text = ("target: These are contraindications, not indications. :: x\n"
            "predicate: treats\n---\ntarget: epilepsy :: epilepsy\npredicate: treats")
    assert [c.target for c in parse_structured_claims(text)] == ["epilepsy"]


def test_several_contexts_and_coadministrations_parse():
    text = ("target: rheumatoid arthritis :: RA\npredicate: treats\n"
            "disease_context: psoriasis :: psoriasis ; Crohn disease :: Crohn's\n"
            "co_administered_with: methotrexate ; folic acid")
    c = parse_structured_claims(text)[0]
    assert c.disease_context == (("psoriasis", "psoriasis"),
                                 ("Crohn disease", "Crohn's"))
    assert c.co_administered_with == ("methotrexate", "folic acid")


def test_a_context_without_a_verbatim_still_parses():
    c = parse_structured_claims(
        "target: PAH :: PAH\npredicate: treats\ndisease_context: congenital heart disease")[0]
    assert c.disease_context == (("congenital heart disease", ""),)


def test_whitespace_and_blank_lines_are_tolerated():
    text = "\n\n  target:  epilepsy :: epilepsy  \n\n  predicate:  treats  \n\n"
    assert parse_structured_claims(text) == [
        Claim(target="epilepsy", target_verbatim="epilepsy", predicate="treats")]


def test_an_unrecognised_line_is_ignored_not_fatal():
    """Models add commentary. It must not take the record with it."""
    text = ("Here are the claims:\ntarget: epilepsy :: epilepsy\n"
            "predicate: treats\nnotes: I hope this helps")
    c = parse_structured_claims(text)[0]
    assert c.target == "epilepsy"


def test_a_legacy_pipe_list_still_parses():
    """The v2 caches hold `name :: verbatim|name :: verbatim`. They must keep working until
    the v3 re-extraction has replaced them, or a cache hit crashes the build."""
    claims = parse_structured_claims("epilepsy :: epilepsy|asthma :: asthma")
    assert [c.target for c in claims] == ["epilepsy", "asthma"]
    assert all(c.predicate == "treats" for c in claims)


# ---------------------------------------------------------------------------
# Duplicate claims from the Highlights repetition
# ---------------------------------------------------------------------------
def test_identical_claims_are_collapsed():
    """An SPL states its indications twice — once in Highlights, once in the full section
    — so the model reports each claim twice. The smoke test saw baclofen return "severe
    spasticity" twice with identical context, and citalopram "major depressive disorder"
    twice."""
    text = ("target: major depressive disorder :: MDD\npredicate: treats\n"
            "population: adults\n---\n"
            "target: major depressive disorder :: MDD\npredicate: treats\n"
            "population: adults")
    assert len(parse_structured_claims(text)) == 1


def test_claims_differing_in_context_are_both_kept():
    """Two claims on the same disease with different context are two claims, not one —
    which is the whole point of #90.3's merge key."""
    text = ("target: severe spasticity :: spasticity\npredicate: treats\n"
            "disease_context: traumatic brain injury :: TBI\n---\n"
            "target: severe spasticity :: spasticity\npredicate: treats\n"
            "disease_context: multiple sclerosis :: MS")
    claims = parse_structured_claims(text)
    assert len(claims) == 2


def test_claims_differing_only_in_predicate_are_both_kept():
    text = ("target: stroke :: stroke\npredicate: treats\n---\n"
            "target: stroke :: stroke\npredicate: prevents")
    assert len(parse_structured_claims(text)) == 2


def test_the_first_occurrence_order_is_preserved():
    text = ("target: b :: b\npredicate: treats\n---\ntarget: a :: a\npredicate: treats\n"
            "---\ntarget: b :: b\npredicate: treats")
    assert [c.target for c in parse_structured_claims(text)] == ["b", "a"]

"""Which acquired SPL labels are worth mining.

`data/raw/dailymed/` is the union of every acquisition run ever made, so it still holds
labels from before `acquire.py` filtered its search pool by human document type (#98/#103).
Filtering future searches does not remove them, and two kinds are not worth mining: animal
labels, whose indications become claims about a human drug, and labels carrying neither an
Indications nor a Contraindications section, which can yield nothing.

The classification reads each label's own `displayName` rather than a hardcoded LOINC list,
because the hardcoded list was written first and was wrong in both directions: it had
`53406-5` down as veterinary (it is LICENSED VACCINE BULK INTERMEDIATE LABEL) and omitted
`50575-0`, `50576-8` and `81203-2`, which are animal types. Those real codes and names are
the parametrised cases below.
"""

from __future__ import annotations

import pytest

from scripts.prune_dailymed_corpus import (
    REASON_ANIMAL,
    REASON_NO_SECTION,
    classify,
    document_type,
    is_animal,
)

HEADER = """<?xml version="1.0" encoding="UTF-8"?>
<document xmlns="urn:hl7-org:v3">
  <id root="x"/>
  <code code="{code}" codeSystem="2.16.840.1.113883.6.1" displayName="{display}"/>
  <title>t</title>
  <component><structuredBody>{body}</structuredBody></component>
</document>
"""
INDICATIONS = ('<component><section><code code="34067-9" '
               'codeSystem="2.16.840.1.113883.6.1"/><text>indicated for epilepsy</text>'
               "</section></component>")
OTHER_SECTION = ('<component><section><code code="34066-1" '
                 'codeSystem="2.16.840.1.113883.6.1"/><text>boxed warning</text>'
                 "</section></component>")


def _label(tmp_path, name, code, display, body=INDICATIONS):
    p = tmp_path / f"{name}.xml"
    p.write_text(HEADER.format(code=code, display=display, body=body))
    return p


# ---------------------------------------------------------------------------
# Reading the document type
# ---------------------------------------------------------------------------
def test_the_document_type_is_read_not_the_first_section_code(tmp_path):
    """A label whose first LOINC code in the file is a *section* code must still report its
    document type. Matching by position is what made the first attempt at this report 278
    labels as doctype "34067-9", which is the indications section."""
    p = tmp_path / "x.xml"
    p.write_text(HEADER.format(code="34391-3", display="HUMAN PRESCRIPTION DRUG LABEL",
                               body=INDICATIONS))
    assert document_type(p) == ("34391-3", "HUMAN PRESCRIPTION DRUG LABEL")


def test_unparseable_xml_is_not_a_crash(tmp_path):
    p = tmp_path / "broken.xml"
    p.write_text("<document><code")
    assert document_type(p) == ("", "")


# ---------------------------------------------------------------------------
# Animal classification — the real codes and names from the corpus
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("code,display", [
    ("50578-4", "PRESCRIPTION ANIMAL DRUG LABEL"),
    ("50577-6", "OTC ANIMAL DRUG LABEL"),
    ("50575-0", "VFD TYPE A MEDICATED ARTICLE ANIMAL DRUG LABEL"),
    ("50576-8", "OTC TYPE A MEDICATED ARTICLE ANIMAL DRUG LABEL"),
    ("81203-2", "BULK INGREDIENT - ANIMAL DRUG"),
    ("101437-2", "INTENTIONAL ANIMAL GENOMIC ALTERATION LABEL"),
])
def test_every_animal_type_in_the_corpus_is_recognised(code, display):
    assert is_animal(code, display)


@pytest.mark.parametrize("code,display", [
    ("34391-3", "HUMAN PRESCRIPTION DRUG LABEL"),
    ("34390-5", "HUMAN OTC DRUG LABEL"),
    ("45129-4", "HUMAN PRESCRIPTION DRUG LABELING WITH HIGHLIGHTS"),
    # The one the guessed list got wrong: a vaccine intermediate, not a veterinary label.
    ("53406-5", "LICENSED VACCINE BULK INTERMEDIATE LABEL"),
    ("53404-0", "VACCINE LABEL"),
    ("53409-9", "BULK INGREDIENT"),
    ("58476-3", "DIETARY SUPPLEMENT"),
    ("60683-0", "PLASMA DERIVATIVE"),
])
def test_no_non_animal_type_is_swept_up(code, display):
    assert not is_animal(code, display)


# ---------------------------------------------------------------------------
# What gets moved
# ---------------------------------------------------------------------------
def test_an_animal_label_is_moved_even_with_an_indications_section(tmp_path):
    """The section is exactly the problem: a veterinary label *with* indications is the one
    that produces "ivermectin treats heartworm" as a human drug claim."""
    p = _label(tmp_path, "vet", "50578-4", "PRESCRIPTION ANIMAL DRUG LABEL")
    to_move, _types, _names = classify(tmp_path)
    assert to_move == {p: REASON_ANIMAL}


def test_a_human_label_with_no_mined_section_is_moved(tmp_path):
    p = _label(tmp_path, "empty", "34391-3", "HUMAN PRESCRIPTION DRUG LABEL",
               body=OTHER_SECTION)
    to_move, _types, _names = classify(tmp_path)
    assert to_move == {p: REASON_NO_SECTION}


def test_a_human_label_with_indications_is_kept(tmp_path):
    _label(tmp_path, "good", "34391-3", "HUMAN PRESCRIPTION DRUG LABEL")
    to_move, types, _names = classify(tmp_path)
    assert to_move == {}
    assert sum(types.values()) == 1


def test_a_contraindications_only_label_is_kept(tmp_path):
    """Both mined sections count. Keying on indications alone would throw away every label
    that states only contraindications, and contraindications are a product in their own
    right (4,487 pairs)."""
    body = INDICATIONS.replace("34067-9", "34070-3")
    _label(tmp_path, "contra", "34391-3", "HUMAN PRESCRIPTION DRUG LABEL", body=body)
    to_move, _types, _names = classify(tmp_path)
    assert to_move == {}


def test_a_non_drug_human_type_is_reported_but_not_moved(tmp_path):
    """Dietary supplements, vaccines, plasma derivatives and the like are a scope question
    about what counts as a drug, not an obvious defect. Deciding it silently inside a
    cleanup script is how scope creeps, so they are counted and left alone."""
    _label(tmp_path, "supp", "58476-3", "DIETARY SUPPLEMENT")
    to_move, types, _names = classify(tmp_path)
    assert to_move == {}
    assert any("DIETARY SUPPLEMENT" in k for k in types)

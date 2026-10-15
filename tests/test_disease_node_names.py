"""A disease node ships with a usable name (#96).

Two defects, both display-only but both blocking verification:

* **91 stub nodes used by 5,086 edges carry no `name` at all.** Their preferred atoms come
  from label-restricted vocabularies (MedDRA, ICD-10), so the node ships as a bare CUI —
  `UMLS:C0278488` for "Metastatic breast cancer", used by 41 edges. In the audit, edges
  whose object was an unnamed CUI could not be judged at all. The source literal is already
  on the edge as `original_object`, so there is a fallback to hand.
* **66 nodes are named in inverted MeSH thesaurus form** — "Diseases, Liver" (24 edges),
  "Headaches, Tension" (22), "Angina, Stable" (16), "Pains, Cancer" (7). De-inverting on the
  last comma is the standard reading of that form and recovers "Liver Diseases".
"""

from __future__ import annotations

import pytest

from medic.export.kgx.nodes import deinvert_thesaurus_name, stub_node


# ---------------------------------------------------------------------------
# Unnamed stubs
# ---------------------------------------------------------------------------
def test_a_stub_without_a_name_is_still_valid():
    node = stub_node("UMLS:C0278488")
    assert node["id"] == "UMLS:C0278488"
    assert "name" not in node  # _clean drops empties rather than shipping ""


def test_a_stub_takes_the_source_literal_as_its_name():
    node = stub_node("UMLS:C0278488", "Metastatic breast cancer")
    assert node["name"] == "Metastatic breast cancer"


# ---------------------------------------------------------------------------
# MeSH thesaurus inversions
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("inverted,expected", [
    ("Diseases, Liver", "Liver Diseases"),
    ("Headaches, Tension", "Tension Headaches"),
    ("Angina, Stable", "Stable Angina"),
    ("Deficiency, Factor VIII", "Factor VIII Deficiency"),
    ("Incontinence, Urge", "Urge Incontinence"),
    ("Pneumonia, Hospital Acquired", "Hospital Acquired Pneumonia"),
    ("Pain, Postoperative", "Postoperative Pain"),
    ("Infections, Soft Tissue", "Soft Tissue Infections"),
    ("Pains, Cancer", "Cancer Pains"),
])
def test_an_inverted_name_is_de_inverted(inverted, expected):
    assert deinvert_thesaurus_name(inverted) == expected


@pytest.mark.parametrize("name", [
    "invasive aspergillosis",
    "type 2 diabetes mellitus",
    # A comma that is not a thesaurus inversion: a list, and a qualifier phrase.
    "asthma, rhinitis and nasal polyps",
    "epithelial ovarian, fallopian tube, or primary peritoneal cancer",
    # Lower-case second part is prose, not an inverted head term.
    "anemia, due to chronic kidney disease",
])
def test_an_ordinary_name_is_untouched(name):
    assert deinvert_thesaurus_name(name) == name


def test_an_abbreviation_used_as_a_name_is_left_alone():
    """DFSP and WARTS are flagged in the issue but de-inversion is not the remedy — there is
    no comma to swap. They need a better label source, which is #13."""
    assert deinvert_thesaurus_name("DFSP") == "DFSP"


def test_de_inversion_is_idempotent():
    once = deinvert_thesaurus_name("Diseases, Liver")
    assert deinvert_thesaurus_name(once) == once


def test_a_stub_name_is_de_inverted_too():
    assert stub_node("MESH:D008107", "Diseases, Liver")["name"] == "Liver Diseases"

"""The drug is not the counter-ion or the active metabolite (#95).

`activeMoiety` is what FDA SRS fills, and for two classes it is not the dispensed drug:

* **Ions and electrolytes.** Multivitamin, parenteral-nutrition and OTC labels list
  `MAGNESIUM CATION`, `CHLORIDE`, `FERROUS CATION` as active moieties, which become drug
  nodes asserting indications — magnesium cation for acid reflux, chloride for anxiety
  disorder. 133 such pairs survive in the current build.
* **Active metabolites of prodrugs.** A ramipril label's moiety is `RAMIPRILAT`, so every
  ramipril indication lands on a node no other resource uses. Likewise `FOSINOPRILAT`,
  `QUINAPRILAT`, `BENAZEPRILAT`, `4-HYDROXYCYCLOPHOSPHAMIDE`.

The metabolite rule is derived, not tabulated: every SPL carries
`<genericMedicine><name>` (1,976 of 1,976), which names the dispensed product —
`FOSINOPRILAT` against `FOSINOPRIL SODIUM`. When the moiety contains a generic-name stem,
the stem is the drug and the moiety is its metabolite. No curated prodrug list to maintain.

What this does *not* fix: the ~561 salt-form drug nodes #95 reports. Measured by
`mention_source`, every one of those comes from Orange Book, EMA, PMDA, GRLS, CDE or
EveryCure — not DailyMed, whose `activeMoiety` already strips the salt
(`TOBRAMYCIN SULFATE` -> `TOBRAMYCIN`). That half belongs with #27's normalization index.
"""

from __future__ import annotations

import pytest
import xml.etree.ElementTree as ET

from medic.ingest.dailymed.__main__ import _extract_ingredients_from_root

_SPL = """<?xml version="1.0" encoding="UTF-8"?>
<document xmlns="urn:hl7-org:v3">
  <setId root="1"/>
  <manufacturedProduct>
    <asEntityWithGeneric><genericMedicine><name>{generic}</name></genericMedicine></asEntityWithGeneric>
  </manufacturedProduct>
  {moieties}
  <component><structuredBody><component><section>
    <code code="34067-9"/><text>Indicated for hypertension.</text>
  </section></component></structuredBody></component>
</document>"""


def _root(generic, *moieties):
    blocks = "".join(
        f"<activeMoiety><activeMoiety><name>{m}</name></activeMoiety></activeMoiety>"
        for m in moieties)
    return ET.fromstring(_SPL.format(generic=generic, moieties=blocks))


@pytest.mark.parametrize("moiety", [
    "MAGNESIUM CATION", "CALCIUM CATION", "FERROUS CATION", "MANGANESE CATION",
    "GADOLINIUM CATION (3+)", "CHLORIDE", "FLUORIDE", "BICARBONATE", "HYDROXIDE",
    "PHOSPHATE ION",
])
def test_an_ion_is_not_a_drug(moiety):
    assert _extract_ingredients_from_root(_root("SODIUM CHLORIDE", moiety)) == []


@pytest.mark.parametrize("generic,moiety,expected", [
    ("FOSINOPRIL SODIUM", "FOSINOPRILAT", "FOSINOPRIL"),
    ("TRANDOLAPRIL", "TRANDOLAPRILAT", "TRANDOLAPRIL"),
    ("BENAZEPRIL HYDROCHLORIDE", "BENAZEPRILAT", "BENAZEPRIL"),
    ("QUINAPRIL HYDROCHLORIDE", "QUINAPRILAT", "QUINAPRIL"),
    ("PERINDOPRIL ERBUMINE", "PERINDOPRILAT", "PERINDOPRIL"),
    ("CYCLOPHOSPHAMIDE", "4-HYDROXYCYCLOPHOSPHAMIDE", "CYCLOPHOSPHAMIDE"),
])
def test_a_metabolite_resolves_to_the_dispensed_drug(generic, moiety, expected):
    assert _extract_ingredients_from_root(_root(generic, moiety)) == [expected]


def test_an_ordinary_moiety_is_untouched():
    assert _extract_ingredients_from_root(_root("VORICONAZOLE", "VORICONAZOLE")) == \
        ["VORICONAZOLE"]


def test_a_salt_generic_does_not_override_a_clean_moiety():
    """activeMoiety already strips the salt, which is what we want — the generic name must
    not drag `TOBRAMYCIN SULFATE` back in."""
    assert _extract_ingredients_from_root(_root("TOBRAMYCIN SULFATE", "TOBRAMYCIN")) == \
        ["TOBRAMYCIN"]


def test_a_combination_keeps_its_non_ion_ingredients():
    out = _extract_ingredients_from_root(
        _root("AMLODIPINE BESYLATE AND BENAZEPRIL HYDROCHLORIDE",
              "AMLODIPINE", "BENAZEPRILAT", "MAGNESIUM CATION"))
    assert "AMLODIPINE" in out
    assert "MAGNESIUM CATION" not in out


def test_a_label_with_no_generic_name_still_works():
    """The rule degrades to plain moiety reading rather than dropping the label."""
    root = ET.fromstring(_SPL.format(
        generic="", moieties="<activeMoiety><activeMoiety><name>RAMIPRILAT</name>"
                             "</activeMoiety></activeMoiety>"))
    assert _extract_ingredients_from_root(root) == ["RAMIPRILAT"]

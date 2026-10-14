"""Homeopathic SPLs are not regulatory indications (#92).

235 canonical indication pairs came from DailyMed labels FDA marks as homeopathic, and the
combination fan-out (#88) multiplies them: 28 SPLs in the current corpus yield dozens of
drug nodes that exist for no other reason — skim milk, silicon dioxide, barium cation —
with edges like `coumarin -> fatigue` and `arsenite -> insomnia`.

Detection uses the SPL's own marketing-category code (`C73614`, "unapproved homeopathic"),
not a text match. Over the 1,976-label corpus the code and the "HOMEOPATHIC"/"HPUS" text
marker agree exactly — 28 files, neither signal alone missing any — so the structured field
is used as primary and the text only as a backstop for a label that states HPUS without
carrying the code.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from medic.ingest.dailymed.__main__ import _row_from_spl_root, is_homeopathic

_HEAD = """<?xml version="1.0" encoding="UTF-8"?>
<document xmlns="urn:hl7-org:v3">
  <setId root="11111111-2222-3333-4444-555555555555"/>
  {marker}
  <component><structuredBody><component><section>
    <code code="34067-9" codeSystem="2.16.840.1.113883.6.1"/>
    <text>{indication}</text>
    <ingredient><ingredientSubstance>
      <activeMoiety><activeMoiety><name>ARSENITE</name></activeMoiety></activeMoiety>
    </ingredientSubstance></ingredient>
  </section></component></structuredBody></component>
</document>"""

CATEGORY = ('<code code="C73614" codeSystem="2.16.840.1.113883.3.26.1.1" '
            'displayName="unapproved homeopathic"/>')


def _spl(marker="", indication="Indicated for insomnia."):
    return ET.fromstring(_HEAD.format(marker=marker, indication=indication))


def test_the_marketing_category_marks_a_label_homeopathic():
    assert is_homeopathic(_spl(marker=CATEGORY))


def test_an_ordinary_label_is_not_homeopathic():
    nda = '<code code="C73594" displayName="NDA"/>'
    assert not is_homeopathic(_spl(marker=nda))
    assert not is_homeopathic(_spl())


def test_hpus_wording_is_a_backstop_when_the_code_is_absent():
    """A label that states HPUS without carrying the category still gets caught."""
    assert is_homeopathic(_spl(indication="Active ingredients 3X HPUS. For insomnia."))


def test_the_word_homeopathic_in_the_text_is_enough():
    assert is_homeopathic(_spl(indication="HOMEOPATHIC remedy indicated for insomnia."))


def test_a_homeopathic_label_yields_no_mined_row():
    """The skip happens at the row level, so no indication, contraindication or drug node
    is created from the label at all."""
    assert _row_from_spl_root(_spl(marker=CATEGORY)) is None


def test_an_ordinary_label_still_yields_a_row():
    row = _row_from_spl_root(_spl(marker='<code code="C73594" displayName="NDA"/>'))
    assert row is not None
    assert row["drug_names"] == ["ARSENITE"]


def test_detection_does_not_fire_on_an_unrelated_word():
    """"home" / "pathic" substrings and the word in a different context must not match."""
    assert not is_homeopathic(_spl(indication="Indicated for idiopathic pulmonary fibrosis."))
    assert not is_homeopathic(_spl(indication="For home use after discharge."))

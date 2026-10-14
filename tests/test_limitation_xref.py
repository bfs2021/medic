"""A cross-reference to Limitations of Use is not a section boundary (#100, #65).

`split_dailymed_section` matched `Limitations of Use` anywhere in the text, including inside
a bracketed pointer to another section — `[see Limitations of Use (1.2)]`. On the KABIVEN
parenteral-nutrition label that cut the contraindications section at `[see`, leaving 290
characters of shipped `supporting_text` ending mid-bracket, and relabelled the following
1,154 characters of ordinary contraindication text as a `LIMITATION_STATEMENT`.

That is the real cause of what #100 reports as an unreliable truncation flag: no cap was
hit, so flagging was arguably correct — the text was cut by our own splitter. One label, but
the ingredient fan-out turns it into the 50 edges the issue counted.

Measured over the corpus: 617 marker occurrences in section chunks, of which exactly 1 is a
cross-reference. The fix is therefore narrow by construction and cannot disturb the 616
genuine subsection headers.
"""

from __future__ import annotations

from medic.spans import split_dailymed_section

# Shape of the KABIVEN contraindications section.
XREF = (
    "CONTRAINDICATIONS Known hypersensitivity to any of the ingredients. Concomitant "
    "treatment with ceftriaxone, due to the risk of fatal ceftriaxone calcium salt "
    "precipitation in the neonate's bloodstream [see Limitations of Use (1.2)]. "
    "Hepatic insufficiency. Severe renal impairment."
)

HEADER = (
    "1 INDICATIONS AND USAGE PRODUCT is indicated for migraine. "
    "Limitations of Use PRODUCT is not indicated for cluster headache."
)


def _roles(text):
    return [s["role"] for s in split_dailymed_section(text, document="d", section_code="x")]


def test_a_cross_reference_does_not_split_the_section():
    assert "LIMITATION_STATEMENT" not in _roles(XREF)
    assert "SUBSECTION_HEADER" not in _roles(XREF)


def test_the_section_text_is_not_cut_at_the_bracket():
    spans = split_dailymed_section(XREF, document="d", section_code="x")
    body = " ".join(s["text"] for s in spans)
    assert "Severe renal impairment" in body
    assert not body.rstrip().endswith("[see")


def test_the_text_after_the_cross_reference_is_not_a_limitation():
    """1,154 characters of ordinary contraindication text were typed as a scope
    restriction, which also put them outside the claim's negation scope."""
    spans = split_dailymed_section(XREF, document="d", section_code="x")
    limitation = " ".join(
        s["text"] for s in spans if s["role"] == "LIMITATION_STATEMENT")
    assert "Hepatic insufficiency" not in limitation


def test_a_genuine_subsection_header_still_splits():
    """616 of the 617 marker occurrences are real headers; none may be disturbed.

    No SECTION_HEADER span here: that is matched only at position 0, and this fixture
    opens with the section number ("1 INDICATIONS AND USAGE"), as real SPL text does.
    """
    assert _roles(HEADER) == [
        "SECTION_TEXT", "SUBSECTION_HEADER", "LIMITATION_STATEMENT"]


def test_a_bare_see_reference_also_does_not_split():
    text = ("PRODUCT is indicated for migraine, with dosing adjusted in renal impairment "
            "see Limitations of Use below for the excluded population.")
    assert "LIMITATION_STATEMENT" not in _roles(text)


def test_splitting_is_still_lossless():
    spans = split_dailymed_section(XREF, document="d", section_code="x")
    assert " ".join(s["text"] for s in spans) == " ".join(XREF.split())

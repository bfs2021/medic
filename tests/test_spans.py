"""Typed source spans (design spec D6, §4.3)."""

import pytest

from medic.spans import spans_for_source, split_dailymed_section

UBRELVY = (
    "UBRELVY is indicated for the acute treatment of migraine with or without aura in "
    "adults. Limitations of Use UBRELVY is not indicated for the preventive treatment of "
    "migraine."
)
WITH_HEADER = (
    "INDICATIONS AND USAGE Ipratropium Bromide and Albuterol Sulfate Inhalation Solution is "
    "indicated for the treatment of bronchospasm associated with COPD."
)


def test_a_limitations_of_use_sentence_becomes_its_own_typed_span():
    spans = split_dailymed_section(
        UBRELVY, document="DailyMed:fd9f9458", section_code="LOINC:34067-9")
    roles = [s["role"] for s in spans]
    assert roles == ["SECTION_TEXT", "SUBSECTION_HEADER", "LIMITATION_STATEMENT"]
    assert spans[0]["text"] == (
        "UBRELVY is indicated for the acute treatment of migraine with or without aura in "
        "adults.")
    assert spans[1]["text"] == "Limitations of Use"
    assert spans[2]["text"] == (
        "UBRELVY is not indicated for the preventive treatment of migraine.")
    assert all(s["document"] == "DailyMed:fd9f9458" for s in spans)
    assert all(s["section_code"] == "LOINC:34067-9" for s in spans)


def test_a_leading_section_header_is_split_off():
    spans = split_dailymed_section(
        WITH_HEADER, document="DailyMed:001e3b1c", section_code="LOINC:34067-9")
    assert [s["role"] for s in spans] == ["SECTION_HEADER", "SECTION_TEXT"]
    assert spans[0]["text"] == "INDICATIONS AND USAGE"
    assert spans[1]["text"].startswith("Ipratropium Bromide")


def test_plain_text_is_one_section_text_span():
    spans = split_dailymed_section(
        "Foo is indicated for bar.", document="DailyMed:x", section_code="LOINC:34067-9")
    assert [s["role"] for s in spans] == ["SECTION_TEXT"]


def test_the_concatenation_is_lossless():
    """Splitting must not drop or invent text — I-7."""
    spans = split_dailymed_section(UBRELVY, document="d", section_code="s")
    assert " ".join(s["text"] for s in spans) == UBRELVY


def test_no_character_is_lost_across_the_real_dailymed_corpus():
    """The guarantee is content-lossless, not byte-lossless.

    89 of 4,024 real records have runs of whitespace at a split boundary, so rejoining with a
    single space differs from the input by whitespace alone. That is acceptable; dropping or
    inventing a character is not (I-7).
    """
    import re
    from pathlib import Path

    import pytest
    import yaml

    corpus = Path("kb/indications/dailymed/indications.yaml")
    if not corpus.exists():
        pytest.skip("the DailyMed ingest intermediate is untracked (1aebf63); needs a local ingest")
    records = yaml.safe_load(corpus.open())
    for record in records:
        text = (record.get("indications_text") or "").strip()
        if not text:
            continue
        spans = split_dailymed_section(text, document="d", section_code="s")
        joined = " ".join(s["text"] for s in spans)
        assert re.sub(r"\s+", "", joined) == re.sub(r"\s+", "", text), text[:120]


def test_a_header_plus_limitation_is_still_lossless():
    text = WITH_HEADER + " Limitations of Use Not for chronic use."
    spans = split_dailymed_section(text, document="d", section_code="s")
    assert " ".join(s["text"] for s in spans) == text
    assert [s["role"] for s in spans] == [
        "SECTION_HEADER", "SECTION_TEXT", "SUBSECTION_HEADER", "LIMITATION_STATEMENT"]


@pytest.mark.parametrize("source,expected", [
    ("DAILYMED", "SECTION_TEXT"),
    ("EMA", "STRUCTURED_FIELD"),
    ("PMDA", "SECTION_TEXT"),
    ("INDIA", "TABLE_CELL"),
    ("CDSCO", "TABLE_CELL"),
    ("SOMETHING_NEW", "UNKNOWN"),
])
def test_each_source_maps_to_its_role(source, expected):
    spans = spans_for_source(source, "Indicated for anxiety.", document="d", section_code="")
    assert spans[-1]["role"] == expected


def test_a_source_with_no_text_emits_no_spans():
    """Russia carries no indication text; an empty span is worse than none."""
    assert spans_for_source("RUSSIA", "", document="d", section_code="") == []
    assert spans_for_source("DAILYMED", "   ", document="d", section_code="") == []


def test_section_code_is_omitted_when_unknown_rather_than_empty():
    spans = spans_for_source("INDIA", "Indicated for anxiety.", document="d", section_code="")
    assert "section_code" not in spans[0]


def test_every_role_emitted_is_a_valid_enum_value():
    """Plan 1 shipped 11,268 invalid enum values; pin the vocabulary at the source."""
    from linkml_runtime import SchemaView

    sv = SchemaView("src/medic/schema/provenance.yaml")
    valid = set(sv.get_enum("TextSpanRoleEnum").permissible_values)
    produced = set()
    for source in ("DAILYMED", "EMA", "PMDA", "INDIA", "CDSCO", "WHO_KNOWS"):
        for text in (UBRELVY, WITH_HEADER, "Indicated for anxiety."):
            produced.update(s["role"] for s in spans_for_source(
                source, text, document="d", section_code="s"))
    assert produced <= valid, produced - valid

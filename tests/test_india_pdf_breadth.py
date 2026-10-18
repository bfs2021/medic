"""The CDSCO annual lists are multi-page tables; only page one was being read.

India shipped **112** drugs where v1.0.0's `india.csv` had **3,118** — the largest
single-source regression in the rebuild.

The cause is one line. A multi-page table repeats its header only on the first page, and a
table with no recognised header was `continue`d past, so every continuation page was
discarded whole. india_2019.pdf has five pages of ~27 usable rows and yielded 27 records
total. Carrying the column map forward from the last page that had a header takes the source
to **1,048 records**, with clean names, complete indications and correct dates.

**What did not work, recorded so it is not retried.** pdfplumber's "text" table strategy
finds far more raw rows on these pages — 56-85 against 8-13 — and every one is a fragment:
it splits on whitespace alignment and clips at fixed character positions, producing names
like "&Letermovir Ta" and "240mg and 480" with the dates lost entirely. Selecting the
strategy by record count actively prefers it, because shredding a page produces more rows.
That would have shipped 2,133 fragments in place of 112 good records, which is worse than
the regression it was meant to fix. An existing test (`test_parse_india_pdf_extracts_records`)
caught it, on the dates.

india_2014.pdf still contributes zero, correctly: it is a different document, a BA/BE
reference-product list with no Indication column, so the header check rejects it rather than
coercing it into drug-indication pairs.
"""

from __future__ import annotations

from medic.ingest.india.parse_pdf import _TABLE_SETTINGS, _records_from_tables

_HEADER = ["S.No", "Name of drug", "Indication", "Date of issue"]
_ROW_A = ["1.", "Alpelisib tablets", "metastatic breast cancer", "04.02.2019"]
_ROW_B = ["2.", "Azelnidipine tablets", "Stage I Hypertension", "05.03.2019"]


def test_a_header_page_parses_and_reports_its_column_map():
    recs, cols = _records_from_tables([[_HEADER, _ROW_A]], page_idx=0, default_year=2019)
    assert [r["source_name"] for r in recs] == ["Alpelisib tablets"]
    assert cols == {"sno": 0, "name": 1, "indication": 2, "date": 3}


def test_a_continuation_page_uses_the_inherited_column_map():
    """The whole bug: this page has no header, and without the inherited map every row on it
    was discarded."""
    cols = {"sno": 0, "name": 1, "indication": 2, "date": 3}
    recs, _ = _records_from_tables(
        [[_ROW_A, _ROW_B]], page_idx=1, default_year=2019, inherited_cols=cols)
    assert [r["source_name"] for r in recs] == ["Alpelisib tablets", "Azelnidipine tablets"]
    assert recs[0]["indication"] == "metastatic breast cancer"


def test_a_continuation_page_without_an_inherited_map_is_still_skipped():
    """No header and no prior page means no way to know which column is which. Guessing
    would invent drug/indication pairs out of column order, which is worse than dropping
    the page."""
    recs, cols = _records_from_tables([[_ROW_A, _ROW_B]], page_idx=1, default_year=2019)
    assert recs == []
    assert cols is None


def test_the_first_row_is_data_when_the_map_is_inherited():
    """Off-by-one guard: with a real header the first data row is header_idx+1, but on a
    continuation page row 0 is already data and must not be eaten."""
    cols = {"name": 1, "indication": 2}
    recs, _ = _records_from_tables(
        [[_ROW_A]], page_idx=1, default_year=2019, inherited_cols=cols)
    assert len(recs) == 1


def test_a_table_whose_header_has_no_indication_column_is_rejected():
    """india_2014.pdf is a BA/BE reference-product list, not an approval list. It should
    contribute nothing rather than being coerced into drug-indication pairs."""
    header = ["S.No", "Name of drug", "Manufacturer"]
    recs, cols = _records_from_tables(
        [[header, ["1.", "Alogliptin Tablet", "M/s. Takeda"]]],
        page_idx=0, default_year=2014)
    assert recs == []
    assert cols is None


def test_only_the_ruled_strategy_is_used():
    """Pins the decision not to add pdfplumber's "text" strategy. It finds more rows and
    every one is a fragment; see the module docstring."""
    assert _TABLE_SETTINGS["vertical_strategy"] == "lines"
    assert _TABLE_SETTINGS["horizontal_strategy"] == "lines"


def test_an_empty_or_header_only_table_yields_nothing():
    for table in ([], [_HEADER], [[]]):
        recs, _ = _records_from_tables([table], page_idx=0, default_year=2019)
        assert recs == []

"""The CDSCO annual lists are text-aligned tables spanning many pages.

India shipped **112** drugs where v1.0.0's `india.csv` had **3,118** — the largest
single-source regression in the rebuild, and the one Nico flagged as concerning. Two causes,
both in the PDF reader rather than anywhere downstream:

1. **The table strategy required ruled borders.** `vertical_strategy: "lines"` finds 8-13
   rows on a sampled page where `"text"` finds 56-85, because the ruling in these documents
   is partial or absent.
2. **Continuation pages were skipped whole.** A multi-page table repeats its header only on
   the first page, and a table with no recognised header was `continue`d past — so only page
   one of each PDF produced anything. india_2019.pdf has 5 pages of ~76 rows and yielded 7
   records.

Fixed: both strategies are tried per page and the better *record* yield wins (not the better
raw-row yield, since a strategy can shred a page into unusable fragments), and the column map
from the last page that had a header is carried forward.

Result: 112 -> 2,132 records, 1,793 unique drug names, 1,434 carrying an indication. Spot
checks are genuine pairs — omeprazole -> duodenal ulcer, cidofovir -> CMV retinitis,
alpelisib -> metastatic breast cancer.

What is still short of v1: ~3,118 vs 2,132. `india_2014.pdf` legitimately contributes zero —
it is a different document ("Preliminary List of Products Permitted ... for BA/BE Studies")
with no Indication column at all, so the header check correctly rejects it.
"""

from __future__ import annotations

from medic.ingest.india.parse_pdf import (
    _TABLE_STRATEGIES,
    _records_from_tables,
)

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


def test_both_strategies_are_configured_and_lines_is_tried_first():
    """`lines` is the more precise where ruling exists, so it keeps priority; `text` is the
    fallback that recovers the unruled majority."""
    assert len(_TABLE_STRATEGIES) == 2
    assert _TABLE_STRATEGIES[0]["vertical_strategy"] == "lines"
    assert _TABLE_STRATEGIES[1]["vertical_strategy"] == "text"


def test_an_empty_or_header_only_table_yields_nothing():
    for table in ([], [_HEADER], [[]]):
        recs, _ = _records_from_tables([table], page_idx=0, default_year=2019)
        assert recs == []

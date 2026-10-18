"""Parse India CDSCO annual approval PDFs into structured records.

CDSCO publishes one PDF per year with a tabular layout:
  - S.No.
  - Name of New Drug
  - Indication
  - Date of Approval

Older PDFs (pre-2015 or so) sometimes use slightly different column names or
have multi-column layouts. We use header-based column detection plus the
`lines/lines` extraction strategy which best matches the bordered tables.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pdfplumber

logger = logging.getLogger(__name__)

#: Ruled-border table detection. Deliberately the only strategy.
#:
#: pdfplumber's "text" strategy finds far more raw rows on these pages (56-85 against 8-13)
#: and every one of them is a fragment: it splits on whitespace alignment and clips at fixed
#: character positions, so a 2025 page yields names like "&Letermovir Ta" and "240mg and 480"
#: with the dates lost entirely. More rows, no records. The real shortfall was never the
#: strategy — it was that continuation pages were being skipped whole (see parse_india_pdf).
_TABLE_SETTINGS = {
    "vertical_strategy": "lines",
    "horizontal_strategy": "lines",
}

_TABLE_STRATEGIES = (_TABLE_SETTINGS,)


def _clean_cell(value: str | None) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", value.replace("\n", " ")).strip()


def _identify_columns(header_row: list[str | None]) -> dict[str, int]:
    """Map normalized header text -> column index."""
    out: dict[str, int] = {}
    for i, h in enumerate(header_row or []):
        if not h:
            continue
        lo = re.sub(r"\s+", " ", h.lower())
        if "drug" in lo or "name" in lo:
            out.setdefault("name", i)
        elif "indication" in lo:
            out.setdefault("indication", i)
        elif "approval" in lo or "date" in lo:
            out.setdefault("date", i)
        elif lo.startswith("s.no") or lo == "no" or lo == "no." or lo == "sl":
            out.setdefault("sno", i)
    return out


def _is_header_row(row: list[str | None]) -> bool:
    text = " ".join((c or "") for c in row).lower()
    keys = ("drug", "indication")
    return all(key in text for key in keys)


def _parse_date(date_str: str, default_year: int = 0) -> str:
    """Convert various Indian date formats to YYYYMMDD."""
    s = (date_str or "").strip()
    if not s:
        return ""
    # DD.MM.YYYY (most common in CDSCO PDFs)
    m = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
    if m:
        return f"{m.group(3)}{int(m.group(2)):02d}{int(m.group(1)):02d}"
    # DD/MM/YYYY or DD-MM-YYYY
    m = re.match(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", s)
    if m:
        return f"{m.group(3)}{int(m.group(2)):02d}{int(m.group(1)):02d}"
    # YYYY-MM-DD
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return f"{m.group(1)}{int(m.group(2)):02d}{int(m.group(3)):02d}"
    # Year-only or unparseable - fall back to default year
    if default_year:
        return f"{default_year}0101"
    return ""


def parse_india_pdf(pdf_path: Path, default_year: int = 0) -> list[dict]:
    """Extract approval records from a single CDSCO annual PDF."""
    records: list[dict] = []
    carried_cols: dict[str, int] | None = None
    with pdfplumber.open(pdf_path) as pdf:
        for page_idx, page in enumerate(pdf.pages):
            best, best_cols = _records_from_tables(
                page.extract_tables(_TABLE_SETTINGS) or [], page_idx, default_year,
                inherited_cols=carried_cols)
            records.extend(best)
            # A multi-page table repeats its header only on the first page, so continuation
            # pages have none and were skipped whole — which is why only page 1 of each PDF
            # produced anything. Carry the mapping forward.
            if best_cols:
                carried_cols = best_cols
    logger.info("Parsed %d records from %s (year=%s)", len(records), pdf_path.name, default_year)
    return records


def _records_from_tables(
    tables: list, page_idx: int, default_year: int,
    inherited_cols: dict[str, int] | None = None,
) -> tuple[list[dict], dict[str, int] | None]:
    """Parse one page's tables into records, and return the column map that worked.

    ``inherited_cols`` is the mapping found on an earlier page of the same PDF, used when a
    table carries no header of its own — the continuation-page case.
    """
    records: list[dict] = []
    cols_used: dict[str, int] | None = None
    for table in tables:
        # Two rows minimum only when a header is expected. With an inherited column map a
        # single-row continuation table is valid data — the last page of a long table often
        # carries one row.
        if not table or len(table) < (1 if inherited_cols else 2):
            continue
        header_idx = None
        for i, row in enumerate(table[:3]):
            if row and _is_header_row(row):
                header_idx = i
                break
        if header_idx is None:
            if not inherited_cols:
                continue
            cols, first_data = inherited_cols, 0
        else:
            cols = _identify_columns(table[header_idx])
            first_data = header_idx + 1
        if "name" not in cols or "indication" not in cols:
            continue
        cols_used = cols
        for row in table[first_data:]:
            if not row or all((c or "").strip() == "" for c in row):
                continue
            name = _clean_cell(row[cols["name"]] if cols["name"] < len(row) else "")
            if not name or name.lower() in ("s.no.", "s.no"):
                continue
            indication = _clean_cell(
                row[cols["indication"]] if cols["indication"] < len(row) else ""
            )
            d = cols.get("date", -1)
            date_cell = _clean_cell(row[d] if 0 <= d < len(row) else "")
            approval_date = _parse_date(date_cell, default_year=default_year)
            n = cols.get("sno", -1)
            sno = _clean_cell(row[n] if 0 <= n < len(row) else "")
            records.append({
                "source": "INDIA",
                "source_name": name,
                "indication": indication,
                "snippet": indication,
                "approval_date": approval_date,
                "serial_no": sno,
                "page_index": page_idx,
                "source_year": default_year,
            })
    return records, cols_used


def parse_all_pdfs(pdf_dir: Path) -> list[dict]:
    """Parse all CDSCO annual PDFs in a directory."""
    all_records: list[dict] = []
    for pdf_path in sorted(pdf_dir.glob("*.pdf")):
        m = re.search(r"(19|20)\d{2}", pdf_path.name)
        default_year = int(m.group(0)) if m else 0
        try:
            all_records.extend(parse_india_pdf(pdf_path, default_year=default_year))
        except Exception as e:
            logger.warning("Failed %s: %s", pdf_path, e)
    return all_records


def deduplicate_by_drug_name(records: list[dict]) -> list[dict]:
    """Keep earliest approval per drug name; combine snippets for repeats."""
    by_name: dict[str, dict] = {}
    for r in records:
        key = (r.get("source_name", "") or "").strip().upper()
        if not key:
            continue
        existing = by_name.get(key)
        if existing is None:
            existing = {
                "source": "INDIA",
                "source_name": r["source_name"],
                "approval_date": r.get("approval_date", ""),
                "indication": r.get("indication", ""),
                "snippets": [],
                "source_years": [],
            }
            by_name[key] = existing
        snippet = (r.get("snippet") or "").strip()
        if snippet and snippet not in existing["snippets"]:
            existing["snippets"].append(snippet)
        new_date = r.get("approval_date", "")
        if new_date and (not existing["approval_date"] or new_date < existing["approval_date"]):
            existing["approval_date"] = new_date
            existing["indication"] = r.get("indication", existing["indication"])
        year = r.get("source_year", 0)
        if year and year not in existing["source_years"]:
            existing["source_years"].append(year)
    out = []
    for rec in by_name.values():
        rec["indication"] = " | ".join(rec["snippets"]) if rec["snippets"] else rec.get("indication", "")
        rec["snippet"] = rec["snippets"][0] if rec["snippets"] else ""
        out.append(rec)
    return out

"""EMA (European Medicines Agency) drug ingest.

Contraindication extraction
---------------------------
The EMA medicines XLSX (`medicines-output-medicines-report_en.xlsx`) only
carries the *Therapeutic indication* free-text column — it does NOT contain
contraindication text. Real EMA contraindications live in the per-product
EPAR Product Information PDF, downloadable from each medicine's landing page::

    https://www.ema.europa.eu/en/medicines/human/EPAR/<medicine-slug>

The PDF has a stable filename pattern::

    <medicine-slug>-epar-product-information_en.pdf

Inside the PDF, the "ANNEX I — SUMMARY OF PRODUCT CHARACTERISTICS" section
always contains a "4.3 Contraindications" subsection.

The extraction is opt-in (``--extract-contras``) because running it across all
~2,500 EMA medicines requires hours of PDF download + LLM disease extraction.
Implementation lives in :func:`_extract_contraindications` and uses the
helpers in ``fetch_epar.py`` and ``parse_epar.py`` in this package. Output is
written to ``kb/indications/ema/contraindications.yaml`` (the on-label merge
in ``merge/on_label_merge.py`` discovers any \\*.yaml under
``kb/indications/<source>/`` and routes by ``relationship_type``).
"""

import logging
from pathlib import Path

import pandas as pd
import typer
import yaml

from medic.grounding.cache import GroundingCache
from medic.grounding.factory import get_grounding_service
from medic.ingest.common import (
    download_file,
    load_source_urls,
    looks_like_disease_name,
    reformat_date,
    standardize_columns,
    write_drug_source_yaml,
    write_grounding_report,
)
from medic.ingest.ema.fetch_epar import fetch_epar_pdf
from medic.ingest.ema.parse_epar import extract_contraindications_text
from medic.ingest.grounding import ground_records
from medic.ingest.sanity import check_row_floor, record_source
from medic.spans import SNIPPET_CHAR_CAP

logger = logging.getLogger(__name__)

app = typer.Typer()

KB_INDICATIONS_DIR = Path("kb/indications/ema")

# EMA EPAR landing pages follow:
#   https://www.ema.europa.eu/en/medicines/human/EPAR/<slug>
# Their "Product information" PDF (the regulatory document equivalent to a
# DailyMed SPL) is published at a deterministic path keyed on the same slug:
#   https://www.ema.europa.eu/en/documents/product-information/<slug>-epar-product-information_en.pdf
# Verified via WebFetch on 2026-05-02 with slug "keppra".
_EPAR_LANDING_PREFIX = "https://www.ema.europa.eu/en/medicines/human/EPAR/"
_PRODUCT_INFO_URL_TEMPLATE = (
    "https://www.ema.europa.eu/en/documents/product-information/"
    "{slug}-epar-product-information_en.pdf"
)


def _epar_product_information_url(epar_url: str) -> str:
    """Derive the EPAR product information PDF URL from an EPAR landing URL.

    Returns "" if the input is not a recognizable EPAR landing URL.
    """
    if not epar_url:
        return ""
    url = epar_url.strip().rstrip("/")
    if not url.lower().startswith(_EPAR_LANDING_PREFIX.lower()):
        return ""
    slug = url[len(_EPAR_LANDING_PREFIX):].split("/")[0].split("?")[0].strip()
    if not slug:
        return ""
    return _PRODUCT_INFO_URL_TEMPLATE.format(slug=slug.lower())


def parse_ema(raw_path: Path) -> list[dict]:
    """Parse EMA XLSX and return standardized records.

    Filters to Human + Authorised medicines, groups by INN name, uses earliest
    approval date per drug name.

    Args:
        raw_path: Path to the EMA Excel file.

    Returns:
        List of dicts with keys: source, source_name, approval_date, atc_code, indication.
    """
    # EMA XLSX has metadata rows before the actual header
    df = pd.read_excel(raw_path, dtype=str)
    if "Category" not in df.columns:
        # Try skipping header rows (EMA puts metadata in rows 0-7)
        for skip in range(1, 15):
            df = pd.read_excel(raw_path, skiprows=skip, dtype=str)
            if "Category" in df.columns:
                break

    # Filter to Human + Authorised
    if "Category" in df.columns:
        df = df[df["Category"].str.strip().str.lower() == "human"]
    if "Medicine status" in df.columns:
        df = df[df["Medicine status"].str.strip().str.lower() == "authorised"]

    # Map columns
    col_mapping = {
        "International non-proprietary name (INN) / common name": "source_name",
        "Marketing authorisation date": "approval_date",
        "ATC code (human)": "atc_code",
        "Therapeutic indication": "indication",
        "Medicine URL": "epar_url",
        "Name of medicine": "product_name",
        "EMA product number": "ema_product_number",
    }
    df = standardize_columns(df, col_mapping)

    # Reformat dates (DD/MM/YYYY)
    if "approval_date" in df.columns:
        df["approval_date"] = df["approval_date"].apply(reformat_date)

    # Drop rows with no source_name
    if "source_name" not in df.columns:
        logger.warning("source_name column not found in EMA data")
        return []

    df = df.dropna(subset=["source_name"])
    df = df[df["source_name"].str.strip() != ""]

    # Ensure optional columns exist
    for col in ("atc_code", "indication", "epar_url", "product_name", "ema_product_number"):
        if col not in df.columns:
            df[col] = ""

    # Group by source_name
    grouped = df.groupby("source_name", sort=False)

    records = []
    for drug_name, group in grouped:
        # Earliest approval date
        dates = [d for d in group["approval_date"] if d] if "approval_date" in group.columns else []
        earliest_date = min(dates) if dates else ""

        # Join ATC codes and indications
        atc_codes = " | ".join(group["atc_code"].dropna().unique().tolist())
        indications = " | ".join(group["indication"].dropna().unique().tolist())

        # First non-empty EPAR URL and product number
        epar_urls = [u for u in group["epar_url"].dropna().tolist() if u and str(u).strip()]
        epar_url = epar_urls[0] if epar_urls else ""
        product_names = [p for p in group["product_name"].dropna().tolist() if p and str(p).strip()]
        product_name = product_names[0] if product_names else ""
        ema_numbers = [n for n in group["ema_product_number"].dropna().tolist() if n and str(n).strip()]
        ema_number = ema_numbers[0] if ema_numbers else ""

        # Per-product indication entries (drug -> indication text per row)
        indication_entries = []
        for _, row in group.iterrows():
            ind_text = str(row.get("indication", "") or "").strip()
            row_url = str(row.get("epar_url", "") or "").strip()
            row_product = str(row.get("product_name", "") or "").strip()
            if ind_text:
                indication_entries.append({
                    "indication_text": ind_text,
                    "epar_url": row_url,
                    "product_name": row_product,
                })

        records.append({
            "source": "EMA",
            "source_name": str(drug_name),
            "approval_date": earliest_date,
            "atc_code": atc_codes,
            "indication": indications,
            "epar_url": epar_url,
            "product_name": product_name,
            "ema_product_number": ema_number,
            "indication_entries": indication_entries,
        })

    logger.info("Parsed %d unique drugs from EMA", len(records))
    return records


def _build_ema_indication_records(grounded_drugs: list[dict], grounding_backend: str) -> list[dict]:
    """Extract structured EMA indications from drug records.

    For each grounded EMA drug with indication text, runs LLM disease extraction
    (reusing the DailyMed function), grounds each disease to MONDO, and emits an
    indication record with EPAR URL as the regulatory document reference.
    """
    from medic.ingest.dailymed.__main__ import (
        extract_diseases_from_text,
        record_claim_context,
    )
    from medic.grounding.factory import get_grounding_service
    from medic.ingest.grounding import flush_disease_grounding, resolve_disease_onto_record

    service = get_grounding_service(grounding_backend)

    indication_records: list[dict] = []
    drugs_processed = 0
    drugs_with_indications = 0

    for drug_rec in grounded_drugs:
        drug_id = drug_rec.get("normalized_id", "")
        drug_label = drug_rec.get("normalized_label", "") or drug_rec.get("source_name", "")
        if not drug_id or drug_rec.get("grounding_status") == "unresolved":
            continue

        drugs_processed += 1
        epar_url = (drug_rec.get("epar_url", "") or "").strip()
        approval_date = drug_rec.get("approval_date", "") or ""

        # Iterate per-product indication entries (preserves URL alignment)
        entries = drug_rec.get("indication_entries", []) or []
        if not entries:
            ind_text = (drug_rec.get("indication", "") or "").strip()
            if ind_text:
                entries = [{
                    "indication_text": ind_text,
                    "epar_url": epar_url,
                    "product_name": drug_rec.get("product_name", ""),
                }]

        for entry in entries:
            ind_text = entry.get("indication_text", "")
            if not ind_text:
                continue
            entry_url = (entry.get("epar_url", "") or epar_url).strip()
            try:
                diseases = extract_diseases_from_text(ind_text, source="EMA")
            except Exception as e:
                logger.warning("Disease extraction failed for %s: %s", drug_label, e)
                diseases = []
            if diseases:
                drugs_with_indications += 1
            for _claim in diseases:
                disease_name, disease_verbatim = (
                    _claim.target, _claim.target_verbatim)
                record: dict = {}
                try:
                    disease_id = resolve_disease_onto_record(record, disease_name, service)
                except Exception as e:
                    logger.warning("Grounding failed for %s: %s", disease_name, e)
                    continue
                if not disease_id:
                    continue
                disease_label = record["final_normalized_disease_label"]

                evidence_item = {
                    "source_type": "REGULATORY",
                    "jurisdiction": "EU",
                    "confidence": "HIGH",
                    "approval_status": "APPROVED",
                    "source_role": "PRIMARY",
                    "explanation": "EMA-approved indication from EMA medicines spreadsheet (primary source)",
                    "snippet": ind_text[:SNIPPET_CHAR_CAP],
                    "original_drug_label": drug_rec.get("source_name", "") or drug_label,
                    "original_disease_label": disease_name,
                    "original_disease_verbatim": disease_verbatim,
                }
                record_claim_context(evidence_item, _claim, grounding_backend)
                ema_product_number = (drug_rec.get("ema_product_number", "") or "").strip()
                if ema_product_number:
                    evidence_item["original_drug_id"] = ema_product_number
                if entry_url:
                    evidence_item["reference"] = entry_url
                    pdf_url = _epar_product_information_url(entry_url)
                    if pdf_url:
                        evidence_item["source_document_url"] = pdf_url
                if approval_date:
                    evidence_item["approval_date"] = approval_date

                record.update({
                    "drug_disease": f"{drug_id}|{disease_id}",
                    "final_normalized_drug_id": drug_id,
                    "final_normalized_drug_label": drug_label,
                    "final_normalized_disease_id": disease_id,
                    "final_normalized_disease_label": disease_label,
                    "fda": False,
                    "ema": True,
                    "pmda": False,
                    "relationship_type": "INDICATION",
                    "indications_text": ind_text,
                    "evidence": [evidence_item],
                })
                indication_records.append(record)

    flush_disease_grounding(service)
    logger.info(
        "EMA indication extraction: %d drugs processed, %d had indications, %d records produced",
        drugs_processed, drugs_with_indications, len(indication_records),
    )
    return indication_records


def _write_ema_indications(records: list[dict]) -> None:
    """Write EMA indication records to kb/indications/ema/indications.yaml."""
    KB_INDICATIONS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = KB_INDICATIONS_DIR / "indications.yaml"
    content = yaml.dump(records, default_flow_style=False, allow_unicode=True, width=1000)
    content = "".join(c for c in content if c == "\n" or c == "\t" or ord(c) >= 32)
    with open(out_path, "w") as f:
        f.write(content)
    logger.info("Wrote %d EMA indications to %s", len(records), out_path)


# ---------------------------------------------------------------------------
# Contraindication extraction (opt-in via --extract-contras)
# ---------------------------------------------------------------------------


def _epar_slug_from_url(epar_url: str) -> str:
    """Derive the EPAR slug from an EMA medicine landing URL.

    Returns "" if the URL is not a recognizable EPAR landing URL.
    """
    if not epar_url:
        return ""
    url = epar_url.strip().rstrip("/")
    if not url.lower().startswith(_EPAR_LANDING_PREFIX.lower()):
        return ""
    slug = url[len(_EPAR_LANDING_PREFIX):].split("/")[0].split("?")[0].strip()
    return slug.lower()


#: Backwards-compatible alias. The guard moved to ``medic.ingest.common`` and now runs
#: inside ``_parse_llm_disease_list``, so every source gets it — not just the EU path
#: (issue #59). Kept as a name here because the §4.3 path reads it directly.
_looks_like_disease_name = looks_like_disease_name


def _extract_contraindications(
    grounded_records: list[dict],
    grounding_backend: str,
) -> list[dict]:
    """Extract contraindications from EMA EPAR Product Information PDFs.

    For each grounded EMA drug record:
      1. Derive the EPAR slug from ``epar_url``.
      2. Download (or read from cache) the EPAR Product Information PDF.
      3. Extract §4.3 Contraindications text.
      4. Run ``extract_diseases_from_text`` on the section text and filter
         the result with :func:`_looks_like_disease_name` (the helper's prompt
         is tuned for indications and can leak refusal prose).
      5. Ground each disease and emit a CONTRAINDICATION record.

    Returns the list of contraindication records (same shape as DailyMed).
    """
    from medic.ingest.dailymed.__main__ import (
        extract_contraindicated_diseases_from_text,
        record_claim_context,
    )
    from medic.ingest.grounding import flush_disease_grounding, resolve_disease_onto_record

    service = get_grounding_service(grounding_backend)

    contraindication_records: list[dict] = []
    drugs_processed = 0
    drugs_with_pdf = 0
    drugs_with_section = 0

    for drug_rec in grounded_records:
        drug_id = drug_rec.get("normalized_id", "")
        drug_label = (
            drug_rec.get("normalized_label", "")
            or drug_rec.get("source_name", "")
        )
        if not drug_id or drug_rec.get("grounding_status") == "unresolved":
            continue

        epar_url = (drug_rec.get("epar_url", "") or "").strip()
        slug = _epar_slug_from_url(epar_url)
        if not slug:
            continue

        drugs_processed += 1
        approval_date = drug_rec.get("approval_date", "") or ""

        try:
            pdf_path = fetch_epar_pdf(slug)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed fetching EPAR PDF for slug=%s: %s", slug, exc)
            continue
        if not pdf_path:
            continue
        drugs_with_pdf += 1

        try:
            contras_text = extract_contraindications_text(pdf_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed parsing §4.3 from %s (slug=%s): %s",
                pdf_path, slug, exc,
            )
            continue
        if not contras_text:
            continue
        drugs_with_section += 1

        try:
            raw_diseases = extract_contraindicated_diseases_from_text(
                contras_text, source="EMA")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Disease extraction failed for slug=%s drug=%s: %s",
                slug, drug_label, exc,
            )
            continue

        # The LLM helper is tuned for indications; filter refusal prose etc.
        diseases = [d for d in raw_diseases if _looks_like_disease_name(d.name)]
        if len(diseases) < len(raw_diseases):
            logger.info(
                "Filtered %d non-disease strings from §4.3 extraction (slug=%s)",
                len(raw_diseases) - len(diseases), slug,
            )

        pdf_url = _PRODUCT_INFO_URL_TEMPLATE.format(slug=slug)
        for _claim in diseases:
            disease_name, disease_verbatim = _claim.target, _claim.target_verbatim
            record: dict = {}
            try:
                disease_id = resolve_disease_onto_record(record, disease_name, service)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Grounding failed for disease=%s (slug=%s): %s",
                    disease_name, slug, exc,
                )
                continue
            if not disease_id:
                continue
            disease_label = record["final_normalized_disease_label"]

            evidence_item = {
                "source_type": "REGULATORY",
                "jurisdiction": "EU",
                "confidence": "HIGH",
                "approval_status": "APPROVED",
                "source_role": "PRIMARY",
                "explanation": (
                    "EMA contraindication from EPAR Product Information SmPC §4.3"
                ),
                "snippet": contras_text[:SNIPPET_CHAR_CAP],
                "original_drug_label": drug_rec.get("source_name", "") or drug_label,
                "original_disease_label": disease_name,
                "original_disease_verbatim": disease_verbatim,
                "reference": epar_url,
                "source_document_url": pdf_url,
            }
            record_claim_context(evidence_item, _claim, grounding_backend)
            ema_product_number = (drug_rec.get("ema_product_number", "") or "").strip()
            if ema_product_number:
                evidence_item["original_drug_id"] = ema_product_number
            if approval_date:
                evidence_item["approval_date"] = approval_date

            record.update({
                "drug_disease": f"{drug_id}|{disease_id}",
                "final_normalized_drug_id": drug_id,
                "final_normalized_drug_label": drug_label,
                "final_normalized_disease_id": disease_id,
                "final_normalized_disease_label": disease_label,
                "fda": False,
                "ema": True,
                "pmda": False,
                "relationship_type": "CONTRAINDICATION",
                "indications_text": contras_text,
                "evidence": [evidence_item],
            })
            contraindication_records.append(record)

    flush_disease_grounding(service)
    logger.info(
        "EMA contraindication extraction: %d drugs processed, %d had PDFs, "
        "%d had §4.3 section, %d records produced",
        drugs_processed, drugs_with_pdf, drugs_with_section,
        len(contraindication_records),
    )
    return contraindication_records


def _write_ema_contraindications(
    records: list[dict],
    out_path: Path | None = None,
) -> Path:
    """Write contraindication records to ``kb/indications/ema/contraindications.yaml``.

    Args:
        records: List of contraindication records.
        out_path: Optional override for the output path (used for testing).

    Returns:
        The path that was written.
    """
    if out_path is None:
        out_path = KB_INDICATIONS_DIR / "contraindications.yaml"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    content = yaml.dump(records, default_flow_style=False, allow_unicode=True, width=1000)
    content = "".join(c for c in content if c == "\n" or c == "\t" or ord(c) >= 32)
    with open(out_path, "w") as f:
        f.write(content)
    logger.info("Wrote %d EMA contraindications to %s", len(records), out_path)
    return out_path


@app.command()
def main(
    grounding_backend: str = typer.Option("lexical", help="Grounding backend to use"),
    force_download: bool = typer.Option(False, "--force-download", help="Force re-download of source data"),
    skip_indications: bool = typer.Option(False, "--skip-indications", help="Skip indication extraction (drug ingest only)"),
    extract_contras: bool = typer.Option(
        False,
        "--extract-contras",
        help=(
            "Run EMA EPAR PDF contraindication extraction (§4.3). "
            "Off by default — multi-hour run across ~2,500 PDFs."
        ),
    ),
) -> None:
    """Ingest EMA data: download, parse, ground, and write output."""
    logging.basicConfig(level=logging.INFO)

    # Load URL config
    source_urls = load_source_urls()
    ema_config = source_urls.get("ema", {})
    url = ema_config.get(
        "url",
        "https://www.ema.europa.eu/en/documents/report/medicines-output-medicines-report_en.xlsx",
    )

    # Download XLSX
    dest_dir = Path("cache/downloads/ema")
    dest_path = dest_dir / "ema_medicines.xlsx"
    raw_path = download_file(url, dest_path, force=force_download)

    # Parse raw data
    records = parse_ema(raw_path)

    # Sanity: `parse_ema` returns [] when an expected column is renamed upstream —
    # a warning, not an exception — which would otherwise overwrite kb/drugs/ema
    # and kb/indications/ema with empty files and still exit 0. The floor turns
    # upstream layout drift into a loud failure. (Same control as Russia/China.)
    check_row_floor("ema", len(records))
    record_source("ema", str(raw_path), len(records))

    # Ground records
    grounding_service = get_grounding_service(grounding_backend)
    cache = GroundingCache()
    grounded_records, report = ground_records(
        records, grounding_service, cache, source_name="ema"
    )

    # Write drug outputs
    output_dir = Path("kb/drugs/ema")
    write_drug_source_yaml(grounded_records, output_dir, "ema")
    write_grounding_report(report, output_dir, "ema")

    logger.info(
        "EMA ingest complete: %d drugs, %d auto-accepted, %d unresolved",
        report["total_drugs"],
        report["auto_accepted"],
        report["unresolved"],
    )

    # Extract indications from primary EMA source
    if not skip_indications:
        indication_records = _build_ema_indication_records(grounded_records, grounding_backend)
        _write_ema_indications(indication_records)
        # Flush the shared dailymed disease cache so we don't lose work if a future run crashes
        from medic.ingest.dailymed.__main__ import _get_disease_cache
        _get_disease_cache().flush()

    # Extract contraindications from EPAR Product Information PDFs (opt-in).
    if extract_contras:
        contraindication_records = _extract_contraindications(
            grounded_records, grounding_backend
        )
        _write_ema_contraindications(contraindication_records)
        from medic.ingest.dailymed.__main__ import _get_disease_cache
        _get_disease_cache().flush()


if __name__ == "__main__":
    app()

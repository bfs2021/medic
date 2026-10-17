"""PMDA (Japan Pharmaceuticals and Medical Devices Agency) drug ingest.

Reads drug records from the consolidated English approvals PDF (the primary
source, fetched by ``medic.ingest.pmda.fetch_primary``) and grounds each
through the current grounding cascade. There is a single acquisition path:
if the primary PDF cannot be fetched or parsed, ingest fails loudly rather
than silently degrading to any legacy table.
"""

import logging
from pathlib import Path

import typer
import yaml

from medic.grounding.cache import GroundingCache
from medic.grounding.factory import get_grounding_service
from medic.ingest.common import (
    write_drug_source_yaml,
    write_grounding_report,
)
from medic.ingest.grounding import (
    flush_disease_grounding,
    ground_records,
    resolve_disease_onto_record,
)
from medic.ingest.sanity import check_row_floor, record_source

logger = logging.getLogger(__name__)

app = typer.Typer()

KB_INDICATIONS_DIR = Path("kb/indications/pmda")

# Primary source: consolidated English approvals PDF.
PRIMARY_PDF = Path("data/raw/pmda/primary/pmda_approvals.pdf")


def _records_for_approval(
    drug_rec: dict, approval: dict, drug_id: str, drug_label: str, service,
) -> tuple[list[dict], bool]:
    """Indication records for ONE approval statement. Returns (records, had_review_url)."""
    from medic.ingest.dailymed.__main__ import (
        extract_diseases_from_text,
        record_claim_context,
    )
    from medic.ingest.pmda.review_lookup import lookup_review

    ind_text = (approval.get("snippet") or "").strip()
    if not ind_text:
        return [], False
    approval_date = (approval.get("approval_date") or "").strip()
    brand_name = (approval.get("brand_name") or "").strip()
    first_brand = brand_name.split(" | ")[0].strip() if brand_name else ""
    document_id = (approval.get("document_id") or "").strip()

    # Look up per-product PMDA review URL by brand or INN
    review_entry = lookup_review(first_brand) or lookup_review(drug_label)
    product_id = ""
    if review_entry and review_entry.get("url"):
        ref_url = review_entry["url"]
        product_id = review_entry.get("product_id", "")
    else:
        search_query = first_brand or drug_label
        ref_url = "https://www.pmda.go.jp/PmdaSearch/iyakuSearch/" if search_query else ""

    try:
        diseases = extract_diseases_from_text(ind_text, source="PMDA")
    except Exception as e:
        logger.warning("Disease extraction failed for %s: %s", drug_label, e)
        diseases = []

    out: list[dict] = []
    for _claim in diseases:
        disease_name, disease_verbatim = _claim.target, _claim.target_verbatim
        record: dict = {}
        try:
            disease_id = resolve_disease_onto_record(record, disease_name, service)
        except Exception as e:
            logger.warning("Grounding failed for %s: %s", disease_name, e)
            continue
        if not disease_id:
            continue
        disease_label = record["final_normalized_disease_label"]

        # Source-side drug ID: PMDA's 12-digit YJ code if upstream supplied it. The English
        # approvals PDF does not carry it, so this is always empty today; the slot is wired so
        # DailyMed-style deep linking works once a YJ-bearing source lands.
        original_drug_id = (drug_rec.get("yj_code", "") or "").strip() or None
        evidence_item = {
            "source_type": "REGULATORY",
            "jurisdiction": "JAPAN",
            "confidence": "HIGH" if product_id else "MEDIUM",
            "approval_status": "APPROVED",
            "source_role": "PRIMARY",
            "explanation": (
                "PMDA-approved indication; per-product review report"
                if product_id else
                "PMDA-approved indication; review report not yet linked (search URL fallback)"
            ),
            # No truncation needed: one approval statement, not a pipe-joined blob of several.
            "snippet": ind_text,
            "original_drug_label": drug_rec.get("source_name", "") or drug_label,
            "original_disease_label": disease_name,
            "original_disease_verbatim": disease_verbatim,
        }
        record_claim_context(evidence_item, _claim)
        if original_drug_id:
            evidence_item["original_drug_id"] = original_drug_id
        # Distinguishes the approvals of one ingredient, so the merge treats each as its own
        # attesting document rather than collapsing them onto one.
        if document_id:
            evidence_item["document_id"] = document_id
        if ref_url:
            evidence_item["reference"] = ref_url
            if ref_url.lower().endswith(".pdf") and "pmda.go.jp" in ref_url.lower():
                evidence_item["source_document_url"] = ref_url
        if approval_date:
            evidence_item["approval_date"] = approval_date
        if product_id:
            evidence_item["product_id"] = product_id

        record.update({
            "drug_disease": f"{drug_id}|{disease_id}",
            "final_normalized_drug_id": drug_id,
            "final_normalized_drug_label": drug_label,
            "final_normalized_disease_id": disease_id,
            "final_normalized_disease_label": disease_label,
            "fda": False,
            "ema": False,
            "pmda": True,
            "relationship_type": "INDICATION",
            "indications_text": ind_text,
            "evidence": [evidence_item],
        })
        out.append(record)
    return out, bool(product_id)


def _build_pmda_indication_records(grounded_drugs: list[dict], grounding_backend: str) -> list[dict]:
    """Extract structured PMDA indications, one statement per approval row.

    The approvals PDF has a row per approval, each with its own date and its own account of what
    was approved. Those rows used to be pipe-joined into a single ``indication`` string per
    ingredient, which made PMDA's records coarser than every other source's: 1,078 of 1,976
    carried several distinct claims in one blob, every one dated to the ingredient's first-ever
    approval, and 737 truncated mid-sentence at 500 characters. Each approval is now its own
    record with its own date, snippet and document id.
    """
    service = get_grounding_service(grounding_backend)

    indication_records: list[dict] = []
    drugs_processed = 0
    drugs_with_indications = 0
    approvals_seen = 0
    with_review_url = 0

    for drug_rec in grounded_drugs:
        drug_id = drug_rec.get("normalized_id", "")
        drug_label = drug_rec.get("normalized_label", "") or drug_rec.get("source_name", "")
        if not drug_id or drug_rec.get("grounding_status") == "unresolved":
            continue

        approvals = drug_rec.get("approvals") or []
        if not approvals:
            # Pre-`approvals` drug record (older cached data): degrade to the joined text
            # rather than silently emitting nothing for the drug.
            blob = (drug_rec.get("indication", "") or "").strip()
            if not blob:
                continue
            approvals = [{
                "snippet": blob,
                "approval_date": drug_rec.get("approval_date", "") or "",
                "brand_name": drug_rec.get("brand_name", "") or "",
                "document_id": "",
            }]

        drugs_processed += 1
        produced_any = False
        for approval in approvals:
            approvals_seen += 1
            records, had_url = _records_for_approval(
                drug_rec, approval, drug_id, drug_label, service)
            if had_url:
                with_review_url += 1
            if records:
                produced_any = True
                indication_records.extend(records)
        if produced_any:
            drugs_with_indications += 1

    flush_disease_grounding(service)
    logger.info(
        "PMDA indication extraction: %d drugs processed, %d approval statements, %d drugs had "
        "indications, %d statements with a per-product review URL, %d records produced",
        drugs_processed, approvals_seen, drugs_with_indications, with_review_url,
        len(indication_records),
    )
    return indication_records


def _write_pmda_indications(records: list[dict]) -> None:
    """Write PMDA indication records to kb/indications/pmda/indications.yaml."""
    KB_INDICATIONS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = KB_INDICATIONS_DIR / "indications.yaml"
    content = yaml.dump(records, default_flow_style=False, allow_unicode=True, width=1000)
    content = "".join(c for c in content if c == "\n" or c == "\t" or ord(c) >= 32)
    with open(out_path, "w") as f:
        f.write(content)
    logger.info("Wrote %d PMDA indications to %s", len(records), out_path)


def _write_pmda_contraindications(records: list[dict]) -> None:
    """Write PMDA contraindications to kb/indications/pmda/contraindications.yaml."""
    KB_INDICATIONS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = KB_INDICATIONS_DIR / "contraindications.yaml"
    content = yaml.dump(records, default_flow_style=False, allow_unicode=True, width=1000)
    content = "".join(c for c in content if c == "\n" or c == "\t" or ord(c) >= 32)
    with open(out_path, "w") as f:
        f.write(content)
    logger.info("Wrote %d PMDA contraindications to %s", len(records), out_path)


def _build_pmda_contraindication_records(
    grounded_drugs: list[dict],
    grounding_backend: str,
    limit: int | None = None,
) -> list[dict]:
    """Extract structured PMDA contraindications from per-product review-report PDFs.

    Mirrors :func:`_build_pmda_indication_records` but pulls free text from the
    Contraindications section of each drug's per-product review-report PDF
    (downloaded via :func:`fetch_review_report`). Drugs whose review URL is the
    PMDA brand-search-page fallback are skipped — only per-product PDFs are
    processed.

    Args:
        grounded_drugs: Output of :func:`ground_records` for PMDA.
        grounding_backend: Backend name for the grounding cascade.
        limit: If set, stop after this many PDFs have been *downloaded* (i.e.
            drugs that resolved to a per-product PDF). Useful for testing.
    """
    from medic.ingest.dailymed.__main__ import (
        extract_contraindicated_diseases_from_text,
    )
    from medic.ingest.pmda.parse_pdf import extract_contraindications_from_pdf
    from medic.ingest.pmda.review_lookup import (
        fetch_review_report,
        lookup_review,
    )

    service = get_grounding_service(grounding_backend)

    records: list[dict] = []
    drugs_eligible = 0
    drugs_with_pdf = 0
    drugs_with_section = 0
    drugs_processed_for_extraction = 0

    for drug_rec in grounded_drugs:
        if limit is not None and drugs_processed_for_extraction >= limit:
            break
        drug_id = drug_rec.get("normalized_id", "")
        drug_label = drug_rec.get("normalized_label", "") or drug_rec.get("source_name", "")
        if not drug_id or drug_rec.get("grounding_status") == "unresolved":
            continue
        drugs_eligible += 1

        brand_name = drug_rec.get("brand_name", "") or ""
        first_brand = brand_name.split(" | ")[0].strip() if brand_name else ""

        # Look up per-product PMDA review URL by brand or INN.
        review_entry = lookup_review(first_brand) or lookup_review(drug_label)
        if not review_entry or not review_entry.get("url"):
            continue
        ref_url = review_entry["url"]
        product_id = review_entry.get("product_id", "")

        # Defensive: skip search-page fallbacks (fetch_review_report does this
        # too, but we avoid the disk-cache check entirely for these).
        if "PmdaSearch/iyakuSearch/" in ref_url:
            continue

        pdf_path = fetch_review_report(ref_url, first_brand or drug_label)
        if pdf_path is None:
            continue
        drugs_with_pdf += 1
        drugs_processed_for_extraction += 1

        sections = extract_contraindications_from_pdf(pdf_path)
        if not sections:
            continue
        section = sections[0]
        section_text = (section.get("text") or "").strip()
        if not section_text:
            continue
        drugs_with_section += 1

        try:
            diseases = extract_contraindicated_diseases_from_text(
                section_text, source="PMDA")
        except Exception as e:
            logger.warning("Disease extraction failed for %s: %s", drug_label, e)
            diseases = []

        approval_date = drug_rec.get("approval_date", "") or ""
        original_drug_id = (drug_rec.get("yj_code", "") or "").strip() or None

        for _claim in diseases:
            disease_name, disease_verbatim = _claim.target, _claim.target_verbatim
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
                "jurisdiction": "JAPAN",
                # HIGH only when product_id (review-report ID) is present —
                # mirrors the indication-record confidence logic.
                "confidence": "HIGH" if product_id else "MEDIUM",
                "approval_status": "APPROVED",
                "source_role": "PRIMARY",
                "explanation": (
                    "PMDA contraindication from per-product review report"
                    if product_id else
                    "PMDA contraindication; review report not yet linked (search URL fallback)"
                ),
                "snippet": section_text[:500],
                "language": section.get("language", ""),
                "original_drug_label": drug_rec.get("source_name", "") or drug_label,
                "original_disease_label": disease_name,
                "original_disease_verbatim": disease_verbatim,
                "reference": ref_url,
            }
            if ref_url.lower().endswith(".pdf") and "pmda.go.jp" in ref_url.lower():
                evidence_item["source_document_url"] = ref_url
            if approval_date:
                evidence_item["approval_date"] = approval_date
            if product_id:
                evidence_item["product_id"] = product_id
            if original_drug_id:
                evidence_item["original_drug_id"] = original_drug_id

            record.update({
                "drug_disease": f"{drug_id}|{disease_id}",
                "final_normalized_drug_id": drug_id,
                "final_normalized_drug_label": drug_label,
                "final_normalized_disease_id": disease_id,
                "final_normalized_disease_label": disease_label,
                "fda": False,
                "ema": False,
                "pmda": True,
                "relationship_type": "CONTRAINDICATION",
                "indications_text": section_text,
                "evidence": [evidence_item],
            })
            records.append(record)

    flush_disease_grounding(service)
    logger.info(
        "PMDA contra extraction: %d drugs eligible, %d PDFs downloaded, "
        "%d with contra section, %d records produced",
        drugs_eligible, drugs_with_pdf, drugs_with_section, len(records),
    )
    return records


@app.command()
def main(
    grounding_backend: str = typer.Option("lexical", help="Grounding backend to use"),
    force_download: bool = typer.Option(False, "--force-download", help="Force re-download of PMDA primary PDF"),
    skip_indications: bool = typer.Option(False, "--skip-indications", help="Skip indication extraction"),
    extract_contras: bool = typer.Option(False, "--extract-contras", help="Extract contraindications from per-product review-report PDFs (downloads PDFs)"),
    contras_limit: int | None = typer.Option(None, "--contras-limit", help="Limit contraindication extraction to first N PDFs (for testing)"),
) -> None:
    """Ingest PMDA drug data from the consolidated English approvals PDF.

    The PDF is the single acquisition path. If it cannot be fetched or parsed,
    ingest fails loudly — it does not degrade to any legacy table.
    """
    logging.basicConfig(level=logging.INFO)

    from medic.ingest.pmda.fetch_primary import fetch_primary_pdf
    from medic.ingest.pmda.parse_pdf import (
        deduplicate_by_ingredient,
        parse_pmda_pdf,
    )

    try:
        pdf_path = fetch_primary_pdf(force=force_download)
        raw_records = parse_pmda_pdf(pdf_path)
        records = deduplicate_by_ingredient(raw_records)
    except Exception as e:
        raise RuntimeError(
            "PMDA primary source unavailable — could not fetch/parse the "
            f"consolidated approvals PDF (expected at {PRIMARY_PDF}). "
            "Fix the PMDA source URL in conf/source_urls.yaml or place the "
            f"PDF at {PRIMARY_PDF}, then re-run. "
            f"Underlying error: {e!r}"
        ) from e

    # Sanity: `parse_pmda_pdf` skips any page whose header map does not resolve and
    # returns [] on a layout change, which would overwrite kb/drugs/pmda and
    # kb/indications/pmda with empty files and still exit 0. The floor turns that
    # into a loud failure. (Same control as Russia/China.)
    check_row_floor("pmda", len(records))
    record_source("pmda", str(pdf_path), len(records))

    logger.info(
        "Using PMDA primary source: %d unique ingredients from %d raw records",
        len(records), len(raw_records),
    )

    # Ground through the cascade — same as all other sources
    grounding_service = get_grounding_service(grounding_backend)
    cache = GroundingCache()
    grounded_records, report = ground_records(
        records, grounding_service, cache, source_name="pmda"
    )

    output_dir = Path("kb/drugs/pmda")
    write_drug_source_yaml(grounded_records, output_dir, "pmda")
    write_grounding_report(report, output_dir, "pmda")

    logger.info(
        "PMDA ingest complete: %d drugs, %d auto-accepted, %d unresolved",
        report["total_drugs"],
        report["auto_accepted"],
        report["unresolved"],
    )

    if not skip_indications:
        indication_records = _build_pmda_indication_records(grounded_records, grounding_backend)
        _write_pmda_indications(indication_records)
        from medic.ingest.dailymed.__main__ import _get_disease_cache
        _get_disease_cache().flush()

    if extract_contras:
        contra_records = _build_pmda_contraindication_records(
            grounded_records, grounding_backend, limit=contras_limit,
        )
        _write_pmda_contraindications(contra_records)
        from medic.ingest.dailymed.__main__ import _get_disease_cache
        _get_disease_cache().flush()


if __name__ == "__main__":
    app()

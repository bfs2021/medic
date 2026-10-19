"""India drug ingest — Central Drugs Standard Control Organisation (CDSCO).

Reads fresh year-by-year PDFs from the CDSCO listing page. This is the single
acquisition path: if the PDFs cannot be fetched or parsed, ingest fails loudly
rather than silently degrading to any legacy table.

NOTE: Contraindication extraction is NOT feasible for CDSCO. CDSCO publishes
only year-batch tabulation PDFs ("List of New Drugs Approved in YYYY"), which
contain a single "Indication" column and no contraindications field. There is
no per-drug landing page, no per-product label or SmPC, and no SPL-equivalent
artifact hosted by CDSCO. Indian package inserts ("PIL") exist commercially
but are not made available by CDSCO under a free, machine-accessible feed.
Until a per-product authoritative document feed appears, CDSCO contributes
INDIA-jurisdiction indications only — never contraindications.
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
from medic.ingest.grounding import ground_records

logger = logging.getLogger(__name__)

app = typer.Typer()

PRIMARY_DIR = Path("data/raw/india/primary")
KB_INDICATIONS_DIR = Path("kb/indications/india")


def _build_india_indication_records(grounded_drugs: list[dict], grounding_backend: str) -> list[dict]:
    """Extract structured India indications from grounded drug records."""
    from medic.ingest.dailymed.__main__ import (
        extract_diseases_from_text,
        record_claim_context,
    )

    service = get_grounding_service(grounding_backend)
    indication_records: list[dict] = []
    drugs_processed = 0
    drugs_with_indications = 0

    for drug_rec in grounded_drugs:
        drug_id = drug_rec.get("normalized_id", "")
        drug_label = drug_rec.get("normalized_label", "") or drug_rec.get("source_name", "")
        if not drug_id or drug_rec.get("grounding_status") == "unresolved":
            continue
        ind_text = (drug_rec.get("indication", "") or "").strip()
        if not ind_text or ind_text.lower().startswith("not applicable"):
            continue
        drugs_processed += 1
        approval_date = drug_rec.get("approval_date", "") or ""
        ref_url = "https://cdsco.gov.in/opencms/opencms/en/Approval_new/Approved-New-Drugs/"

        try:
            diseases = extract_diseases_from_text(ind_text, source="INDIA")
        except Exception as e:
            logger.warning("Disease extraction failed for %s: %s", drug_label, e)
            diseases = []
        if diseases:
            drugs_with_indications += 1
        for _claim in diseases:
            disease_name, disease_verbatim = _claim.target, _claim.target_verbatim
            try:
                result = service.ground_disease_best(disease_name)
            except Exception as e:
                logger.warning("Grounding failed for %s: %s", disease_name, e)
                continue
            if not result or not result.id:
                continue
            disease_id, disease_label = result.id, result.label
            # TODO original_drug_id: CDSCO does not publish a stable per-drug
            # identifier in its annual approval PDFs. The base64-encoded
            # `num_id` URL parameter (e.g. ``?num_id=MTM1NTA=``) on the listing
            # page resolves to a YEAR-BATCH PDF, not a per-drug record — the
            # individual drug rows inside each PDF carry only an in-document
            # serial number (``S.No.``) that is not globally unique. There is
            # no per-product CDSCO landing page or PIL feed today; until one
            # appears, India evidence cannot carry a meaningful
            # ``original_drug_id``. Leaving the slot empty rather than
            # synthesising a non-resolvable identifier.
            evidence_item = {
                "source_type": "REGULATORY",
                "jurisdiction": "INDIA",
                "confidence": "HIGH",
                "approval_status": "APPROVED",
                "source_role": "PRIMARY",
                "explanation": "CDSCO-approved indication from India primary source PDF",
                "snippet": ind_text[:500],
                "reference": ref_url,
                "original_drug_label": drug_rec.get("source_name", "") or drug_label,
                "original_disease_label": disease_name,
                "original_disease_verbatim": disease_verbatim,
            }
            record_claim_context(evidence_item, _claim, grounding_backend)
            if approval_date:
                evidence_item["approval_date"] = approval_date
            indication_records.append({
                "drug_disease": f"{drug_id}|{disease_id}",
                "final_normalized_drug_id": drug_id,
                "final_normalized_drug_label": drug_label,
                "final_normalized_disease_id": disease_id,
                "final_normalized_disease_label": disease_label,
                "fda": False, "ema": False, "pmda": False,
                "relationship_type": "INDICATION",
                "indications_text": ind_text,
                "evidence": [evidence_item],
            })
    logger.info(
        "India indication extraction: %d drugs processed, %d had indications, %d records produced",
        drugs_processed, drugs_with_indications, len(indication_records),
    )
    return indication_records


def _write_india_indications(records: list[dict]) -> None:
    KB_INDICATIONS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = KB_INDICATIONS_DIR / "indications.yaml"
    content = yaml.dump(records, default_flow_style=False, allow_unicode=True, width=1000)
    content = "".join(c for c in content if c == "\n" or c == "\t" or ord(c) >= 32)
    with open(out_path, "w") as f:
        f.write(content)
    logger.info("Wrote %d India indications to %s", len(records), out_path)


@app.command()
def main(
    grounding_backend: str = typer.Option("lexical", help="Grounding backend to use"),
    force_download: bool = typer.Option(False, "--force-download", help="Force re-download of CDSCO PDFs"),
    skip_indications: bool = typer.Option(False, "--skip-indications", help="Skip indication extraction"),
) -> None:
    """Ingest India drug data from the CDSCO annual approval PDFs.

    The CDSCO PDFs are the single acquisition path. If they cannot be fetched
    or parsed, ingest fails loudly — it does not degrade to any legacy table.
    """
    logging.basicConfig(level=logging.INFO)

    from medic.ingest.india.fetch_primary import fetch_all_pdfs
    from medic.ingest.india.parse_pdf import (
        deduplicate_by_drug_name,
        parse_all_pdfs,
    )

    try:
        if force_download or not any(PRIMARY_DIR.glob("*.pdf")):
            fetch_all_pdfs(force=force_download)
        raw_records = parse_all_pdfs(PRIMARY_DIR)
        records = deduplicate_by_drug_name(raw_records)
    except Exception as e:
        raise RuntimeError(
            "India (CDSCO) primary source unavailable — could not fetch/parse "
            f"the annual approval PDFs (expected under {PRIMARY_DIR}). "
            "Run with --force-download to re-fetch, fix the CDSCO source URLs "
            "in conf/source_urls.yaml, or place the PDFs manually, then re-run. "
            f"Underlying error: {e!r}"
        ) from e

    if not records:
        raise RuntimeError(
            f"India (CDSCO) PDFs parsed to zero drug records (under {PRIMARY_DIR}). "
            "The PDFs may be empty, malformed, or in an unexpected layout. "
            "Re-acquire with --force-download and verify the source URLs in "
            "conf/source_urls.yaml."
        )

    logger.info(
        "Using India primary source: %d unique drugs from %d raw records",
        len(records), len(raw_records),
    )

    grounding_service = get_grounding_service(grounding_backend)
    cache = GroundingCache()
    grounded_records, report = ground_records(
        records, grounding_service, cache, source_name="india"
    )

    output_dir = Path("kb/drugs/india")
    write_drug_source_yaml(grounded_records, output_dir, "india")
    write_grounding_report(report, output_dir, "india")

    logger.info(
        "India ingest complete: %d drugs, %d auto-accepted, %d unresolved",
        report["total_drugs"], report["auto_accepted"], report["unresolved"],
    )

    if not skip_indications:
        indication_records = _build_india_indication_records(grounded_records, grounding_backend)
        _write_india_indications(indication_records)
        from medic.ingest.dailymed.__main__ import _get_disease_cache
        _get_disease_cache().flush()


if __name__ == "__main__":
    app()

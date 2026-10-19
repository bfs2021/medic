"""Drug merge: combines drug source records into a unified drug list.

Reads all kb/drugs/<source>/*.yaml files, groups by normalized_id,
merges metadata across sources, and writes products/drug_list.yaml.

Merge logic:
1. Load all source YAML files from kb/drugs/<source>/
2. Filter out records with grounding_status == "unresolved"
3. Group remaining records by normalized_id (canonical CURIE)
4. For each group, merge: source_ingredients, approval flags, marketing_status_usa,
   alternate_ids, and approval_date (earliest)
5. Write output as DrugList wrapper: {"drugs": drug_list}
"""

import logging
from pathlib import Path

import yaml

from medic import product_view as pv
from medic.grounding.lexical.preprocess import base_normalize
from medic.grounding.store import LiteralMappingStore
from medic.provenance_build import build_mention, validate_mention_chain

logger = logging.getLogger(__name__)

# Grounding decision store: the git-tracked audit of every Stage-1 grounding, including
# the preprocessing rules (subject_preprocessing) that fired. We funnel those rules onto
# the product Mention's GroundingStep (I-8). Reading a git-tracked TSV keeps the merge
# offline/deterministic (I-2/I-4).
_DRUG_GROUNDING_STORE = "mappings/drug_grounding.sssom.tsv"


def _load_applied_rules(
    store_path: str = _DRUG_GROUNDING_STORE,
) -> tuple[dict[tuple[str, str], list[str]], dict[tuple[str, str], list[str]]]:
    """Build the (subject_id, object_id) -> applied-rules lookup from the grounding store.

    Returns two dicts: the primary keyed on the mention id (SSSOM ``subject_id``, present
    on ~99% of rows) and a fallback keyed on the base-normalized subject label, both paired
    with the grounded ``object_id``. A grounding with no rule row resolves to ``[]``.
    """
    by_id: dict[tuple[str, str], list[str]] = {}
    by_label: dict[tuple[str, str], list[str]] = {}
    store = LiteralMappingStore(store_path, "drug")
    store.load()
    for d in store.all_rows():
        if not d.object_id or not d.subject_preprocessing:
            continue
        rules = list(d.subject_preprocessing)
        if d.subject_id:
            by_id[(d.subject_id, d.object_id)] = rules
        by_label[(base_normalize(d.subject_label), d.object_id)] = rules
    return by_id, by_label


def _applied_rules_for(grounding: dict | None, rules_lookup) -> list[str]:
    """Resolve the applied preprocessing rules for a grounding via the store lookup."""
    if not grounding or not rules_lookup:
        return []
    object_id = grounding.get("grounded_id")
    if not object_id:
        return []
    by_id, by_label = rules_lookup
    subject_id = grounding.get("subject_id")
    if subject_id and (subject_id, object_id) in by_id:
        return by_id[(subject_id, object_id)]
    original = grounding.get("original_string") or ""
    return by_label.get((base_normalize(original), object_id), [])

# Source -> (regulatory authority, concrete data-source name) for the approvals[] model.
# All drug sources are the authority themselves (PRIMARY); DailyMed (INTERMEDIARY) is an
# indication source, not a drug source.
_SOURCE_AUTHORITY = {
    "ORANGEBOOK": ("FDA", "ORANGEBOOK"),
    "PURPLEBOOK": ("FDA", "PURPLEBOOK"),
    "EMA": ("EMA", "EMA_EPAR"),
    "PMDA": ("PMDA", "PMDA"),
    "INDIA": ("CDSCO", "CDSCO"),
    "RUSSIA": ("MOH_RUSSIA", "GRLS"),
    "CHINA": ("NMPA_CHINA", "CDE_CHINA"),
}

# Source → approval flag mapping
SOURCE_APPROVAL_MAP = {
    "ORANGEBOOK": "approved_usa",
    "PURPLEBOOK": "approved_usa",
    "EMA": "approved_europe",
    "PMDA": "approved_japan",
    "INDIA": "approved_india",
    "RUSSIA": "approved_russia",
    "CHINA": "approved_china",
    "EVERYCURE": None,  # no specific jurisdiction
}

# Marketing status permissiveness: higher = more permissive
_STATUS_PRIORITY = {
    "NONE": 0,
    "DISCN": 1,
    "RX": 2,
    "OTC": 3,
}

# Map common variations to schema enum values
_STATUS_NORMALIZE = {
    "DISCONTINUED": "DISCN",
    "DISCN": "DISCN",
    "RX": "RX",
    "OTC": "OTC",
    "NONE": "NONE",
}


def _load_all_sources(kb_dir: Path) -> list[dict]:
    """Load all drug source YAML files from kb_dir subdirectories."""
    all_records = []
    if not kb_dir.exists():
        logger.warning("KB drugs directory does not exist: %s", kb_dir)
        return all_records

    for source_dir in sorted(kb_dir.iterdir()):
        if not source_dir.is_dir():
            continue
        for yaml_file in sorted(source_dir.glob("*.yaml")):
            try:
                with open(yaml_file) as f:
                    records = yaml.safe_load(f)
                if not records:
                    continue
                if isinstance(records, dict):
                    records = [records]
                all_records.extend(records)
            except Exception:
                logger.warning("Failed to read %s", yaml_file)

    return all_records


# Source → jurisdiction mapping for evidence items
_SOURCE_JURISDICTION = {
    "ORANGEBOOK": "USA",
    "PURPLEBOOK": "USA",
    "EMA": "EU",
    "PMDA": "JAPAN",
    "INDIA": "INDIA",
    "RUSSIA": "RUSSIA",
    "CHINA": "CHINA",
}

_SOURCE_DESCRIPTION = {
    "ORANGEBOOK": "FDA Orange Book approved drug product",
    "PURPLEBOOK": "FDA Purple Book approved biologic",
    "EMA": "European Medicines Agency authorized medicinal product",
    "PMDA": "Japan PMDA approved pharmaceutical",
    "INDIA": "India CDSCO registered drug",
    "RUSSIA": "Russian Ministry of Health registered drug",
    "CHINA": "China CDE/NMPA approved drug",
}


def _merge_group(drug_id: str, records: list[dict], rules_lookup=None) -> dict:
    """Merge multiple source records for the same drug into one.

    ``rules_lookup`` is the ``(by_id, by_label)`` pair from :func:`_load_applied_rules`;
    when supplied, the Stage-1 preprocessing rules for the representative grounding are
    funneled onto the Mention's GroundingStep.
    """
    all_ingredients = []
    all_alt_ids = set()
    all_source_xrefs = set()
    marketing_statuses = []
    evidence_items = []
    label = ""
    earliest_date = None
    # Representative grounding/normalization for the merged drug. A merged drug
    # aggregates several source records, each with its own Stage-1 grounding of a
    # different source string that all resolved to this same canonical curie. We
    # carry ONE representative object (not a list): the grounding with the highest
    # confidence, preferring one whose grounded/normalized id already equals the
    # canonical curie. This keeps the product faithful (the decision that produced
    # this id) without exploding into a per-source list.
    best_grounding = None
    best_normalization = None
    best_translation = None
    best_grounding_conf = -1.0
    best_record = None
    # approvals[] (RegulatoryStatus per authoritative artifact), deduped by (authority, source)
    approvals_by_key: dict[tuple, dict] = {}

    approved = {
        "approved_usa": False,
        "approved_europe": False,
        "approved_japan": False,
        "approved_india": False,
        "approved_russia": False,
        "approved_china": False,
    }

    for rec in records:
        source = rec.get("source", "")
        ingredient = rec.get("source_name", "")
        if ingredient and ingredient not in all_ingredients:
            all_ingredients.append(ingredient)

        # Set approval flags
        approval_field = SOURCE_APPROVAL_MAP.get(source)
        if approval_field:
            approved[approval_field] = True

        # Build evidence item for this source
        jurisdiction = _SOURCE_JURISDICTION.get(source, "")
        if jurisdiction:
            import urllib.parse
            drug_name = rec.get("source_name", "") or rec.get("normalized_label", "")
            drug_encoded = urllib.parse.quote_plus(drug_name) if drug_name else ""

            ev = {
                "source_type": "REGULATORY",
                "jurisdiction": jurisdiction,
                "confidence": "HIGH",
                "approval_status": "APPROVED",
                "explanation": _SOURCE_DESCRIPTION.get(source, f"Approved by {source}"),
            }
            # Build a verifiable URL for the source
            if source in ("ORANGEBOOK", "PURPLEBOOK") and drug_encoded:
                appl_no = (rec.get("application_number", "") or "").split("|")[0].strip()
                if appl_no:
                    ev["reference"] = (
                        f"https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm"
                        f"?event=overview.process&ApplNo={appl_no}"
                    )
                else:
                    ev["reference"] = f"https://dailymed.nlm.nih.gov/dailymed/search.cfm?labeltype=all&query={drug_encoded}"
            elif source == "EMA":
                epar_url = (rec.get("epar_url", "") or "").strip()
                if epar_url:
                    ev["reference"] = epar_url
                elif drug_encoded:
                    ev["reference"] = f"https://www.ema.europa.eu/en/medicines?search_api_fulltext={drug_encoded}"
            elif source == "PMDA" and drug_encoded:
                ev["reference"] = "https://www.pmda.go.jp/PmdaSearch/iyakuSearch/"
            elif source == "INDIA" and drug_encoded:
                ev["reference"] = "https://cdscoonline.gov.in/CDSCO/Drugs"
            elif source == "RUSSIA" and drug_encoded:
                ev["reference"] = "https://grls.rosminzdrav.ru/Default.aspx"
            elif source == "CHINA" and drug_encoded:
                ev["reference"] = "https://www.cde.org.cn/main/xxgk/listpage/2f78f372c1867de05a2cd5c26a793612"
            # Include approval date
            date_val = rec.get("approval_date", "")
            if date_val and str(date_val).strip():
                ev["explanation"] += f" (approved {date_val})"
            evidence_items.append(ev)

            # Build the structured RegulatoryStatus approval (approvals[] model),
            # one per (authority, source), mirroring this source's evidence row.
            auth = _SOURCE_AUTHORITY.get(source)
            if auth:
                authority, source_name = auth
                approval = approvals_by_key.get((authority, source_name))
                if approval is None:
                    approval = {
                        "authority": authority,
                        "source": source_name,
                        "status": "APPROVED",
                        "source_role": "PRIMARY",
                    }
                    if ev.get("reference"):
                        approval["regulatory_document_url"] = ev["reference"]
                    appl_no = (rec.get("application_number", "") or "").strip()
                    if appl_no:
                        approval["application_number"] = appl_no
                    approvals_by_key[(authority, source_name)] = approval
                # earliest approval date per (authority, source)
                if date_val and str(date_val).strip():
                    ds = str(date_val).strip()
                    if not approval.get("approval_date") or ds < approval["approval_date"]:
                        approval["approval_date"] = ds

        # Collect marketing status
        ms = rec.get("marketing_status_usa", "")
        if ms:
            normalized = _STATUS_NORMALIZE.get(ms.strip().upper(), "")
            if normalized:
                marketing_statuses.append(normalized)

        # Collect alternate IDs (stored as list in new format).
        #
        # A record's own combination components are excluded. `alternate_ids` is exported as
        # `skos:exactMatch`, and the components of "ACETAMINOPHEN; BUTALBITAL; CAFFEINE" are
        # three different molecules, not three names for one. The ingester no longer writes
        # them here, but every kb/drugs record produced before that fix still carries them, and
        # re-ingesting eight sources to drop a derived field is a much larger blast radius than
        # filtering on read — the components are right there on the same record.
        rec_components = set()
        rec_grounding = rec.get("grounding")
        if isinstance(rec_grounding, dict):
            rec_components = {str(c) for c in (rec_grounding.get("components") or []) if c}
        # Same reasoning, second source: EveryCure's own pre-grounded id. The ingester calls it
        # "untrusted" and grounds the *name* instead, then folded the id into `alternate_ids`
        # anyway — so the export asserted `ofatumumab skos:exactMatch dimethyl ether`. Filtered
        # on read for the same reason as the components: kb/drugs/everycure was written with
        # these already folded in, and re-ingesting to drop a derived field is a much larger
        # blast radius than excluding it here, where the record still carries `everycure_id`.
        everycure_id = str(rec.get("everycure_id") or "")
        if everycure_id:
            rec_components.add(everycure_id)
        alt = rec.get("alternate_ids", [])
        if isinstance(alt, list):
            all_alt_ids.update(str(x) for x in alt if x and str(x) not in rec_components)
        elif isinstance(alt, str) and alt and alt not in rec_components:
            all_alt_ids.add(alt)
        # Source-copied cross-references (see Drug.source_xrefs). Unioned like alternate_ids;
        # no component filter is needed because nothing writes components here.
        xrefs = rec.get("source_xrefs", [])
        if isinstance(xrefs, list):
            all_source_xrefs.update(str(x) for x in xrefs if x)
        elif isinstance(xrefs, str) and xrefs:
            all_source_xrefs.add(xrefs)

        # Use first non-empty label
        if not label:
            label = rec.get("normalized_label", "")

        # Pick the representative grounding/normalization object. Prefer the
        # contributing record whose resolution landed on this drug's canonical id,
        # breaking ties by grounding confidence. Records may lack the structured
        # objects (older ingest runs / pre-grounded sources) — those are skipped.
        grounding_obj = rec.get("grounding")
        if isinstance(grounding_obj, dict):
            conf = grounding_obj.get("confidence")
            try:
                conf = float(conf) if conf is not None else 0.0
            except (TypeError, ValueError):
                conf = 0.0
            matches_canonical = grounding_obj.get("grounded_id") == drug_id
            # Rank: on-target groundings always beat off-target ones; then by conf.
            rank = conf + (1000.0 if matches_canonical else 0.0)
            if rank > best_grounding_conf:
                best_grounding_conf = rank
                best_grounding = grounding_obj
                best_record = rec
                norm_obj = rec.get("normalization")
                best_normalization = norm_obj if isinstance(norm_obj, dict) else None
                # Carry the Stage-0 translation from the same representative
                # record (non-English sources only; None for English sources).
                trans_obj = rec.get("translation")
                best_translation = trans_obj if isinstance(trans_obj, dict) else None

        # Track earliest approval date
        date_val = rec.get("approval_date", "")
        if date_val and str(date_val).strip():
            date_str = str(date_val).strip()
            if earliest_date is None or date_str < earliest_date:
                earliest_date = date_str

    # Determine most permissive marketing status
    if marketing_statuses:
        best_status = max(marketing_statuses, key=lambda s: _STATUS_PRIORITY.get(s, 0))
    else:
        best_status = "NONE"

    # v2.0 shape: identity lives on `mention`, approvals on `approvals[]`. The flat
    # curie/curie_label/approved_*/marketing_status_usa/approval_date and the flat
    # translation/grounding/normalization objects are gone — `mention.steps` carries the
    # full transformation trail. `approved`/`best_status`/`earliest_date` are still
    # computed above solely to populate approvals[] and the marketing status on them.
    _ = (approved, earliest_date)  # consumed into mention/approvals below, not emitted flat
    drug = {
        "source_ingredients": all_ingredients if all_ingredients else [],
        "alternate_ids": sorted(all_alt_ids) if all_alt_ids else [],
    }
    if all_source_xrefs:
        drug["source_xrefs"] = sorted(all_source_xrefs)

    if evidence_items:
        drug["evidence"] = evidence_items

    # --- Transformation-provenance model (added additively; see spec 2026-07-28) ---
    # The identity Mention: verbatim literal + ordered steps (grounding, + translation
    # for zh/ru) + resolved CHEBI. Built from the representative source record.
    rep = best_record or (records[0] if records else {})
    literal = (rep.get("original_literal") or rep.get("source_name")
               or (best_translation or {}).get("source_value") or label or drug_id)
    src_lang = (best_translation or {}).get("source_language")
    # Every drug string is read verbatim from a structured source column, so the mention
    # opens with a STRUCTURED_FIELD ExtractionStep (spec Example 3) before any translation
    # or grounding — parity with the disease side and a full replayable trail (I-8).
    drug["identity"] = build_mention(
        original_literal=literal,
        entity_type="drug",
        mention_id=rep.get("mention_id"),
        source=rep.get("source"),
        source_language=src_lang,
        # the per-source parser that read the cell (e.g. medic-ingest-china), so the step
        # names the code that produced the literal, not just "a structured field"
        extraction={
            "method": "STRUCTURED_FIELD",
            "tool": f"medic-ingest-{(rep.get('source') or 'unknown').lower()}",
        },
        translation=best_translation,
        grounding=best_grounding,
        normalization=best_normalization,
        applied_rules=_applied_rules_for(best_grounding, rules_lookup),
        resolved_id=drug_id,
        resolved_label=label or None,
    )

    # approvals[]: stamp the most-permissive USA marketing status onto the FDA approvals.
    if approvals_by_key:
        for (authority, source_name), approval in approvals_by_key.items():
            if source_name in ("ORANGEBOOK", "PURPLEBOOK") and best_status != "NONE":
                approval["marketing_status"] = best_status
        drug["approvals"] = list(approvals_by_key.values())

    return drug


# is_<flag> boolean -> DrugFeatureEnum value (drug.yaml).
_FEATURE_MAP = {
    "is_steroid": "STEROID", "is_antimicrobial": "ANTIMICROBIAL",
    "is_chemotherapy": "CHEMOTHERAPY", "is_glucose_regulator": "GLUCOSE_REGULATOR",
    "is_vaccine_or_antigen": "VACCINE_OR_ANTIGEN",
    "is_no_therapeutic_value": "NO_THERAPEUTIC_VALUE", "is_metallic_salt": "METALLIC_SALT",
    "is_allergen": "ALLERGEN",
    "is_radioisotope_or_diagnostic_agent": "RADIOISOTOPE_OR_DIAGNOSTIC_AGENT",
    "is_cancer_drug": "CANCER_DRUG", "is_antipsychotic": "ANTIPSYCHOTIC",
    "is_sedative": "SEDATIVE", "is_analgesic": "ANALGESIC",
    "is_cardiovascular": "CARDIOVASCULAR", "is_cell_therapy": "CELL_THERAPY",
    "is_combination_therapy": "COMBINATION_THERAPY",
}
_ATC_LEVEL_KEYS = ("main", "level1", "level2", "level3", "level4", "level5")


def _nest_atc_and_features(drug: dict) -> None:
    """Build the nested `atc` object + the `features[]` list from the flat enrichment
    fields (additive: the flat atc_*/is_* stay until the rebuild removes them)."""
    atc: dict = {}
    if drug.get("atc_codes"):
        atc["codes"] = list(drug["atc_codes"])
    for lvl in _ATC_LEVEL_KEYS:
        val = drug.get("atc_main" if lvl == "main" else f"atc_{lvl}")
        if val:
            atc[lvl] = val
    if atc:
        drug["atc"] = atc
    drug["features"] = [enum for flag, enum in _FEATURE_MAP.items() if drug.get(flag)]


def merge_drugs(
    kb_dir: Path = Path("kb/drugs"),
    output_path: Path = Path("products/drug_list.yaml"),
) -> list[dict]:
    """Merge drug source records from all sources into a unified list.

    Args:
        kb_dir: Directory containing source subdirectories with YAML files.
        output_path: Path to write the merged drug list.

    Returns:
        List of merged drug records (for testability).
    """
    # Phase 1: Load all source records
    all_records = _load_all_sources(kb_dir)
    logger.info("Loaded %d total source records", len(all_records))

    # Phase 2: Filter out unresolved records
    accepted_records = [
        r for r in all_records
        if r.get("grounding_status", "accepted") != "unresolved"
    ]
    logger.info(
        "Filtered to %d accepted records (removed %d unresolved)",
        len(accepted_records),
        len(all_records) - len(accepted_records),
    )

    # Phase 3: Group by normalized_id
    groups: dict[str, list[dict]] = {}
    for record in accepted_records:
        drug_id = record.get("normalized_id", "")
        if not drug_id or drug_id in ("", "nan", "Error"):
            continue
        groups.setdefault(drug_id, []).append(record)

    logger.info("Found %d unique drugs across all sources", len(groups))

    # Phase 4: Merge each group + stamp the computed reliability tier
    from medic.reliability import StatementType, score_reliability, StatementReviewStore
    review = StatementReviewStore().load()
    rules_lookup = _load_applied_rules()
    merged = {}
    for drug_id, records in groups.items():
        drug = _merge_group(drug_id, records, rules_lookup=rules_lookup)
        drug["reliability"] = score_reliability(
            drug, StatementType.DRUG_APPROVAL, review_status=review.status(drug)
        ).value
        merged[drug_id] = drug

    # I-8 guard: the provenance must explain the id the record asserts.
    chain_problems = [p for d in merged.values()
                      for p in validate_mention_chain(d.get("identity") or {})]
    if chain_problems:
        logger.warning("provenance chain violations: %d", len(chain_problems))
        for p in chain_problems[:10]:
            logger.warning("  %s", p)
    else:
        logger.info("provenance chains intact across %d drugs", len(merged))

    # Phase 5: Sort and enrich
    drug_list = sorted(merged.values(), key=lambda d: pv.drug_id(d))

    # Enrichment pipeline
    from medic.enrichment.atc_smiles import enrich_atc_smiles
    from medic.enrichment.drug_tags import classify_drugs
    from medic.enrichment.combination import detect_combinations
    from medic.enrichment.pharos import enrich_pharos
    from medic.enrichment.rxnorm_extension import enrich_rxnorm_extension
    from medic.enrichment.rxnorm_resolve import enrich_rxcui_xrefs

    enrich_atc_smiles(drug_list)
    classify_drugs(drug_list)
    detect_combinations(drug_list)
    enrich_pharos(drug_list)
    enrich_rxnorm_extension(drug_list)
    enrich_rxcui_xrefs(drug_list)

    # Nest the ATC fields into one object and collapse the boolean is_* tags into a
    # single features[] list (v2.0 shape; the flat fields stay additively until rebuild).
    for d in drug_list:
        _nest_atc_and_features(d)

    # Write output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    content = yaml.dump(
        {"drugs": drug_list},
        default_flow_style=False,
        allow_unicode=True,
        width=1000,
    )
    # Remove null bytes and control chars from output
    content = "".join(c for c in content if c == "\n" or c == "\t" or (ord(c) >= 32))
    with open(output_path, "w") as f:
        f.write(content)

    logger.info("Merged %d drugs -> %s", len(drug_list), output_path)
    return drug_list


def main():
    logging.basicConfig(level=logging.INFO)
    merge_drugs()


if __name__ == "__main__":
    main()

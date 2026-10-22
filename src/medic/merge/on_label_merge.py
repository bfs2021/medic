"""On-label merge: combines on-label source records into unified lists.

Reads all kb/indications/<source>/*.yaml files, deduplicates by
(drug_id, disease_id, relationship_type), merges evidence,
and writes products/indication_list.yaml and products/contraindication_list.yaml.
"""

import logging
from pathlib import Path

import yaml

from medic import product_view as pv
from medic.enabler_partners import WRONG_PAIRING, enabler_class
from medic.grounding_store_view import GroundingStoreView
from medic.mention import mint_mention_id
from medic.confidence import corroboration
from medic.spans import is_truncated, readable_span_indices, spans_for_source, spans_with_role
from medic.validation.extraction_fidelity import (
    assertion_negated,
    entailment_score,
    locatable_anchor,
)
from medic.provenance_build import (
    build_assertion,
    build_confidence_breakdown,
    build_mention,
    validate_pair,
)

logger = logging.getLogger(__name__)

# LOINC section codes for the FDA SPL sections the disease is extracted from.
_LOINC_SECTION = {"INDICATION": "LOINC:34067-9", "CONTRAINDICATION": "LOINC:34070-3"}

#: Private key stashing a record's pre-xref disease id, so the merge-time normalization hop
#: can be recorded as a step. Consumed (and removed) when the disease Mention is built.
PRE_XREF_KEY = "_pre_xref_disease_id"

#: Stage-1 decision store for diseases — the authoritative record of every string->ID
#: decision, used to recover a grounding the source record did not carry (I-4).
DISEASE_GROUNDING_STORE = "mappings/disease_grounding.sssom.tsv"

#: Stage-1 decision store for drugs — the same authoritative record the disease side uses, now
#: that the drug mention is built per-source rather than copied from drug_list.yaml (D4).
DRUG_GROUNDING_STORE = "mappings/drug_grounding.sssom.tsv"

KB_INDICATIONS_DIR = Path("kb/indications")
INDICATION_OUTPUT = Path("products/indication_list.yaml")
CONTRAINDICATION_OUTPUT = Path("products/contraindication_list.yaml")
DISEASE_LIST_PATH = Path("products/disease_list.yaml")
DRUG_LIST_PATH = Path("products/drug_list.yaml")
ORANGEBOOK_PATH = Path("kb/drugs/orangebook/orangebook.yaml")
PURPLEBOOK_PATH = Path("kb/drugs/purplebook/purplebook.yaml")
RUSSIA_PATH = Path("kb/drugs/russia/russia.yaml")
CHINA_PATH = Path("kb/drugs/china/china.yaml")
DRUGS_AT_FDA_URL = (
    "https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm"
    "?event=overview.process&ApplNo={appl_no}"
)
PURPLE_BOOK_URL = "https://purplebooksearch.fda.gov/?query={bla}"
GRLS_URL = "https://grls.rosminzdrav.ru/Default.aspx"
CDE_CHINA_URL = "https://www.cde.org.cn/main/xxgk/listpage/2f78f372c1867de05a2cd5c26a793612"


def _build_fda_artifact_lookup(
    orangebook_path: Path = ORANGEBOOK_PATH,
    purplebook_path: Path = PURPLEBOOK_PATH,
) -> dict[str, list[dict]]:
    """Map drug CURIE -> list of FDA authoritative artifacts (Orange/Purple Book).

    Each artifact is a dict carrying source name, deep-linked URL, and
    per-source identifier (NDA list for Orange Book, BLA list for Purple Book,
    plus approval_date when available).

    These are emitted as ADDITIONAL `regulatory_status` rows alongside any
    DailyMed-derived row, since label record (DailyMed) and marketing
    authorisation record (Drugs@FDA / Purple Book) describe different things.
    """
    out: dict[str, list[dict]] = {}
    if orangebook_path.exists():
        try:
            with open(orangebook_path) as f:
                ob = yaml.safe_load(f) or []
        except Exception:
            ob = []
        for rec in ob:
            curie = rec.get("normalized_id", "")
            if not curie:
                continue
            appl_str = (rec.get("application_number", "") or "").strip()
            if not appl_str:
                continue
            # Keep all NDAs, pipe-joined; URL points to first one
            first_appl = appl_str.split("|")[0].strip()
            artifact = {
                "source": "ORANGEBOOK",
                "source_role": "PRIMARY",
                "application_number": appl_str,
                "regulatory_document_url": DRUGS_AT_FDA_URL.format(appl_no=first_appl),
            }
            approval_date = (rec.get("approval_date", "") or "").strip()
            if approval_date:
                artifact["approval_date"] = approval_date
            out.setdefault(curie, []).append(artifact)
    if purplebook_path.exists():
        try:
            with open(purplebook_path) as f:
                pb = yaml.safe_load(f) or []
        except Exception:
            pb = []
        for rec in pb:
            curie = rec.get("normalized_id", "")
            if not curie:
                continue
            bla = (rec.get("bla_number", "") or "").strip()
            if not bla:
                continue
            first_bla = bla.split("|")[0].strip()
            artifact = {
                "source": "PURPLEBOOK",
                "source_role": "PRIMARY",
                "bla_number": bla,
                "regulatory_document_url": PURPLE_BOOK_URL.format(bla=first_bla),
            }
            approval_date = (rec.get("approval_date", "") or "").strip()
            if approval_date:
                artifact["approval_date"] = approval_date
            out.setdefault(curie, []).append(artifact)
    # Russia: each russia drug record contributes a GRLS artifact (no indication
    # data, but a marker that the drug is registered in Russia). Same shape as
    # FDA artifacts but emitted as authority=MOH_RUSSIA when the merge runs.
    if RUSSIA_PATH.exists():
        try:
            with open(RUSSIA_PATH) as f:
                ru = yaml.safe_load(f) or []
        except Exception:
            ru = []
        for rec in ru:
            curie = rec.get("normalized_id", "")
            if not curie:
                continue
            artifact = {
                "source": "GRLS",
                "authority": "MOH_RUSSIA",
                "source_role": "PRIMARY",
                "regulatory_document_url": GRLS_URL,
            }
            approval_date = (rec.get("approval_date", "") or "").strip()
            if approval_date:
                artifact["approval_date"] = approval_date
            out.setdefault(curie, []).append(artifact)

    if CHINA_PATH.exists():
        try:
            with open(CHINA_PATH) as f:
                cn = yaml.safe_load(f) or []
        except Exception:
            cn = []
        for rec in cn:
            curie = rec.get("normalized_id", "")
            if not curie:
                continue
            artifact = {
                "source": "CDE_CHINA",
                "authority": "NMPA_CHINA",
                "source_role": "PRIMARY",
                "regulatory_document_url": CDE_CHINA_URL,
            }
            approval_date = (rec.get("approval_date", "") or "").strip()
            if approval_date:
                artifact["approval_date"] = approval_date
            out.setdefault(curie, []).append(artifact)

    n_drugs = len(out)
    n_ob = sum(1 for arts in out.values() if any(a["source"] == "ORANGEBOOK" for a in arts))
    n_pb = sum(1 for arts in out.values() if any(a["source"] == "PURPLEBOOK" for a in arts))
    n_ru = sum(1 for arts in out.values() if any(a["source"] == "GRLS" for a in arts))
    n_cn = sum(1 for arts in out.values() if any(a["source"] == "CDE_CHINA" for a in arts))
    logger.info(
        "Loaded artifact links: %d drugs (OB=%d, PB=%d, GRLS=%d, CDE=%d)",
        n_drugs, n_ob, n_pb, n_ru, n_cn,
    )
    return out


# Backwards-compat shim used by the existing test suite (returns first artifact URL only)
def _build_fda_url_lookup(
    orangebook_path: Path = ORANGEBOOK_PATH,
    purplebook_path: Path = PURPLEBOOK_PATH,
) -> dict[str, str]:
    """Legacy single-URL lookup. Prefers Orange Book over Purple Book."""
    artifacts = _build_fda_artifact_lookup(orangebook_path, purplebook_path)
    out: dict[str, str] = {}
    for curie, arts in artifacts.items():
        # Orange Book wins (small molecules first)
        ob = next((a for a in arts if a["source"] == "ORANGEBOOK"), None)
        pb = next((a for a in arts if a["source"] == "PURPLEBOOK"), None)
        if ob:
            out[curie] = ob["regulatory_document_url"]
        elif pb:
            out[curie] = pb["regulatory_document_url"]
    return out


def _build_drug_approval_dates(drug_list_path: Path) -> dict[str, dict[str, str]]:
    """Map drug CURIE -> {authority: that authority's earliest approval date}.

    Keyed by authority rather than collapsed to one earliest date across all of them. A
    `RegulatoryStatus` row names exactly one authority (I-10), so the only date that may fill
    it is a date that authority itself issued. Collapsing first is how warfarin's Russian
    registration date (20061229) ended up on an FDA/DailyMed row: `earliest_approval_date`
    takes `min()` over every authority, and warfarin's only recorded date is the Russian one.
    That put a date the FDA never issued on 2,194 edges at reliability HIGH, and neither I-1
    gate can see it — both compare source against jurisdiction and never read the row's
    content.
    """
    if not drug_list_path.exists():
        return {}
    try:
        with open(drug_list_path) as f:
            data = yaml.safe_load(f)
    except Exception:
        return {}
    out: dict[str, dict[str, str]] = {}
    for drug in data.get("drugs", []):
        curie = pv.drug_id(drug)
        if not curie:
            continue
        for approval in pv.approvals(drug):
            authority = (approval.get("authority") or "").strip()
            date_val = (approval.get("approval_date") or "").strip()
            if not authority or not date_val:
                continue
            per_authority = out.setdefault(curie, {})
            if date_val < per_authority.get(authority, "99999999"):
                per_authority[authority] = date_val
    logger.info("Loaded approval dates for %d drugs", len(out))
    return out


def _build_mondo_xref_map(disease_list_path: Path) -> dict[str, str]:
    """Map non-MONDO disease IDs (UMLS, DOID, OMIM, etc.) to MONDO via crossreferences."""
    if not disease_list_path.exists():
        logger.warning("Disease list not found at %s; skipping MONDO normalization", disease_list_path)
        return {}
    try:
        with open(disease_list_path) as f:
            data = yaml.safe_load(f)
    except Exception:
        logger.warning("Failed to read %s; skipping MONDO normalization", disease_list_path)
        return {}
    xref_map: dict[str, str] = {}
    for disease in data.get("diseases", []):
        mondo_id = disease.get("category_class", "")
        if not mondo_id.startswith("MONDO:"):
            continue
        for xref in disease.get("crossreferences", []):
            if xref and not xref.startswith("MONDO:") and xref not in xref_map:
                xref_map[xref] = mondo_id
    logger.info("Loaded %d non-MONDO -> MONDO mappings", len(xref_map))
    return xref_map


def _normalize_pmda_evidence(record: dict) -> None:
    """Rewrite legacy PMDA URLs and add source_document_url for PDF refs.

    Acts on the `evidence` list in-place. Catches stale `english/search/search.html?q=`
    URLs from older ingest runs and replaces them with the live PmdaSearch landing.
    For PMDA-hosted review-report PDFs, adds `source_document_url` symmetrically
    to the DailyMed pattern.
    """
    for ev in record.get("evidence", []) or []:
        if (ev.get("jurisdiction") or "").upper() != "JAPAN":
            continue
        ref = ev.get("reference") or ""
        new_ref = _normalize_pmda_url(ref)
        if new_ref != ref:
            ev["reference"] = new_ref
            ref = new_ref
        if _is_pmda_review_pdf(ref) and not ev.get("source_document_url"):
            ev["source_document_url"] = ref


def _build_mondo_label_map(disease_list_path: Path = DISEASE_LIST_PATH) -> dict[str, str]:
    """Map MONDO id -> its canonical Mondo label.

    Stage-2 normalization used to rewrite the *id* and leave the *label* alone, so a record
    that correctly reached Mondo still shipped whatever string the grounder attached on the
    way — 961 of 5,544 MONDO-resolved pairs carried a non-Mondo label (`MYCOSES`,
    `Ulcer of esophagus NOS`, `Crisis addisonian` on `MONDO:0019801`).

    That is wrong on its own terms: the label is supposed to name the resolved entity, and a
    reader comparing two records cannot tell a synonym from a different disease. It also
    happens to be how MedDRA and SNOMED term text reached the products, since the UMLS index
    supplies whichever atom it picked as the label (see `grounding/lexical/loaders/umls.py`).
    """
    if not disease_list_path.exists():
        logger.warning("Disease list not found at %s; canonical labels unavailable",
                       disease_list_path)
        return {}
    try:
        with open(disease_list_path) as f:
            data = yaml.safe_load(f)
    except Exception:
        logger.warning("Failed to read %s; canonical labels unavailable", disease_list_path)
        return {}
    out: dict[str, str] = {}
    for disease in data.get("diseases", []):
        mondo_id = disease.get("category_class", "")
        label = (disease.get("label") or "").strip()
        if mondo_id.startswith("MONDO:") and label:
            out[mondo_id] = label
    logger.info("Loaded %d canonical Mondo labels", len(out))
    return out


def _canonical_disease_label(
    disease_id: str,
    current: str,
    label_map: dict[str, str] | None,
    store_label: str | None = None,
) -> str:
    """The label that belongs to ``disease_id``, falling back to what the record carried.

    Applies to every MONDO-resolved record, not only the ones the xref hop rewrote: a record
    grounded straight to Mondo could still have picked up a foreign label from the index.

    For a record resting at a non-MONDO id the decision store is authoritative (I-4), and
    ``store_label == ""`` is a decision, not a miss: the concept is named only by a vocabulary
    I-14 rule 2 forbids publishing, so it ships unnamed here too. Falling through to ``current``
    in that case is what put 104 MedDRA strings into the KGX export — the grounding store had
    already blanked them, and the record kept the label its ingest run attached.
    """
    if not disease_id:
        return current
    if label_map and label_map.get(disease_id):
        return label_map[disease_id]
    if store_label is not None:
        return store_label
    return current


def _normalize_disease_id(record: dict, xref_map: dict[str, str]) -> None:
    """Rewrite disease ID in-place to MONDO if a crossreference mapping exists.

    Stashes the pre-rewrite id under :data:`PRE_XREF_KEY` so the merge-time hop can be
    recorded as a real NormalizationStep on the mention (I-8) — otherwise the trail would
    end at the source's id while the record asserts the MONDO one.
    """
    disease_id = _get_disease_id(record)
    if not disease_id or disease_id.startswith("MONDO:"):
        return
    mondo_id = xref_map.get(disease_id)
    if mondo_id:
        record[PRE_XREF_KEY] = disease_id
        if "final_normalized_disease_id" in record:
            record["final_normalized_disease_id"] = mondo_id
        if "normalized_disease_id" in record:
            record["normalized_disease_id"] = mondo_id
        # Update drug_disease compound key if present
        drug_id = _get_drug_id(record)
        if drug_id and "drug_disease" in record:
            record["drug_disease"] = f"{drug_id}|{mondo_id}"


def merge_on_label() -> None:
    """Merge on-label source records from all sources."""
    associations: dict[str, dict] = {}
    seen_records: set[str] = set()
    xref_map = _build_mondo_xref_map(DISEASE_LIST_PATH)
    mondo_labels = _build_mondo_label_map(DISEASE_LIST_PATH)
    drug_approval_dates = _build_drug_approval_dates(DRUG_LIST_PATH)
    drug_store = GroundingStoreView(DRUG_GROUNDING_STORE, "drug").load()
    logger.info("Loaded %d drug grounding decisions from the store", len(drug_store))
    section_warrants = _load_section_warrants()
    fda_artifacts = _build_fda_artifact_lookup()
    grounding_store = GroundingStoreView(DISEASE_GROUNDING_STORE, "disease").load()
    logger.info("Loaded %d disease grounding decisions from the store", len(grounding_store))
    normalized_count = 0

    # Failures are collected, not swallowed. The `try` used to wrap the whole per-file record
    # loop, so one bad record silently discarded every record after it in that file — up to
    # 4,024 for DailyMed — and the build still exited 0. Now the read and each record fail
    # separately, both are counted per source, and `merge_on_label` raises at the end (see
    # `_report_failures`): every failure is reported before anything stops, which is the point
    # of collecting, but a build that lost records does not get to look like a clean one.
    failures: list[str] = []

    for source_dir in sorted(KB_INDICATIONS_DIR.iterdir()):
        if not source_dir.is_dir():
            continue
        for yaml_file in sorted(source_dir.glob("*.yaml")):
            source = source_dir.name.upper()
            try:
                with open(yaml_file) as f:
                    records = yaml.safe_load(f)
            except (OSError, yaml.YAMLError):
                logger.error("Failed to read %s", yaml_file, exc_info=True)
                failures.append(f"{source}: unreadable file {yaml_file}")
                continue
            if not records:
                continue
            if isinstance(records, dict):
                records = [records]
            for index, record in enumerate(records):
                try:
                    # kb/indications records carry no `source` field — the directory IS the
                    # source (kb/indications/<source>/). Stamp it so the mention records where
                    # it came from and the section-warrant lookup can key on it.
                    record.setdefault("source", source)
                    before = _get_disease_id(record)
                    _normalize_disease_id(record, xref_map)
                    _normalize_pmda_evidence(record)
                    after = _get_disease_id(record)
                    if before != after:
                        normalized_count += 1
                    pair_key = _pair_key(record)
                    record_key = _make_key(record)
                    if pair_key is None or record_key is None:
                        continue
                    if record_key in seen_records:
                        continue
                    seen_records.add(record_key)
                    pair = associations.get(pair_key)
                    if pair is None:
                        pair = {
                            "drug_id": _get_drug_id(record),
                            "drug_label": (record.get("final_normalized_drug_label", "")
                                           or record.get("normalized_drug_label", "")),
                            "disease_id": _get_disease_id(record),
                            # The label follows the id: once the chain lands on a canonical
                            # MONDO id, the canonical Mondo label is what names it.
                            "disease_label": _canonical_disease_label(
                                _get_disease_id(record),
                                (record.get("final_normalized_disease_label", "")
                                 or record.get("normalized_disease_label", "")),
                                mondo_labels,
                                grounding_store.label_for(_get_disease_id(record))
                                if grounding_store is not None else None),
                            "relationship_type": record.get("relationship_type", ""),
                            "assertions": [],
                        }
                        for flag in ("is_allergen", "is_diagnostic_agent"):
                            if record.get(flag) is not None:
                                pair[flag] = record[flag]
                        associations[pair_key] = pair
                    for sa in _build_source_assertions(
                            record, drug_approval_dates, fda_artifacts=fda_artifacts,
                            store=grounding_store, drug_store=drug_store,
                            warrants=section_warrants, mondo_labels=mondo_labels):
                        _append_assertion(pair, sa)
                except Exception:
                    # The record index and its ids, not just the filename: "something in
                    # dailymed/indications.yaml" is not a debuggable message for a 4,024-row file.
                    logger.error("Failed to merge %s record %d (%s -> %s)", yaml_file, index,
                                 _get_drug_id(record) or "?", _get_disease_id(record) or "?",
                                 exc_info=True)
                    failures.append(
                        f"{source}: record {index} of {yaml_file.name} "
                        f"({_get_drug_id(record) or '?'} -> {_get_disease_id(record) or '?'})")

    logger.info("Normalized %d disease IDs to MONDO", normalized_count)

    # A pair is created before its assertions are built, so a record that fails mid-build
    # leaves the pair behind with an empty `assertions` list — identity and no evidence, which
    # asserts nothing and would ship as a real association. Drop them; the failure itself is
    # already recorded above.
    empty = [key for key, pair in associations.items() if not pair.get("assertions")]
    for key in empty:
        pair = associations.pop(key)
        logger.error("Dropping pair with no assertions: %s -> %s",
                     pair.get("drug_id"), pair.get("disease_id"))
    if empty:
        failures.append(f"MERGE: {len(empty)} pair(s) left with no assertions")

    # Order the assertions and compute the pair-level aggregate before anything reads them.
    for pair in associations.values():
        _finalize_pair(pair)

    # Stamp the computed reliability tier onto each pair (the single quality metric).
    from medic.reliability import score_reliability, StatementReviewStore
    review = StatementReviewStore().load()
    for assoc in associations.values():
        assoc["reliability"] = score_reliability(
            assoc, review_status=review.status(assoc)
        ).value

    # Invariant guard: I-8/I-8b chain integrity, I-10 source consistency, I-11 confidence
    # completeness, I-12 terminal normalization, I-13 pair aggregation. Every violation is
    # collected and logged before anything raises — the count is the signal, and stopping at
    # the first hides how many records are affected. But SPEC §4 calls these hard rules, so a
    # build that breaks one does not get to exit 0 and write products anyway.
    n_assertions = sum(len(p.get("assertions") or []) for p in associations.values())
    problems = [p for pair in associations.values() for p in validate_pair(pair)]
    if problems:
        logger.error("provenance invariant violations: %d", len(problems))
        for p in problems[:20]:
            logger.error("  %s", p)
        if len(problems) > 20:
            logger.error("  ... and %d more", len(problems) - 20)
        failures.append(f"{len(problems)} provenance invariant violation(s)")
    else:
        logger.info("provenance invariants hold across %d pairs / %d assertions",
                    len(associations), n_assertions)

    indications = []
    contraindications = []
    for assoc in sorted(
        associations.values(),
        key=lambda a: (
            a.get("drug_id", ""),
            a.get("disease_id", ""),
            a.get("relationship_type", ""),
        ),
    ):
        if assoc.get("relationship_type") == "INDICATION":
            indications.append(assoc)
        else:
            contraindications.append(assoc)

    INDICATION_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with open(INDICATION_OUTPUT, "w") as f:
        yaml.dump({"associations": indications}, f, default_flow_style=False, allow_unicode=True)

    with open(CONTRAINDICATION_OUTPUT, "w") as f:
        yaml.dump({"associations": contraindications}, f, default_flow_style=False, allow_unicode=True)

    logger.info(
        "Merged %d indications, %d contraindications",
        len(indications),
        len(contraindications),
    )

    # Products are written first, deliberately: a failed build you can diff is more useful
    # than no build at all, and `reports/build_qc.yaml` needs something to read. The non-zero
    # exit is what stops it being mistaken for a good one.
    _report_failures(failures)


class MergeFailed(RuntimeError):
    """The merge lost records or broke an invariant. Products may be on disk; do not ship them."""


def _report_failures(failures: list[str]) -> None:
    """Raise if the merge lost anything, after every failure has already been logged."""
    if not failures:
        return
    by_source: dict[str, int] = {}
    for entry in failures:
        by_source[entry.split(":", 1)[0]] = by_source.get(entry.split(":", 1)[0], 0) + 1
    summary = ", ".join(f"{k}={v}" for k, v in sorted(by_source.items()))
    raise MergeFailed(
        f"{len(failures)} merge failure(s) — {summary}. The products in products/ are "
        f"incomplete; see the ERROR log above for each one. Fix the source records or the "
        f"invariant they break; do not release this build."
    )


def _get_drug_id(record: dict) -> str:
    """Get drug ID from either field naming convention."""
    return record.get("final_normalized_drug_id", "") or record.get("normalized_drug_id", "")


def _get_disease_id(record: dict) -> str:
    """Get disease ID from either field naming convention."""
    return record.get("final_normalized_disease_id", "") or record.get("normalized_disease_id", "")


def _claim_context_key(record: dict) -> str:
    """The qualifying context, as part of the pair key (#90.3, #24).

    Two claims that differ only in context are two claims. With context out of the key they
    collapsed into one association whose qualifiers were whichever record merged first — and
    formoterol, indicated for asthma *maintenance* and contraindicated for *acute attacks*,
    became a self-clash on one pair (#93). That is why a clash gate was the wrong
    instrument: it would have demoted a correct pair instead of telling the two apart.

    Built from the first evidence row, since a record carries one. Contexts are sorted so
    the key does not depend on the order the model listed them, and the relation is included
    because prevention and treatment of one disease are different claims (#99).
    """
    rows = record.get("evidence") or []
    ev = rows[0] if rows and isinstance(rows[0], dict) else {}
    parts = [
        ",".join(sorted(str(c) for c in (ev.get("disease_context") or []))),
        (ev.get("population_context") or "").strip(),
        (ev.get("temporal_context") or "").strip(),
        (ev.get("claim_predicate") or "").strip().lower(),
    ]
    return "|".join(parts) if any(parts) else ""


def _pair_key(record: dict) -> str | None:
    """The canonical pair a record contributes to: drug|disease|relationship|context.

    Source-free on purpose: two regulators stating the same qualified claim must merge into
    one pair with two assertions, which is what the corroboration logic rests on (#61).
    """
    drug_id = _get_drug_id(record)
    disease_id = _get_disease_id(record)
    if not drug_id or not disease_id:
        return None
    if "Error" in drug_id or "Error" in disease_id:
        logger.warning("Skipping record with error ID: %s / %s", drug_id, disease_id)
        return None
    key = f"{drug_id}|{disease_id}|{record.get('relationship_type', '')}"
    context = _claim_context_key(record)
    return f"{key}|{context}" if context else key


def _document_for(record: dict, evidence: dict) -> str:
    """A stable id for the document this record came from (design spec D2).

    Always returns something. The document is half the record key, so an empty one would
    silently re-collapse two documents into one assertion — the bug this model removes.
    """
    source = (record.get("source") or "").upper()
    # An ingester that can name the exact document it read wins outright. PMDA uses this to
    # keep the several approvals of one ingredient apart, which the pipe-joined blob could not.
    explicit = (evidence.get("document_id") or "").strip()
    if explicit:
        return explicit
    setid = (evidence.get("setid") or record.get("set_id") or "").strip()
    if setid:
        return f"DailyMed:{setid}"
    reference = (evidence.get("reference") or "").strip()
    if "ema.europa.eu" in reference:
        return f"EMA:{reference.rstrip('/').rsplit('/', 1)[-1]}"
    if reference:
        return f"{source or 'SOURCE'}:{reference.rstrip('/').rsplit('/', 1)[-1]}"
    # No document identifier at all (PMDA search-URL fallback, GRLS). Key on the source plus
    # the drug, so two drugs from one registry do not collide into one assertion.
    return f"{source or 'SOURCE'}:{_get_drug_id(record) or 'unknown'}"


def _make_key(record: dict) -> str | None:
    """One key per (pair, source, document) — the unit of a SourceAssertion."""
    pair = _pair_key(record)
    if pair is None:
        return None
    evidence = (record.get("evidence") or [{}])[0]
    if not isinstance(evidence, dict):
        evidence = {}
    return f"{pair}|{(record.get('source') or '').upper()}|{_document_for(record, evidence)}"


# Map jurisdiction strings to canonical RegulatoryAuthorityEnum values
_JURISDICTION_TO_AUTHORITY = {
    "USA": "FDA",
    "EU": "EMA",
    "JAPAN": "PMDA",
    "INDIA": "CDSCO",
    "RUSSIA": "MOH_RUSSIA",
    "CHINA": "NMPA_CHINA",
}


_ARTIFACT_JURISDICTION = {
    "ORANGEBOOK": "USA",
    "PURPLEBOOK": "USA",
    "GRLS": "RUSSIA",
    "CDE_CHINA": "CHINA",
}


def _is_search_url(url: str) -> bool:
    """Return True if the URL is a generic search/landing page (not a stable record)."""
    if not url:
        return False
    lo = url.lower()
    return (
        "search.cfm" in lo
        or "search-results" in lo
        or "/search?" in lo
        or "/search/search.html" in lo
        or "/medicines?search_api_fulltext" in lo
        or "/pmdasearch/iyakusearch" in lo
        or "purplebooksearch.fda.gov/?query=" in lo
        or "purplebooksearch.fda.gov/results" in lo
        or "/approved-new-drugs/" in lo
    )


def _identify_source_from_evidence(ev: dict, authority: str) -> str:
    """Infer the DataSourceNameEnum value from evidence content.

    Inspects `source_role` hints, the URL host, and the explanation text.
    Falls back to the authority's default source.
    """
    ref = (ev.get("reference") or "").lower()
    explanation = (ev.get("explanation") or "").lower()
    if "dailymed.nlm.nih.gov" in ref or "dailymed" in explanation:
        return "DAILYMED"
    if "accessdata.fda.gov" in ref:
        return "ORANGEBOOK"
    if "purplebooksearch" in ref or "purple book" in explanation:
        return "PURPLEBOOK"
    if "ema.europa.eu" in ref or "epar" in explanation:
        return "EMA_EPAR"
    if "pmda.go.jp" in ref or "pmda" in explanation:
        return "PMDA"
    if "cdsco.gov.in" in ref or "cdsco" in explanation:
        return "CDSCO"
    if "grls.rosminzdrav" in ref:
        return "GRLS"
    if "cde.org.cn" in ref:
        return "CDE_CHINA"
    # Fall back by authority
    defaults = {
        "FDA": "DAILYMED",
        "EMA": "EMA_EPAR",
        "PMDA": "PMDA",
        "CDSCO": "CDSCO",
        "MOH_RUSSIA": "GRLS",
        "NMPA_CHINA": "CDE_CHINA",
    }
    return defaults.get(authority, "OTHER")


def _extract_setid(url: str) -> str:
    """Extract SPL setid from a DailyMed lookup URL, or empty string."""
    if not url or "lookup.cfm?setid=" not in url:
        return ""
    return url.split("setid=")[-1].split("&")[0].strip()


# PMDA-specific URL constants
_PMDA_DEAD_SEARCH_PREFIX = "https://www.pmda.go.jp/english/search/search.html?q="
_PMDA_DEAD_SEARCH_PREFIX_HTTP = "http://www.pmda.go.jp/english/search/search.html?q="
_PMDA_LIVE_SEARCH_URL = "https://www.pmda.go.jp/PmdaSearch/iyakuSearch/"


def _normalize_pmda_url(url: str) -> str:
    """Rewrite the dead PMDA english/search URL to the live PmdaSearch landing.

    The pattern `https://www.pmda.go.jp/english/search/search.html?q=...` was
    emitted by older ingest paths but now 404s. Replace with the live
    `PmdaSearch/iyakuSearch/` landing page (acceptable fallback).
    All other PMDA URLs (deep-linked PDFs, etc.) are passed through untouched.
    """
    if not url:
        return url
    if url.startswith(_PMDA_DEAD_SEARCH_PREFIX) or url.startswith(_PMDA_DEAD_SEARCH_PREFIX_HTTP):
        return _PMDA_LIVE_SEARCH_URL
    return url


def _is_pmda_review_pdf(url: str) -> bool:
    """True if the URL is a PMDA-hosted PDF (review report or package insert)."""
    if not url:
        return False
    lo = url.lower()
    return "pmda.go.jp" in lo and lo.endswith(".pdf")


# Fields preserved from a dropped INTERMEDIARY row onto its surviving PRIMARY
# counterpart. Keeps audit data (raw source labels) when the per-source ingest
# hasn't yet re-emitted them on the PRIMARY row.
_INTERMEDIARY_CARRYOVER_FIELDS = (
    "original_drug_label",
    "original_disease_label",
    "original_drug_id",
)


def _evidence_dedup_key(ev: dict) -> tuple[str, str]:
    """Compute the (jurisdiction, source) key used for evidence dedup.

    Mirrors `_identify_source_from_evidence` so PRIMARY-vs-INTERMEDIARY rows
    that came from the same authority+source collapse to a single key.
    """
    jur = (ev.get("jurisdiction") or "").upper()
    ref = (ev.get("reference") or "").lower()
    explanation = (ev.get("explanation") or "").lower()
    src = ""
    if "dailymed.nlm.nih.gov" in ref or "dailymed" in explanation:
        src = "DAILYMED"
    elif "accessdata.fda.gov" in ref:
        src = "ORANGEBOOK"
    elif "purplebooksearch" in ref or "purple book" in explanation:
        src = "PURPLEBOOK"
    elif "ema.europa.eu" in ref or "epar" in explanation:
        src = "EMA_EPAR"
    elif "pmda.go.jp" in ref or "pmda" in explanation:
        src = "PMDA"
    elif "cdsco.gov.in" in ref or "cdsco" in explanation:
        src = "CDSCO"
    elif "grls.rosminzdrav" in ref:
        src = "GRLS"
    elif "cde.org.cn" in ref:
        src = "CDE_CHINA"
    return (jur, src)


def _dedup_evidence_prefer_primary(evidence_items: list[dict]) -> list[dict]:
    """Drop INTERMEDIARY rows when a PRIMARY row exists for same (jurisdiction, source).

    Before dropping, copy `original_drug_label` / `original_disease_label` /
    `original_drug_id` from the INTERMEDIARY onto the matching PRIMARY row when
    the PRIMARY doesn't already carry them. This is a stop-gap so audit fields
    aren't lost while per-source ingesters are being re-run with the new code
    that emits these fields on PRIMARY rows directly.

    Non-REGULATORY evidence (LITERATURE, etc.) is passed through unchanged.
    Rows whose key cannot be inferred (no jurisdiction or no recognised source)
    are passed through unchanged so we never drop unclassifiable data.
    """
    if not evidence_items:
        return evidence_items

    # First pass: find all PRIMARY rows by key.
    primary_by_key: dict[tuple[str, str], dict] = {}
    for ev in evidence_items:
        if (ev.get("source_type") or "").upper() != "REGULATORY":
            continue
        if (ev.get("source_role") or "").upper() != "PRIMARY":
            continue
        key = _evidence_dedup_key(ev)
        if not key[0] or not key[1]:
            continue
        # If multiple PRIMARY rows for same key (shouldn't happen but defensive),
        # keep the first; downstream `_build_regulatory_status_from_evidence`
        # will dedup again via `seen` set.
        primary_by_key.setdefault(key, ev)

    if not primary_by_key:
        return evidence_items

    # Second pass: walk the list and drop INTERMEDIARY rows whose key has a
    # PRIMARY counterpart, after carrying over original_* fields.
    result: list[dict] = []
    dropped = 0
    for ev in evidence_items:
        if (ev.get("source_type") or "").upper() != "REGULATORY":
            result.append(ev)
            continue
        role = (ev.get("source_role") or "").upper()
        if role != "INTERMEDIARY":
            result.append(ev)
            continue
        key = _evidence_dedup_key(ev)
        if not key[0] or not key[1]:
            result.append(ev)
            continue
        primary = primary_by_key.get(key)
        if primary is None:
            # No PRIMARY counterpart; keep the INTERMEDIARY row.
            result.append(ev)
            continue
        # Carry over audit fields onto PRIMARY before dropping the INTERMEDIARY.
        for fld in _INTERMEDIARY_CARRYOVER_FIELDS:
            val = ev.get(fld)
            if val and not primary.get(fld):
                primary[fld] = val
        dropped += 1

    if dropped:
        logger.debug("Dropped %d INTERMEDIARY evidence rows superseded by PRIMARY", dropped)
    return result


def _build_regulatory_status_from_evidence(
    evidence_items: list[dict],
    drug_id: str = "",
    drug_approval_dates: dict[str, dict[str, str]] | None = None,
    fda_url_lookup: dict[str, str] | None = None,
    fda_artifacts: dict[str, list[dict]] | None = None,
) -> list[dict]:
    """Derive structured RegulatoryStatus entries from evidence items.

    Emits **one row per (authority, source) artifact**, not one per authority:
    a drug with both a DailyMed label and an Orange Book NDA gets two FDA rows,
    one for each authoritative artifact. This matches the original ask
    ("a label record and a marketing-authorisation record describe different
    things; both are valuable").

    Backwards-compat: `fda_url_lookup` is still accepted for older callers but
    `fda_artifacts` (richer, multi-source per drug) is preferred.
    """
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()  # (authority, source) — one row per pair
    _dates_by_authority = (drug_approval_dates or {}).get(drug_id, {}) if drug_id else {}

    def fallback_date(authority: str) -> str:
        """The drug-level date for *this* authority, never another one's."""
        return _dates_by_authority.get(authority, "")

    # Normalize approval_status helper
    def _norm_status(s: str) -> str:
        s = (s or "APPROVED").upper()
        if s not in ("APPROVED", "WITHDRAWN", "INVESTIGATIONAL", "OFF_LABEL"):
            return "APPROVED"
        return s

    # 1. Rows derived directly from REGULATORY evidence items
    for ev in evidence_items or []:
        if (ev.get("source_type") or "").upper() != "REGULATORY":
            continue
        jurisdiction = (ev.get("jurisdiction") or "").upper()
        authority = _JURISDICTION_TO_AUTHORITY.get(jurisdiction)
        if not authority:
            continue
        source = _identify_source_from_evidence(ev, authority)
        key = (authority, source)
        ref = ev.get("reference") or ""
        # Rewrite legacy dead PMDA search URL on read; the kb yaml may still
        # carry the old `english/search/search.html?q=` pattern from earlier runs.
        if source == "PMDA":
            ref = _normalize_pmda_url(ref)
        entry: dict = {
            "authority": authority,
            "source": source,
            "status": _norm_status(ev.get("approval_status")),
        }
        source_role = ev.get("source_role")
        if source_role:
            entry["source_role"] = source_role
        if ref and str(ref).startswith("http"):
            entry["regulatory_document_url"] = ref
        approval_date = ev.get("approval_date") or fallback_date(authority)
        if approval_date:
            entry["approval_date"] = approval_date
        # Source-specific identifiers
        if source == "DAILYMED":
            setid = _extract_setid(ref)
            if setid:
                entry["setid"] = setid
        # Carry through evidence-level identifiers when present
        if ev.get("product_id"):
            entry["product_id"] = ev["product_id"]
        if ev.get("setid") and source == "DAILYMED" and "setid" not in entry:
            entry["setid"] = ev["setid"]
        if ev.get("application_number"):
            entry["application_number"] = ev["application_number"]
        if ev.get("bla_number"):
            entry["bla_number"] = ev["bla_number"]
        if ev.get("source_document_url"):
            entry["source_document_url"] = ev["source_document_url"]
        # Build DailyMed PDF document URL when we have a setid
        if source == "DAILYMED" and entry.get("setid") and "source_document_url" not in entry:
            entry["source_document_url"] = (
                f"https://dailymed.nlm.nih.gov/dailymed/downloadpdffile.cfm?setid={entry['setid']}"
            )
        # PMDA: when the reference is a deep-linked review-report PDF, expose
        # it ALSO as source_document_url (mirrors the DailyMed pattern).
        if source == "PMDA" and "source_document_url" not in entry and _is_pmda_review_pdf(ref):
            entry["source_document_url"] = ref
        if key in seen:
            # Replace if this entry is richer
            for i, existing in enumerate(rows):
                if (existing.get("authority"), existing.get("source")) == key and len(entry) > len(existing):
                    rows[i] = entry
                    break
        else:
            seen.add(key)
            rows.append(entry)

    # 2. Add OB/PB/GRLS/CDE marketing-registry artifacts as additional rows
    artifacts = (fda_artifacts or {}).get(drug_id, []) if drug_id else []
    artifact_authority_default = {
        "ORANGEBOOK": "FDA",
        "PURPLEBOOK": "FDA",
        "GRLS": "MOH_RUSSIA",
        "CDE_CHINA": "NMPA_CHINA",
    }
    for artifact in artifacts:
        source = artifact["source"]
        authority = artifact.get("authority") or artifact_authority_default.get(source, "FDA")
        key = (authority, source)
        if key in seen:
            continue
        seen.add(key)
        entry = {
            "authority": authority,
            "source": source,
            "status": "APPROVED",
            "source_role": artifact.get("source_role", "PRIMARY"),
            "regulatory_document_url": artifact.get("regulatory_document_url", ""),
        }
        approval_date = artifact.get("approval_date") or fallback_date(authority)
        if approval_date:
            entry["approval_date"] = approval_date
        if "application_number" in artifact:
            entry["application_number"] = artifact["application_number"]
        if "bla_number" in artifact:
            entry["bla_number"] = artifact["bla_number"]
        # Pass through source_document_url when the artifact builder was able
        # to construct one. Currently no marketing-registry artifact emits it:
        # - ORANGEBOOK (Drugs@FDA labels): the canonical URL is
        #     https://www.accessdata.fda.gov/drugsatfda_docs/label/<year>/<NDA>[s###]lbl.pdf
        #   but <year> is the year of the most recent label revision (NOT the
        #   approval year stored in Orange Book) and the actual filename almost
        #   always carries a supplement suffix (e.g. 020233s007lbl.pdf), so it
        #   cannot be constructed deterministically without scraping each
        #   Drugs@FDA detail page. Verified 404s for plausible guesses against
        #   real NDAs (1994/020233lbl.pdf, 2023/214070lbl.pdf, 2016/208294lbl.pdf,
        #   yearless 214070lbl.pdf). Skipping per the conservative principle.
        # - PURPLEBOOK (BLA approval letters): linked from per-BLA detail pages
        #   on purplebooksearch.fda.gov. TODO: add a separate scraping pass to
        #   capture the approval-letter PDF URL per BLA.
        # - GRLS / CDE_CHINA: no per-record document URL pattern.
        if artifact.get("source_document_url"):
            entry["source_document_url"] = artifact["source_document_url"]
        rows.append(entry)

    # 3. Legacy fda_url_lookup fallback: only fires when no artifact and existing
    #    FDA row carries only a search URL. (Kept so old test paths still work.)
    if fda_url_lookup and drug_id in fda_url_lookup:
        fda_deep_link = fda_url_lookup[drug_id]
        for row in rows:
            if row.get("authority") != "FDA":
                continue
            ref = row.get("regulatory_document_url", "")
            if not ref or _is_search_url(ref):
                row["regulatory_document_url"] = fda_deep_link

    return rows


#: Curated per-source structural warrants (conf/section_warrants.yaml): which source sections
#: assert which relation *by construction*, used when the text carries no cue phrase.
SECTION_WARRANTS_PATH = Path("conf/section_warrants.yaml")


def _load_section_warrants(path: Path = SECTION_WARRANTS_PATH) -> dict[str, str]:
    """Load ``<SOURCE>[/<section_code>] -> relationship`` from the curated warrant table.

    The file is a list of ``SectionWarrant`` records (schema-governed); the composite lookup key
    is built here and exists only in memory. It used to be a map keyed by the composite string,
    which could not be validated and forced the key to be split on read.
    """
    if not path.exists():
        logger.warning("Section warrants not found at %s; unmatched claims stay none_found", path)
        return {}
    try:
        with open(path) as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:
        logger.warning("Failed to read %s; unmatched claims stay none_found", path)
        return {}
    out: dict[str, str] = {}
    for warrant in (data.get("warrants") or []):
        if not isinstance(warrant, dict):
            continue
        source = str(warrant.get("source") or "").strip()
        rel = str(warrant.get("relationship") or "").strip()
        if not source or not rel:
            continue
        section = str(warrant.get("section_code") or "").strip()
        key = f"{source}/{section}" if section else source
        out[key.upper()] = rel.upper()
    logger.info("Loaded %d section warrants", len(out))
    return out


def _warrant_for(
    warrants: dict[str, str], source: str, section_code: str, relationship: str,
) -> str | None:
    """The warrant key licensing ``relationship`` for this source/section, if any.

    Checks the specific ``SOURCE/section`` key before the bare ``SOURCE`` key, and only
    returns a warrant that *matches* the relation being asserted — a warrant for indications
    must never license a contraindication.
    """
    src = (source or "").upper()
    rel = (relationship or "").upper()
    for key in (f"{src}/{section_code}" if section_code else "", src):
        if key and warrants.get(key.upper()) == rel:
            return key
    return None


def _polarity_flags(
    raw: str, check_text: str, spans: list[dict], span_index: int | None,
    verbatim: str = "",
) -> tuple[bool, list[str]]:
    """Is this INDICATION inverted, and what claim-level flags does it earn?

    Three checks, deliberately separated by how destructive their verdict is:

    * **strict, on the claim's own span** — the full disease phrase occurs in the text the
      claim was read from, and *every* occurrence sits inside a negated scope. Returns
      ``negated=True``, which `build_assertion` turns into ``negated_inversion`` and the
      reliability gate turns into EXCLUDED. ``head_fallback=False``, so a drop never fires
      on a head word belonging to a different disease ("vertebral" borrowing "hip").

    * **lenient, on the claim's own span** — the same check anchored on the head content
      word. Too loose to exclude a record on, but it is what catches
      ``raloxifene -> vertebral fractures`` from "a significant reduction in the incidence of
      vertebral, **but not** hip fractures": the disease is in the text but not in an
      indication relationship to the drug. That is `over_extraction` (5.2) — LOW, not
      EXCLUDED.

    * **the Limitations-of-Use spans the claim was NOT read from** — a disease whose only
      textual basis is a strictly-negated mention inside a scope restriction was never
      indicated at all. Guarded by "and the claim's own span does not support it", because a
      label routinely says "indicated for X" *and* "Limitations of Use: not indicated for X
      in patients under 12", which is a legitimate indication with a restriction, not an
      inversion.

    The third check exists because the span filter that keeps LIMITATION_STATEMENT out of the
    claim's scope also kept it out of the negation check's scope — so the one place inversions
    are most likely to hide was the one place nothing looked.

    When none of the three can locate the disease in the source at all, the claim earns
    ``polarity_unverified``: the check did not run, which is not the same as passing it
    (#59). That is a recording flag, not a verdict — it deliberately does not move the
    reliability tier, since the entailment score already grades how well the source
    supports the claim.
    """
    limitation = " ".join(
        s["text"] for s in spans_with_role(
            spans, "LIMITATION_STATEMENT", exclude=span_index)
    )
    # Search for whatever the ingest screen searched for. `original_disease_verbatim` is
    # the source's own wording and is present by construction; the canonical name often is
    # not — 1,108 of 6,772 rebuilt DailyMed rows are locatable only via the verbatim. While
    # this function anchored on the name alone, the two halves of the polarity check were
    # looking for different strings, which is the drift #59 fixed for span scope. It also
    # silently voided #66's safety net: that change stopped dropping limitation-only rows
    # at ingest *because* this function would flag them, which only holds when both halves
    # can see the same disease.
    anchor = locatable_anchor(raw, verbatim, check_text, limitation)

    strict_neg, strict_total, _ = assertion_negated(anchor, check_text, head_fallback=False)
    if strict_total and strict_neg == strict_total:
        return True, []

    flags: list[str] = []
    lenient_neg, lenient_total, _ = assertion_negated(anchor, check_text, head_fallback=True)
    if lenient_total and lenient_neg == lenient_total:
        flags.append("over_extraction")

    # Only meaningful when the claim's own span gives the disease no positive support.
    if entailment_score(anchor, check_text) == 0.0:
        if limitation:
            neg, total, _ = assertion_negated(anchor, limitation, head_fallback=False)
            if total and neg == total:
                return True, flags

    # Nothing above reached a verdict: neither the strict nor the lenient anchor could
    # locate the disease, and no limitation span spoke to it. Recording nothing here
    # laundered "not checked" into "checked and clean" — the #59 hole. The usual cause is
    # the extraction prompt asking the LLM to canonicalise a name the source spells
    # differently, which leaves no anchor to scan back from.
    #
    # `lenient_total` is part of the condition on purpose: a head-word hit is too loose to
    # drop a record on, but it *is* a verdict — it is what produced `over_extraction` above.
    # A claim carrying that flag was checked and found wanting, not left unchecked. That
    # narrows the flag to 207 of 11696 shipped INDICATIONs (1.8%); the ingest screen's own
    # strict-only bucket is the wider 12% (see extraction_fidelity.ScreenResult).
    if strict_total == 0 and lenient_total == 0:
        flags.append("polarity_unverified")
    return False, flags


def _resolution_confidence(mention: dict | None) -> float | None:
    """A Mention's aggregate resolution confidence (``None`` when it has no trail)."""
    if not isinstance(mention, dict):
        return None
    resolution = mention.get("resolution")
    if not isinstance(resolution, dict):
        return None
    conf = resolution.get("confidence")
    try:
        return float(conf) if conf is not None else None
    except (TypeError, ValueError):
        return None


def _build_drug_mention(
    record: dict, evidence: dict, drug_id: str, drug_label: str,
    *, store: GroundingStoreView | None = None,
) -> dict:
    """The drug Mention for ONE source document — the mirror of _build_disease_provenance.

    Replaces the merge-elected identity from ``drug_list.yaml``, which stamped one source's
    trail onto every association naming that drug (design spec §1). The literal comes from
    THIS document's ``original_drug_label``; the trail is recovered from the decision store by
    the same lookup the disease side uses, so a drug absent from ``drug_list.yaml`` still gets
    a trail instead of the 462-record hole.
    """
    raw = (evidence.get("original_drug_label") or "").strip()
    asserted = not raw
    if asserted:
        # The source gave no drug string of its own. Do NOT present the canonical label as if
        # it were verbatim (I-7) — fall back for naming, but record the grounding as
        # source_asserted so the record admits nothing was matched.
        raw = drug_label or drug_id

    mention_id = mint_mention_id(raw, "drugs")
    grounding, applied_rules, predicate_id, gflags = None, [], "", []
    if store is not None and not asserted:
        dec = store.decision_for(mention_id=mention_id, literal=raw, object_id=drug_id)
        if dec:
            grounding = dec.as_grounding(original_string=raw)
            applied_rules = list(dec.applied_rules)
            predicate_id, gflags = dec.predicate_id, dec.flags()
    if grounding is None and drug_id:
        grounding = {
            "original_string": raw,
            "grounded_id": drug_id,
            "grounded_label": drug_label or None,
            "grounding_quality": "source_asserted",
        }

    # I-12: a chain ending in a CURIE ends with a NORMALIZATION step, identity or not, so a
    # reader can tell "no normalization was needed" from "none was recorded".
    normalization = None
    if drug_id:
        normalization = {
            "original_id": drug_id,
            "normalized_id": drug_id,
            "normalized_label": drug_label or None,
            "normalization_quality": "identity",
        }

    source = (record.get("source") or "").upper()
    return build_mention(
        original_literal=raw,
        entity_type="drug",
        normalization=normalization,
        mention_id=mention_id,
        source=source or None,
        extraction={"output_value": raw, "method": "STRUCTURED_FIELD",
                    "tool": f"medic-ingest-{source.lower()}" if source else "medic-ingest"},
        grounding=grounding,
        applied_rules=applied_rules,
        grounding_predicate=predicate_id,
        grounding_flags=gflags,
        resolved_id=drug_id or None,
        resolved_label=drug_label or None,
    )


def _build_disease_provenance(
    record: dict, assoc: dict, disease_id: str, disease_label: str,
    store: GroundingStoreView | None = None,
    warrants: dict[str, str] | None = None,
) -> tuple[dict | None, dict | None]:
    """Assemble the inlined disease Mention **and** the association's Assertion.

    Two distinct concerns, deliberately kept apart:

    * the **Mention** answers "what entity is this string?" — the recognition of the disease
      in this label's text plus its grounding/normalization trail (association-specific,
      because the disease is extracted per-indication). Its ExtractionStep carries only
      recognition-level signals.
    * the **Assertion** answers "what is the source claiming about it?" — the supporting
      quote, how well it supports the claim, and relation-level failure modes (negation,
      over-extraction, wrong section). The relation itself is named by the association's
      ``relationship_type``, never repeated here.

    The raw disease string comes from evidence[0].original_disease_label; the section text
    becomes the single TextSpan the extraction quotes a substring of.
    """
    evidence = assoc.get("evidence") or []
    ev0 = evidence[0] if evidence and isinstance(evidence[0], dict) else {}
    raw = (ev0.get("original_disease_label") or disease_label or "").strip()
    # The source's own wording for this disease, when the ingester recorded it (#64).
    verbatim = (ev0.get("original_disease_verbatim") or "").strip()
    if not raw:
        return None, None
    rel = (assoc.get("relationship_type") or "").upper()
    # Section text comes from the source record (indications_text is no longer stored on
    # the association — it lives once here as the Mention's TextSpan).
    section = (record.get("indications_text") or record.get("raw_indication_text") or "").strip()
    snippet = (ev0.get("snippet") or "").strip()
    # The SPL's own element boundaries, when the ingester kept them. Splitting the
    # concatenation instead let a `Limitations of Use` marker in one element govern the
    # next one's text (#65). Absent on records mined before that was recorded.
    parts = record.get(
        "contraindications_text_parts" if rel == "CONTRAINDICATION"
        else "indications_text_parts")
    parts = [p for p in parts if isinstance(p, str)] if isinstance(parts, list) else None

    source = (record.get("source") or "").upper()
    # Same document id the owning assertion uses, so every span says which document it came
    # from (I-10). This used to hardcode the DailyMed setid, leaving EMA/PMDA/India spans with
    # an empty document while their assertion carried a real one.
    document = _document_for(record, ev0)
    section_code = _LOINC_SECTION.get(rel, "") if source == "DAILYMED" else ""
    spans = spans_for_source(
        source, section or snippet, document=document, section_code=section_code,
        # Only meaningful when the spans describe the section; a snippet fallback is
        # already one chunk, and the parts would not line up with it.
        parts=parts if section else None)

    # The extraction reads the first span that is neither a header nor a scope restriction
    # (medic.spans.readable_span_indices — the same definition the destructive ingest screen
    # now reads, so the two halves cannot drift again; that drift was issue #59). The check
    # used to run over " ".join([snippet, section]), i.e. the whole flattened section.
    readable = readable_span_indices(spans)
    span_index = readable[0] if readable else None

    # --- claim-level: how well does the source support THIS relation, and is it negated? ---
    support, negated = None, False
    claim_flags: list[str] = []
    check_text = spans[span_index]["text"] if span_index is not None else (snippet or raw)
    if check_text:
        # Scored against the string the source actually contains, so a label that writes
        # "MI" where the LLM returned "myocardial infarction" is not recorded as having
        # zero support for its own indication.
        support = entailment_score(
            locatable_anchor(raw, verbatim, check_text, ""), check_text)
        if rel == "INDICATION":
            negated, claim_flags = _polarity_flags(
                raw, check_text, spans, span_index, verbatim)
    # A class rule, not a text rule: an enabler partner (beta-lactamase inhibitor,
    # cilastatin, cobicistat) never owns a treatment indication, so an INDICATION on one is
    # the combination partner's claim credited to the wrong active moiety (FAILURE_MODES
    # 5.4, #88). The text supports the claim perfectly — for the product. The flag is what
    # a reader sees; `reliability._enabler_gate` applies the same verdict at score time.
    drug_mention = assoc.get("drug") if isinstance(assoc.get("drug"), dict) else {}
    if rel == "INDICATION" and enabler_class(
            drug_mention.get("resolved_label") or drug_mention.get("original_literal")):
        if WRONG_PAIRING not in claim_flags:
            claim_flags.append(WRONG_PAIRING)
    warrant = _warrant_for(warrants or {}, record.get("source") or "", section_code, rel)
    assertion = build_assertion(
        supporting_quote=snippet or raw,
        method="LLM",
        relationship=rel,
        section_warrant=warrant,
        subject_confidence=_resolution_confidence(assoc.get("drug")),
        relationship_confidence=support,
        negated=negated,
        flags=claim_flags,
    )

    # --- entity-level: recognising the disease mention (no relation info) ---
    # A span sitting on the ingester's snippet cap was cut, so the mention may be supported by
    # text we never saw (FAILURE_MODES 5.6). EMA slices every snippet to SNIPPET_CHAR_CAP, so
    # this is the recognition signal that actually has a detector — unlike `hallucination`,
    # which entailment alone cannot tell apart from a correct synonym (see _extraction_step).
    #
    # Truncation alone is not the failure: if the disease occurs verbatim in the text we DID
    # see (entailment 1.0), what got cut is irrelevant to recognising it. The flag fires only
    # where the two coincide — the span was cut *and* the visible text does not fully support
    # the mention, so the missing text is a live explanation for the gap. Flagging on length
    # alone moved 2,207 fully-supported records to MEDIUM and said nothing about any of them.
    truncated = is_truncated(check_text) and (support is None or support < 1.0)
    extraction = {
        "supporting_quote": check_text,
        "output_value": raw,
        "method": "LLM",
        "confidence": support,
        "span_index": span_index,
        # The source's own wording, so char_start/char_end can locate the text the
        # extraction read rather than the canonical name it produced (#64).
        "verbatim": verbatim,
        "flags": ["truncated_snippet"] if truncated else [],
    }
    grounding = record.get("disease_grounding") or record.get("grounding")
    normalization = record.get("disease_normalization") or record.get("normalization")
    grounding = grounding if isinstance(grounding, dict) else None
    normalization = normalization if isinstance(normalization, dict) else None

    # The decision store is the authoritative record (I-4). Use it to (a) recover a grounding
    # the source record never carried, and (b) supply the preprocessing rules / predicate /
    # flags that the funneled object omits.
    mention_id = mint_mention_id(raw, "diseases")
    applied_rules: list[str] = []
    predicate_id, gflags = "", []
    if store is not None:
        want = (grounding or {}).get("grounded_id") or ""
        dec = store.decision_for(mention_id=mention_id, literal=raw, object_id=want)
        if dec:
            if grounding is None:
                grounding = dec.as_grounding(original_string=raw)
            else:
                # The record's own grounding object was written by whichever ingest run
                # produced it, so its `grounded_label` is a snapshot of what the index called
                # the target back then. The store is the authoritative record (I-4) and the
                # label is derived, not a decision — so refresh it. Without this a label
                # retired from the index (I-14 restricted a vocabulary, or the preference
                # order changed) lives on in kb/ until every source is re-ingested.
                grounding["grounded_label"] = dec.object_label
            applied_rules = list(dec.applied_rules)
            predicate_id, gflags = dec.predicate_id, dec.flags()

    # No grounding object and no decision in the store, yet the source carried an id: it was
    # supplied pre-grounded. Record that honestly (`source_asserted`) rather than leaving a
    # chain that stops at a string while the record asserts an ontology id.
    if grounding is None and disease_id:
        grounding = {
            "original_string": raw,
            "grounded_id": disease_id,
            "grounded_label": disease_label or None,
            "grounding_quality": "source_asserted",
        }

    # A merge-time xref hop (HP/UMLS -> MONDO) is a real transformation: record it.
    merge_normalization = None
    pre_xref = record.get(PRE_XREF_KEY)
    chain_end = ((normalization or {}).get("normalized_id")
                 or (grounding or {}).get("grounded_id") or pre_xref)
    if disease_id and chain_end and chain_end != disease_id:
        merge_normalization = {
            "original_id": chain_end,
            "normalized_id": disease_id,
            "normalized_label": disease_label or None,
            "normalization_quality": "asserted_exact",
            "tool": "medic-normalizer/1",
        }

    # I-12: a chain ending in a CURIE ends with a NORMALIZATION step, identity or not. India
    # records carry no normalization object at all, so synthesise the identity hop rather than
    # leaving a chain that stops at a grounding while the record asserts a canonical id.
    if normalization is None and merge_normalization is None:
        chain_id = (grounding or {}).get("grounded_id") or disease_id
        if chain_id:
            normalization = {
                "original_id": chain_id,
                "normalized_id": chain_id,
                "normalized_label": disease_label or None,
                "normalization_quality": "identity",
            }

    mention = build_mention(
        original_literal=raw,
        entity_type="disease",
        mention_id=mention_id,
        source=record.get("source"),
        spans=spans,
        extraction=extraction,
        grounding=grounding,
        normalization=normalization,
        applied_rules=applied_rules,
        grounding_predicate=predicate_id,
        grounding_flags=gflags,
        merge_normalization=merge_normalization,
        resolved_id=disease_id or None,
        resolved_label=disease_label or None,
    )

    if span_index is not None:
        # Record which span the claim was read from, and which spans its negation check ran
        # over — so a reader can verify the LIMITATION_STATEMENT was excluded (§4.3).
        assertion["span_index"] = span_index
        assertion["negation_scope"] = readable

    # The object (disease) confidence is only known once its mention is built; fold it in and
    # rebuild the breakdown so all three components and the product stay consistent (I-11).
    conf = assertion.get("confidence") or {}
    object_conf = _resolution_confidence(mention)
    assertion["confidence"] = build_confidence_breakdown(
        conf.get("subject"),
        object_conf if object_conf is not None else conf.get("object"),
        conf.get("relationship"),
        basis=conf.get("basis", "MEASURED"),
    )
    return mention, assertion


def _build_source_assertions(
    record: dict,
    drug_approval_dates: dict[str, dict[str, str]] | None = None,
    fda_url_lookup: dict[str, str] | None = None,
    fda_artifacts: dict[str, list[dict]] | None = None,
    store: GroundingStoreView | None = None,
    drug_store: GroundingStoreView | None = None,
    warrants: dict[str, str] | None = None,
    mondo_labels: dict[str, str] | None = None,
) -> dict:
    """One source document's assertion about a pair — internally single-source (I-10).

    Both Mentions are built from THIS record's own evidence, so the drug trail, the disease
    trail, the spans and the evidence all come from the same document. The merge-elected
    identity from drug_list.yaml is gone (D4).
    """
    drug_id = _get_drug_id(record)
    disease_id = _get_disease_id(record)
    drug_label = (record.get("final_normalized_drug_label", "")
                  or record.get("normalized_drug_label", ""))
    # Canonicalised here too, so the mention's resolved_label and terminal NormalizationStep
    # agree with the pair rather than preserving the grounder's incidental label.
    disease_label = _canonical_disease_label(
        disease_id,
        (record.get("final_normalized_disease_label", "")
         or record.get("normalized_disease_label", "")),
        mondo_labels,
        store.label_for(disease_id) if store is not None else None)

    # An assertion describes ONE attesting document (D2), so it is built from this record's
    # own evidence and nothing else. Drug-level marketing artifacts (Orange Book NDAs, Purple
    # Book BLAs, GRLS, CDE) are deliberately NOT folded in here: they attest that the *drug*
    # is registered, not that it is indicated for this disease, and they already live on
    # `Drug.approvals` in drug_list.yaml with their deep links. Injecting them produced
    # 3,799 fabricated assertions — relabelled copies of the real one, claiming a disease
    # mention_source of ORANGEBOOK for a disease Orange Book never saw.
    evidence = _dedup_evidence_prefer_primary(record.get("evidence", []) or [])
    ev0 = evidence[0] if evidence and isinstance(evidence[0], dict) else {}

    reg_rows = _build_regulatory_status_from_evidence(
        evidence, drug_id, drug_approval_dates, fda_url_lookup, None)

    out: dict = {
        "source": (record.get("source") or "").upper(),
        "document": _document_for(record, ev0),
        "drug": _build_drug_mention(record, ev0, drug_id, drug_label, store=drug_store),
    }
    if ev0.get("jurisdiction"):
        out["jurisdiction"] = ev0["jurisdiction"]
    if ev0:
        out["evidence"] = ev0
    if reg_rows:
        out["regulatory_status"] = reg_rows[0]
    if record.get("hyperrelations"):
        out["hyperrelations"] = record["hyperrelations"]

    # _build_disease_provenance reads relationship_type, evidence and drug off a dict shaped
    # like the old association; hand it a view rather than duplicating its logic.
    view = {"relationship_type": record.get("relationship_type", ""),
            "evidence": evidence, "drug": out["drug"]}
    disease_mention, assertion = _build_disease_provenance(
        record, view, disease_id, disease_label, store=store, warrants=warrants)
    if disease_mention:
        out["spans"] = disease_mention.pop("source_spans", []) or []
        out["disease"] = disease_mention
    if assertion:
        out["assertion"] = assertion
    # A list of one: the caller loops, and keeping the shape leaves room for a source that
    # genuinely attests the same claim in more than one document.
    return [out]


def _append_assertion(pair: dict, assertion: dict) -> None:
    """Add one source document's assertion to a pair, ignoring an exact document repeat."""
    key = (assertion.get("source", ""), assertion.get("document", ""))
    if any((a.get("source", ""), a.get("document", "")) == key
           for a in pair.setdefault("assertions", [])):
        return
    pair["assertions"].append(assertion)


def _finalize_pair(pair: dict) -> None:
    """Order the assertions and recompute the pair-level aggregates.

    Ordering is by (source, document) rather than insertion order, so a rerun is byte-identical
    regardless of the order the kb/ files happen to be walked in.
    """
    pair.setdefault("assertions", []).sort(
        key=lambda a: (a.get("source", ""), a.get("document", "")))
    # Grouped by source, not flat: noisy-OR assumes its inputs fail independently, and the
    # 21 DailyMed relabellings of one hydrochlorothiazide label do not. See
    # `medic.confidence.corroboration` for why each source contributes once.
    by_source: dict[str, list[float]] = {}
    for a in pair["assertions"]:
        conf = ((a.get("assertion") or {}).get("confidence") or {}).get("overall")
        if conf is not None:
            by_source.setdefault(a.get("source", ""), []).append(float(conf))
    pair["confidence"] = {
        "overall": round(corroboration(by_source), 6),
        "method": "NOISY_OR",
        "n_assertions": len(pair["assertions"]),
        # How many *independent* sources the aggregate actually rests on. Without it a
        # reader cannot tell 0.91 from two regulators apart from 0.91 from one regulator
        # with a rich label — which is the whole distinction the aggregate now makes.
        "n_sources": len(by_source),
    }


def main():
    logging.basicConfig(level=logging.INFO)
    merge_on_label()


if __name__ == "__main__":
    main()

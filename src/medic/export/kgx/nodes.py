"""KGX node builders.

Two product-backed node types — drugs and diseases — plus stub nodes for edge endpoints no
product describes. Stubs exist because a KGX graph with a dangling edge is invalid, and MeDIC
genuinely produces such endpoints: research associations carry ``UNII:`` drug ids and
``ORPHA:``/``UMLS:`` disease ids that Stage-2 normalization could not map to CHEBI/MONDO.
Making them visible is the point — the stub count is a coverage metric, not a defect to hide.

Every builder omits empty values rather than writing ``""`` or ``[]``: an absent fact should
be an absent key, so a consumer never has to distinguish "no ATC code" from "ATC code is the
empty string".
"""

from __future__ import annotations

from medic import product_view as pv
from medic.export.kgx import biolink as bl
from medic.reliability import (
    StatementReviewStore,
    default_review_store,
    score_reliability,
)


def _clean(node: dict) -> dict:
    """Drop keys whose value carries no information, and sort for deterministic output."""
    return {k: v for k, v in sorted(node.items()) if v not in ("", [], {}, None)}


def _uniq(values) -> list[str]:
    return sorted({v.strip() for v in values if isinstance(v, str) and v.strip()})


def _step(mention: dict, category: str) -> dict:
    """The first step of a given category in a mention's resolution pipeline."""
    pipeline = ((mention.get("resolution") or {}).get("pipeline")) or []
    for step in pipeline:
        if isinstance(step, dict) and step.get("category") == category:
            return step
    return {}


# ---------------------------------------------------------------------------
# Drugs
# ---------------------------------------------------------------------------
def drug_node(
    drug: dict, review: StatementReviewStore | None = None
) -> dict | None:
    """A drug node, or ``None`` when the drug never resolved to a canonical id.

    An unresolved drug has no identifier to be a node *of*. It is not silently lost — it
    stays in ``products/drug_list.yaml`` and in the grounding reports (invariant I-4).
    """
    drug_id = pv.drug_id(drug)
    if not drug_id:
        return None

    identity = drug.get("identity") or {}
    resolution = identity.get("resolution") or {}
    atc = drug.get("atc") or {}

    description = " · ".join(
        v for v in (drug.get("drug_class"), drug.get("therapeutic_area"),
                    drug.get("drug_function"), drug.get("drug_target"))
        if isinstance(v, str) and v.strip()
    )

    node = {
        "id": drug_id,
        "name": pv.drug_label(drug),
        "category": bl.DRUG_CATEGORIES,
        "provided_by": [bl.AGGREGATOR],
        "description": description,
        "synonym": _uniq([
            *(drug.get("source_ingredients") or []),
            *(drug.get("synonyms") or []),
            identity.get("original_literal") or "",
        ]),
        # `alternate_ids` are ids MeDIC's own resolution established; `source_xrefs` are
        # cross-references copied from an enrichment source (PHAROS, RxNorm). Both are
        # `xref` here — Biolink's loose cross-reference — but only the first set is a
        # SSSOM exactMatch (see Drug.source_xrefs in the schema).
        "xref": _uniq([
            *(drug.get("alternate_ids") or []),
            *(drug.get("source_xrefs") or []),
            drug.get("drugbank_id") or "",
        ]),

        # chemistry
        "medic_smiles": drug.get("smiles") or "",
        "medic_atc_codes": _uniq(atc.get("codes") or drug.get("atc_codes") or []),
        "medic_atc_main": atc.get("main") or drug.get("atc_main") or "",
        "medic_atc_level1": atc.get("level1") or drug.get("atc_level1") or "",
        "medic_atc_level2": atc.get("level2") or drug.get("atc_level2") or "",
        "medic_atc_level3": atc.get("level3") or drug.get("atc_level3") or "",
        "medic_atc_level4": atc.get("level4") or drug.get("atc_level4") or "",
        "medic_atc_level5": atc.get("level5") or drug.get("atc_level5") or "",
        "medic_features": list(drug.get("features") or []),

        # combinations
        "medic_is_combination_therapy": drug.get("is_combination_therapy") or "",
        "medic_combination_ingredients": _uniq(
            drug.get("combination_therapy_ingredients") or []),
        "medic_combination_ingredient_ids": _uniq(
            drug.get("combination_therapy_ingredients_curies") or []),

        # regulatory summary (approvals are node properties, not edges — spec §5.6)
        "medic_approved_authorities": sorted(pv.approved_authorities(drug)),
        "medic_approved_jurisdictions": sorted(pv.approved_jurisdictions(drug)),
        "medic_earliest_approval_date": pv.earliest_approval_date(drug),
        "medic_marketing_status_usa": pv.marketing_status_usa(drug),
        # Split by issuing authority, not flattened (#52 / LICENSING.md). `medic_application_
        # numbers` is FDA application/BLA numbers — US government public domain. Registration
        # numbers from registers that grant no open licence (Russian GRLS) go in their own
        # field, tagged with the authority, so a consumer can tell the provenances apart and
        # so the restricted half can be dropped without touching FDA data.
        "medic_application_numbers": _uniq(
            pv.application_numbers_by_authority(drug).get("FDA", [])),
        "medic_registration_numbers": _uniq(
            f"{authority}:{number}"
            for authority, numbers in sorted(
                pv.application_numbers_by_authority(drug).items())
            if authority != "FDA"
            for number in numbers
        ),
        "medic_regulatory_document_urls": _uniq(
            a.get("regulatory_document_url") or "" for a in pv.approvals(drug)),

        # provenance join keys into the shipped mappings/ decision stores (spec §2.3)
        "medic_mention_id": identity.get("id") or "",
        "medic_original_literal": identity.get("original_literal") or "",
        "medic_mention_source": identity.get("mention_source") or "",
        "medic_grounding_quality": _step(identity, "GROUNDING").get("quality") or "",
        "medic_resolution_confidence": resolution.get("confidence"),
        "medic_reliability": score_reliability(
            drug, review_status=(review or default_review_store()).status(drug)
        ).value,
    }
    return _clean(node)


# ---------------------------------------------------------------------------
# Diseases
# ---------------------------------------------------------------------------
#: The disease filter flags carried onto nodes. Only ``True`` values are emitted — writing 26
#: booleans onto 23,148 nodes would add megabytes of ``false`` for no information.
DISEASE_FLAG_PREFIX = "f_"


def disease_node(disease: dict) -> dict | None:
    disease_id = disease.get("category_class") or ""
    if not disease_id:
        return None

    node = {
        "id": disease_id,
        "name": disease.get("label") or "",
        "category": bl.DISEASE_CATEGORIES,
        "provided_by": [bl.AGGREGATOR],
        "description": disease.get("definition") or "",
        "synonym": _uniq(disease.get("synonyms") or []),
        "xref": _uniq(disease.get("crossreferences") or []),
        "medic_subsets": _uniq(disease.get("subsets") or []),
    }
    for key, value in disease.items():
        if key.startswith(DISEASE_FLAG_PREFIX) and value is True:
            node[f"{bl.EXTENSION_PREFIX}{key}"] = True
    return _clean(node)


# ---------------------------------------------------------------------------
# Stubs and assembly
# ---------------------------------------------------------------------------
def stub_node(curie: str, name: str = "") -> dict:
    """A minimal node for an edge endpoint no product describes (spec §4.3)."""
    return _clean({
        "id": curie,
        "name": name,
        "category": [bl.category_for_prefix(curie)],
        "provided_by": [bl.AGGREGATOR],
        "medic_stub": True,
    })


def build_nodes(
    drugs: list[dict],
    diseases: list[dict],
    referenced: dict[str, str] | None = None,
    review: StatementReviewStore | None = None,
) -> list[dict]:
    """All nodes, deduplicated and sorted by id.

    ``referenced`` maps every id an edge points at to the best label the association knew, so
    endpoints missing from the products become stubs and the graph stays referentially
    closed.
    """
    review = review or default_review_store()
    by_id: dict[str, dict] = {}

    for drug in drugs:
        node = drug_node(drug, review)
        if node:
            by_id.setdefault(node["id"], node)
    for disease in diseases:
        node = disease_node(disease)
        if node:
            by_id.setdefault(node["id"], node)

    for curie, label in (referenced or {}).items():
        if curie and curie not in by_id:
            by_id[curie] = stub_node(curie, label)

    return [by_id[key] for key in sorted(by_id)]

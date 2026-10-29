"""KGX node builders.

Two product-backed node types — drugs and diseases — plus stub nodes for edge endpoints no
product describes. Stubs exist because a KGX graph with a dangling edge is invalid, and MeDIC
genuinely produces such endpoints: research associations carry ``UNII:`` drug ids and
``ORPHA:``/``UMLS:`` disease ids that Stage-2 normalization could not map to CHEBI/MONDO.
Making them visible is the point — the stub count is a coverage metric, not a defect to hide.

Every builder omits empty values rather than writing ``""`` or ``[]``: an absent fact should
be an absent key, so a consumer never has to distinguish "no ATC code" from "ATC code is the
empty string".

Every node is an instance of its Biolink class: the un-namespaced properties go through the
pydantic class for the node's category (see :func:`_build`), which fixes ``category`` and
rejects any property the class does not declare. The ``medic_`` layer is attached after.
"""

from __future__ import annotations

import re

from pydantic import ValidationError

from medic import product_view as pv
from medic.export.kgx import biolink as bl
from medic.grounding_store_view import GroundingStoreView
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


def _build(category: str, record: dict) -> dict:
    """Construct one node: the Biolink core through its class, then the ``medic_`` layer.

    The class stamps ``category`` — one per node, its own — and is strict about everything
    else, so a property Biolink 4.4.6 does not define on that class cannot be written. A
    ``ValidationError`` is a bug in a builder, not bad data, and is left to propagate.
    """
    record = {k: v for k, v in record.items() if v not in ("", [], {}, None)}
    extension = {k: v for k, v in record.items() if k.startswith(bl.EXTENSION_PREFIX)}
    core = {k: v for k, v in record.items() if k not in extension}
    unknown = {k for k in extension if k not in bl.EXTENSION_SLOTS
               and not k.startswith(f"{bl.EXTENSION_PREFIX}{DISEASE_FLAG_PREFIX}")}
    if unknown:
        raise ValueError(f"extension properties not declared in EXTENSION_SLOTS: {unknown}")

    cls = bl.model_class(category)
    try:
        built = cls(**core).model_dump(mode="json", exclude_none=True)
    except ValidationError as exc:
        raise ValueError(f"node {core.get('id')} is not a valid {cls.__name__}: {exc}") from exc
    return _clean({**built, **extension})


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
def unii_by_drug(*products: list[dict]) -> dict[str, str]:
    """``{drug_id: UNII}`` from the indication and contraindication evidence rows (#94).

    The UNII is asserted by the SPL and recorded at ingest next to `original_drug_label`;
    this just collects it per grounded drug id so the node builder can publish it as an
    xref. Read through `product_view`, so it follows the association shape rather than a
    flat field. Takes already-loaded association *lists*, as `_load_all` hands them over.

    First value wins. A drug whose rows disagree would mean two moieties grounding to one
    id, which is a grounding question and not something to paper over here by picking a
    winner — the disagreement stays visible in the evidence rows either way.
    """
    out: dict[str, str] = {}
    for product in products:
        for assoc in product or []:
            drug_id = pv.assoc_drug_id(assoc)
            if not drug_id or drug_id in out:
                continue
            for ev in pv.assoc_evidence(assoc):
                unii = (ev.get("original_drug_unii") or "").strip()
                if unii:
                    out[drug_id] = unii
                    break
    return out


def drug_node(
    drug: dict,
    review: StatementReviewStore | None = None,
    unii_by_drug: dict[str, str] | None = None,
) -> dict | None:
    """A drug node, or ``None`` when the drug never resolved to a canonical id.

    An unresolved drug has no identifier to be a node *of*. It is not silently lost — it
    stays in ``products/drug_list.yaml`` and in the grounding reports (invariant I-4).
    """
    drug_id = pv.drug_id(drug)
    if not drug_id:
        return None

    identity = drug.get("identity") or {}
    atc = drug.get("atc") or {}
    unii = (unii_by_drug or {}).get(drug_id, "")
    marketing = pv.marketing_status_usa(drug) or ""

    description = " · ".join(
        v for v in (drug.get("drug_class"), drug.get("therapeutic_area"),
                    drug.get("drug_function"), drug.get("drug_target"))
        if isinstance(v, str) and v.strip()
    )

    node = {
        "id": drug_id,
        "name": pv.drug_label(drug),
        "provided_by": [bl.AGGREGATOR],
        "description": description,
        "synonym": _uniq([
            *(drug.get("source_ingredients") or []),
            *(drug.get("synonyms") or []),
            identity.get("original_literal") or "",
        ]),
        # Three strengths of cross-reference, all of them Biolink `xref` (its loose
        # cross-reference) and only the first a SSSOM exactMatch:
        #
        # * `alternate_ids` — ids MeDIC's own resolution established.
        # * `source_xrefs` — copied from an enrichment source's record (PHAROS synonyms
        #   matched by *label*, the RxNorm ingredient RxCUI by *approximate* match), so
        #   published as closeMatch; see Drug.source_xrefs in the schema.
        # * the SPL-asserted UNII, where an indication row carried one (#94). 717 drug ids
        #   (3,448 assertions) are unknown to the SRI Node Normalizer because ChEBI and DRON
        #   mint ids newer than its snapshot, so a consumer joining on the primary id drops
        #   those edges — and they are the biologics. A UNII is stable and lets the join
        #   succeed whatever the primary id turns out to be. It belongs to the
        #   `source_xrefs` class, not `alternate_ids`: the label asserts the UNII of its
        #   active moiety, but that the moiety is *this* ChEBI term is MeDIC's grounding
        #   decision, so an exactMatch row would publish an identity we did not verify. It
        #   reaches `xref` here but not the SSSOM export, which reads the drug product only.
        "xref": _uniq([
            *(drug.get("alternate_ids") or []),
            *(drug.get("source_xrefs") or []),
            drug.get("drugbank_id") or "",
            f"UNII:{unii}" if unii else "",
        ]),
        # US marketing status, where Biolink's enum can say it (RX/OTC; DISCN cannot).
        "available_from": [bl.AVAILABLE_FROM[marketing]]
        if marketing in bl.AVAILABLE_FROM else [],

        # chemistry — the ATC code is the leaf; every coarser level is its prefix
        "medic_smiles": drug.get("smiles") or "",
        "medic_atc_codes": _uniq(atc.get("codes") or drug.get("atc_codes") or []),
        "medic_features": list(drug.get("features") or []),

        # combinations
        "medic_is_combination_therapy": drug.get("is_combination_therapy") or "",
        "medic_combination_ingredient_ids": _uniq(
            drug.get("combination_therapy_ingredients_curies") or []),

        # regulatory summary (approvals are node properties, not edges — spec §5.6)
        "medic_approved_authorities": sorted(pv.approved_authorities(drug)),
        "medic_approved_jurisdictions": sorted(pv.approved_jurisdictions(drug)),
        "medic_earliest_approval_date": pv.earliest_approval_date(drug),
        "medic_marketing_status_usa": marketing,
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

        # provenance join key into the shipped mappings/ decision stores (spec §2.3). The
        # grounding quality, source and confidence it used to carry beside it are the
        # store's own columns, reachable through this key.
        "medic_mention_id": identity.get("id") or "",
        "medic_reliability": score_reliability(
            drug, review_status=(review or default_review_store()).status(drug)
        ).value,
    }
    return _build(bl.DRUG_CATEGORY, node)


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
        "name": deinvert_thesaurus_name(disease.get("label") or ""),
        "provided_by": [bl.AGGREGATOR],
        "description": disease.get("definition") or "",
        "synonym": _uniq(disease.get("synonyms") or []),
        "xref": _uniq(disease.get("crossreferences") or []),
        "subsets": _uniq(disease.get("subsets") or []),
    }
    for key, value in disease.items():
        if key.startswith(DISEASE_FLAG_PREFIX) and value is True:
            node[f"{bl.EXTENSION_PREFIX}{key}"] = True
    return _build(bl.DISEASE_CATEGORY, node)


# ---------------------------------------------------------------------------
# Stubs and assembly
# ---------------------------------------------------------------------------
#: An inverted thesaurus heading: "Diseases, Liver", "Pneumonia, Hospital Acquired". MeSH
#: stores entry terms head-first for alphabetisation, and the UMLS loader turns the stored
#: form into the node's display name. Recognised rather than guessed at: the part after the
#: comma must start with a capital, which distinguishes a heading from a list ("asthma,
#: rhinitis and nasal polyps") or a prose qualifier ("anemia, due to chronic kidney
#: disease").
_THESAURUS_INVERSION = re.compile(r"^([A-Z][^,]*?), ([A-Z][A-Za-z0-9 .'-]*)$")


def deinvert_thesaurus_name(name: str) -> str:
    """Read an inverted thesaurus heading the way it is meant to be read (#96).

    66 disease nodes ship names like "Diseases, Liver" (24 edges), "Headaches, Tension"
    (22) and "Pains, Cancer" (7). Swapping on the comma recovers the natural form. Anything
    that is not an inversion is returned unchanged, so a list or a qualifier phrase is safe.
    """
    match = _THESAURUS_INVERSION.match((name or "").strip())
    if not match:
        return name
    head, modifier = match.group(1), match.group(2)
    return f"{modifier} {head}"


def stub_node(curie: str, name: str = "") -> dict:
    """A minimal node for an edge endpoint no product describes (spec §4.3).

    ``name`` is the best label the association knew — which for a CUI whose preferred atoms
    are all in label-restricted vocabularies is the source literal, the only name we may
    ship. 91 stubs used by 5,086 edges had none at all, so the node went out as a bare CUI
    and the audit could not judge the edges at all (#96).
    """
    return _build(bl.category_for_prefix(curie), {
        "id": curie,
        "name": deinvert_thesaurus_name(name),
        "provided_by": [bl.AGGREGATOR],
        "medic_stub": True,
    })


#: Where the disease grounding decisions live. Same path the merge reads.
DISEASE_GROUNDING_STORE = "mappings/disease_grounding.sssom.tsv"


def _default_disease_store() -> GroundingStoreView | None:
    """The committed disease grounding store, or ``None`` when it is absent.

    ``None`` rather than raising, so a unit test that builds nodes from literals in a temp
    directory still works. The artefact-level check
    (`test_restricted_labels_not_published`) is what guarantees the real export consulted it.
    """
    from pathlib import Path

    if not Path(DISEASE_GROUNDING_STORE).exists():
        return None
    return GroundingStoreView(DISEASE_GROUNDING_STORE, "disease").load()


def build_nodes(
    drugs: list[dict],
    diseases: list[dict],
    referenced: dict[str, str] | None = None,
    review: StatementReviewStore | None = None,
    disease_store: GroundingStoreView | None = None,
    unii_by_drug: dict[str, str] | None = None,
) -> list[dict]:
    """All nodes, deduplicated and sorted by id.

    ``referenced`` maps every id an edge points at to the best label the association knew, so
    endpoints missing from the products become stubs and the graph stays referentially
    closed.

    ``disease_store`` is consulted before a stub is named. #96's source-literal fallback is
    right for a concept that simply has no label, and wrong for one the grounding store
    *withholds* a label for: there, every atom naming the concept comes from a vocabulary
    I-14 rule 2 forbids republishing, and the extraction canonicalises toward that same
    vocabulary term, so the "source literal" is usually the restricted string itself. It
    named 102 such concepts — `UMLS:C0024303` "lymphocytic lymphoma", `UMLS:C0234933`
    "erythema multiforme, severe" — which is the leak `test_restricted_labels_not_published`
    exists for, arriving by a second route.

    `label_for` distinguishes the two: ``""`` is a decision to publish no name, ``None`` is
    no opinion. Only the first blocks the fallback.
    """
    review = review or default_review_store()
    if disease_store is None:
        disease_store = _default_disease_store()
    withheld = {
        curie for curie in (referenced or {})
        if disease_store is not None and disease_store.label_for(curie) == ""
    }
    by_id: dict[str, dict] = {}

    for drug in drugs:
        node = drug_node(drug, review, unii_by_drug)
        if node:
            by_id.setdefault(node["id"], node)
    for disease in diseases:
        node = disease_node(disease)
        if node:
            by_id.setdefault(node["id"], node)

    for curie, label in (referenced or {}).items():
        if curie and curie not in by_id:
            by_id[curie] = stub_node(curie, "" if curie in withheld else label)

    return [by_id[key] for key in sorted(by_id)]

"""Shared grounding logic for drug and disease source ingests.

This is the centralized plumbing for MeDIC entity resolution. The default and
intended path is the **deterministic two-stage lexical grounder**:

  Stage 1 — grounding (source string -> initial id) via the lexical SQLite index
            (``medic.grounding.lexical_backend.LexicalCascadeGrounding``), with every
            decision (including failures) written to the SSSOM decision store under
            ``mappings/{drug,disease}_grounding.sssom.tsv``.
  Stage 2 — normalization (initial id -> canonical MONDO/CHEBI) via the target
            namespace's own asserted xrefs/replaced_by, written to
            ``mappings/{disease,drug}_normalization.sssom.tsv``.

Each record receives the structured ``grounding`` and ``normalization`` objects
(see ``medic/schema/grounding.yaml``) **and** a backwards-compatible set of flat
fields (``normalized_id``, ``normalized_label``, ``grounding_confidence``,
``grounding_service``, ``grounding_status``, ``alternate_ids``) that the merge and
export stages still read. The flat fields are a compatibility shim mapped from the
new result (see ``_apply_result``); do not remove them until merge/export migrate.

Legacy non-lexical backends (``cascade``/``nameres``/``oak``/``ols``/``gilda``) still
work through the fallback path in ``_ground_one_legacy`` and continue to populate
only the flat fields.
"""

from __future__ import annotations

import logging
import os

from medic.grounding.base import GroundingService
from medic.grounding.cache import GroundingCache
from medic.grounding.lexical_backend import LexicalCascadeGrounding
from medic.normalization.mapping_index import load_mapping_index
from medic.normalization.normalizer import Normalizer
from medic.normalization.store import NormalizationMappingStore

logger = logging.getLogger(__name__)

# Stage-2 normalization index locations (built by ``just build-normalization-index``).
_NORM_INDEX = {
    "diseases": "cache/normalization/diseases.db",
    "drugs": "cache/normalization/drugs.db",
}
_NORM_STORE = {
    "diseases": "mappings/disease_normalization.sssom.tsv",
    "drugs": "mappings/drug_normalization.sssom.tsv",
}
_TARGET_PREFIX = {"diseases": "MONDO", "drugs": "CHEBI"}
_NORM_TOOL = "medic-normalizer/1"

# Cache one Normalizer per entity type per process (each owns a store handle).
_NORMALIZERS: dict[str, Normalizer] = {}


def _get_normalizer(entity_type: str) -> Normalizer:
    """Build (once) the Stage-2 normalizer for an entity type.

    Uses the target namespace's asserted mappings if the compiled index exists;
    otherwise runs a ``none``-only normalizer (id unchanged) so grounding still
    works. Drugs ground natively to CHEBI, so their normalization is a near-identity
    pass and a missing drug index is expected/benign.
    """
    nz = _NORMALIZERS.get(entity_type)
    if nz is not None:
        return nz
    index_path = _NORM_INDEX[entity_type]
    mapping_index: dict = {}
    if os.path.exists(index_path):
        mapping_index = load_mapping_index(index_path)
    else:
        logger.info(
            "No normalization index at %s; %s normalization is identity-only "
            "(run `just build-normalization-index`).",
            index_path,
            entity_type,
        )
    store = NormalizationMappingStore(_NORM_STORE[entity_type], entity_type)
    store.load()
    nz = Normalizer(entity_type, mapping_index, store, _TARGET_PREFIX[entity_type], _NORM_TOOL)
    _NORMALIZERS[entity_type] = nz
    return nz


def flush_normalizers() -> None:
    """Persist every open normalization store to its SSSOM TSV (call once per batch)."""
    for nz in _NORMALIZERS.values():
        nz.store.save()


# Backwards-compatible internal alias.
_flush_normalizers = flush_normalizers


def _effective(grounded_id, grounded_label, norm_decision):
    """Return (effective_id, effective_label) — normalized when a mapping applies."""
    if norm_decision is not None and norm_decision.normalization_quality != "none":
        return norm_decision.object_id, (norm_decision.object_label or grounded_label)
    return grounded_id, grounded_label


class _Resolved:
    """The outcome of a single two-stage resolve (grounding + normalization)."""

    __slots__ = ("status", "effective_id", "effective_label", "confidence",
                 "alternate_ids", "grounding_obj", "normalization_obj")

    def __init__(self, status, effective_id, effective_label, confidence,
                 alternate_ids, grounding_obj, normalization_obj):
        self.status = status
        self.effective_id = effective_id
        self.effective_label = effective_label
        self.confidence = confidence
        self.alternate_ids = alternate_ids
        self.grounding_obj = grounding_obj
        self.normalization_obj = normalization_obj


def _resolve(entity_type: str, raw: str, backend: LexicalCascadeGrounding,
             mention_id: str | None = None) -> _Resolved:
    """Ground + Stage-2 normalize ``raw``; return the effective id and structured objects.

    Records every grounding decision into the backend's SSSOM store as a side effect of
    the ``ground_*`` call (stamped with the mention's ``mention_id``). Does not mutate
    any record.
    """
    ground = backend.ground_disease if entity_type == "diseases" else backend.ground_drug
    results = ground(raw, mention_id=mention_id)  # populates the grounding SSSOM store
    decisions = backend.last_decision(entity_type, raw)
    resolved = [d for d in decisions if d.object_id is not None]
    primary = resolved[0] if resolved else (decisions[0] if decisions else None)

    grounded_id = results[0].id if results else None
    grounded_label = results[0].label if results else None
    confidence = results[0].score if results else 0.0

    grounding_obj = {
        "subject_id": mention_id,
        "original_string": raw,
        "grounded_id": grounded_id,
        "grounded_label": grounded_label,
        "grounding_quality": primary.grounding_quality if primary else "unresolved",
        "confidence": confidence,
    }
    component_ids = [d.object_id for d in resolved]
    if len(resolved) > 1:  # a combination literal legitimately grounds to several ids
        grounding_obj["components"] = component_ids

    if not grounded_id:
        return _Resolved("unresolved", "", "", 0.0, [], grounding_obj, None)

    norm_decision = _get_normalizer(entity_type).normalize(grounded_id, grounded_label)
    normalization_obj = {
        "original_id": grounded_id,
        "normalized_id": norm_decision.object_id,
        "normalized_label": norm_decision.object_label,
        "normalization_quality": norm_decision.normalization_quality,
        "tool": norm_decision.tool,
    }
    eff_id, eff_label = _effective(grounded_id, grounded_label, norm_decision)
    # `alternate_ids` means "another id for THIS entity"; the SSSOM export turns each one into
    # a `skos:exactMatch`, the strongest identity claim SSSOM has. The other components of a
    # combination literal are different molecules, not other names for this one — they belong
    # on `grounding_obj["components"]` and nowhere else. Folding them in here made the export
    # assert `butalbital skos:exactMatch paracetamol` (from "ACETAMINOPHEN; BUTALBITAL;
    # CAFFEINE") across 397 component pairs, and put them on 289 KGX drug nodes as xrefs.
    #
    # The pre-normalization id is a real alternate: same entity, the id Stage 1 rested on
    # before Stage 2 canonicalised it.
    alt: list[str] = []
    if grounded_id != eff_id:
        alt.append(grounded_id)
    return _Resolved("accepted", eff_id, eff_label or "", confidence, alt,
                     grounding_obj, normalization_obj)


def _apply_result(
    record: dict,
    entity_type: str,
    raw: str,
    backend: LexicalCascadeGrounding,
) -> str:
    """Resolve ``raw`` and write structured objects + flat compat fields onto ``record``.

    Used for per-source ingest records (kb/drugs/*, kb/indications/*), which are NOT
    schema-validated and therefore carry both the structured ``grounding``/
    ``normalization`` objects (spec §4) and the flat fields the merge/export stages read.
    Returns the flat ``grounding_status`` so callers can tally counts.
    """
    from medic.mention import MENTION_ID_KEY, assign_mention
    mention_id = record.get(MENTION_ID_KEY) or assign_mention(record, entity_type)
    r = _resolve(entity_type, raw, backend, mention_id=mention_id)
    record["grounding"] = r.grounding_obj
    if r.normalization_obj is not None:
        record["normalization"] = r.normalization_obj
    record["normalized_id"] = r.effective_id
    record["normalized_label"] = r.effective_label
    record["grounding_confidence"] = round(r.confidence, 4)
    record["grounding_service"] = "lexical"
    record["grounding_status"] = r.status
    record["alternate_ids"] = r.alternate_ids
    return r.status


def resolve_disease_onto_record(
    record: dict,
    disease_name: str,
    backend: GroundingService,
    id_key: str = "final_normalized_disease_id",
    label_key: str = "final_normalized_disease_label",
) -> str:
    """Ground a raw disease name AND attach the structured grounding objects onto a record.

    This is the disease-side analogue of the drug ``_apply_result`` path: it runs the
    deterministic two-stage resolve (Stage-1 lexical grounding + Stage-2 MONDO
    normalization) via :func:`_resolve`, writes the effective id/label to ``id_key`` /
    ``label_key``, and attaches the structured ``disease_grounding`` (and, when present,
    ``disease_normalization``) objects that ``on_label_merge._carry_disease_grounding``
    funnels onto the product association. The full decision is also logged to the SSSOM
    grounding/normalization stores as a side effect of :func:`_resolve`.

    Non-lexical backends (``cascade``/``nameres``/``oak``/``ols``/``gilda``) don't build
    the structured objects, so this falls back to the legacy ``ground_disease_best``
    behaviour: it writes only the id/label, attaching no structured objects.

    Returns the effective disease id (empty string when nothing grounded), so callers can
    skip records whose disease didn't resolve — matching the old ``if not result.id`` gate.
    """
    if isinstance(backend, LexicalCascadeGrounding):
        from medic.mention import mint_mention_id
        r = _resolve("diseases", disease_name, backend,
                     mention_id=mint_mention_id(disease_name, "diseases"))
        if r.status == "unresolved" or not r.effective_id:
            return ""
        record[id_key] = r.effective_id
        record[label_key] = r.effective_label or disease_name
        record["disease_grounding"] = r.grounding_obj
        if r.normalization_obj is not None:
            record["disease_normalization"] = r.normalization_obj
        return r.effective_id

    # Legacy fallback: flat id/label only, no structured objects.
    result = backend.ground_disease_best(disease_name)
    if not result or not result.id:
        return ""
    record[id_key] = result.id
    record[label_key] = result.label
    return result.id


def flush_disease_grounding(backend: GroundingService) -> None:
    """Persist the grounding + normalization SSSOM stores after a disease-grounding batch.

    Ingesters that resolve disease names via :func:`resolve_disease_onto_record` accumulate
    Stage-1/Stage-2 decisions in memory; call this once per batch so the SSSOM decision logs
    are written a single time (mirrors the flush at the end of :func:`ground_disease_records`).
    A no-op for non-lexical backends, which don't use the SSSOM stores.
    """
    if isinstance(backend, LexicalCascadeGrounding):
        backend.flush()
        flush_normalizers()


def normalize_existing_id(
    record: dict,
    entity_type: str,
    id_key: str,
    label_key: str | None = None,
    attach_object: bool = True,
    object_key: str = "normalization",
) -> None:
    """Stage-2 normalize an id a pre-grounded source already carries (no re-grounding).

    For sources that arrive WITH ids (everycure, cureid): map the existing id toward the
    canonical target namespace, updating ``id_key`` in place when the target namespace
    asserts a mapping (e.g. an obsolete-term replacement); otherwise the id is unchanged.

    ``attach_object`` controls whether a structured ``normalization`` object is written
    onto the record. Set it to ``False`` for schema-validated records whose class has no
    ``normalization`` slot (e.g. ``IndicationAssociation``) — the SSSOM decision store
    still records the decision, only the record-level object is suppressed.

    Always records the decision into the normalization SSSOM store; does not persist it —
    call :func:`flush_normalizers` once after a batch so the store is written a single time.
    """
    grounded_id = (record.get(id_key) or "").strip()
    if not grounded_id:
        return
    grounded_label = record.get(label_key) if label_key else record.get("normalized_label")
    normalizer = _get_normalizer(entity_type)
    nd = normalizer.normalize(grounded_id, grounded_label)
    if attach_object:
        record[object_key] = {
            "original_id": grounded_id,
            "normalized_id": nd.object_id,
            "normalized_label": nd.object_label,
            "normalization_quality": nd.normalization_quality,
            "tool": nd.tool,
        }
    if nd.normalization_quality != "none":
        record[id_key] = nd.object_id
        if attach_object and nd.object_label:
            record[label_key or "normalized_label"] = nd.object_label


# ---------------------------------------------------------------------------
# Legacy fallback (non-lexical backends): flat fields only, no SSSOM stores.
# ---------------------------------------------------------------------------
def _ground_one_legacy(name: str, service: GroundingService, is_drug: bool) -> dict:
    ground = service.ground_drug if is_drug else service.ground_disease
    candidates = ground(name)
    if not candidates:
        return {
            "normalized_id": "", "normalized_label": "", "grounding_confidence": 0.0,
            "grounding_service": "", "grounding_status": "unresolved", "alternate_ids": [],
        }
    best = candidates[0]
    normalized = service.normalize(best.id)
    canonical_id = normalized.id if normalized else best.id
    alt = normalized.alternate_ids if normalized else best.alternate_ids
    return {
        "normalized_id": canonical_id, "normalized_label": best.label,
        "grounding_confidence": round(best.score, 4), "grounding_service": best.service,
        "grounding_status": "accepted", "alternate_ids": alt,
    }


def ground_records(
    records: list[dict],
    grounding_service: GroundingService,
    cache: GroundingCache,
    source_name: str,
) -> tuple[list[dict], dict]:
    """Ground a list of drug records and return grounded records + report.

    Each record must have a ``source_name`` key holding the verbatim source string.
    Adds the structured ``grounding``/``normalization`` objects plus the flat compat
    fields. With the default lexical backend, all decisions are persisted to the
    ``mappings/*.sssom.tsv`` stores (the ``cache`` arg is retained for signature
    compatibility with legacy backends and is otherwise unused here).
    """
    lexical = isinstance(grounding_service, LexicalCascadeGrounding)
    grounded: list[dict] = []
    counts = {"total_drugs": len(records), "auto_accepted": 0,
              "review_recommended": 0, "llm_reranked": 0, "unresolved": 0}
    unresolved_drugs: list[str] = []

    for record in records:
        drug_name = record["source_name"]
        if lexical:
            status = _apply_result(record, "drugs", drug_name, grounding_service)
        else:
            entry = _ground_one_legacy(drug_name, grounding_service, is_drug=True)
            record.update(entry)
            status = entry["grounding_status"]
        if status == "unresolved":
            counts["unresolved"] += 1
            unresolved_drugs.append(drug_name)
        else:
            counts["auto_accepted"] += 1
        grounded.append(record)

    if lexical:
        grounding_service.flush()
        _flush_normalizers()

    report = {**counts, "unresolved_drugs": unresolved_drugs}
    return grounded, report


def ground_disease_records(
    records: list[dict],
    grounding_service: GroundingService,
    cache: GroundingCache,
    source_name: str,
    disease_name_key: str = "disease_name",
    disease_id_key: str = "disease_id",
) -> tuple[list[dict], dict]:
    """Ground / re-ground disease IDs in a list of association records.

    Records already carrying a MONDO id are only Stage-2 normalized (kept as-is when no
    asserted mapping applies). Records without a MONDO id are re-grounded from their
    disease name via the lexical backend, then normalized. Records without a name are
    left untouched.
    """
    lexical = isinstance(grounding_service, LexicalCascadeGrounding)
    grounded: list[dict] = []
    # `regrounded_to_mondo` counts only re-groundings that actually landed on MONDO. The
    # accept test below admits HP as well, and both outcomes used to increment this one
    # counter — so the normalization step could report "1,254 re-grounded to MONDO" in the
    # same line as "non-MONDO: 3229 -> 3229" and neither number was wrong. They were
    # HP re-groundings.
    counts = {"total_diseases": len(records), "already_mondo": 0,
              "regrounded_to_mondo": 0, "regrounded_to_hp": 0, "newly_grounded": 0,
              "review_recommended": 0, "unresolved": 0}

    def _count_regrounding(new_id: str, had_id: bool) -> None:
        if not had_id:
            counts["newly_grounded"] += 1
        elif new_id.startswith("MONDO:"):
            counts["regrounded_to_mondo"] += 1
        else:
            counts["regrounded_to_hp"] += 1
    unresolved_diseases: list[str] = []
    label_key = disease_name_key.replace("name", "label")

    for record in records:
        disease_name = record.get(disease_name_key, "")
        existing_id = record.get(disease_id_key, "") or ""

        # Already canonical MONDO: only Stage-2 normalize (e.g. obsolete replacement).
        # IndicationAssociation now carries a ``disease_normalization`` slot, so attach the
        # structured decision under that key (the merge funnels it to the product record).
        if existing_id.startswith("MONDO:"):
            if lexical:
                normalize_existing_id(
                    record, "diseases", disease_id_key, label_key,
                    attach_object=True, object_key="disease_normalization",
                )
            counts["already_mondo"] += 1
            grounded.append(record)
            continue

        if not disease_name:
            if existing_id:
                counts["unresolved"] += 1
                unresolved_diseases.append(f"{existing_id} (no name)")
            grounded.append(record)
            continue

        if not lexical:
            entry = _ground_one_legacy(disease_name, grounding_service, is_drug=False)
            if entry["grounding_status"] == "unresolved" or not entry["normalized_id"].startswith(
                ("MONDO:", "HP:")
            ):
                counts["unresolved"] += 1
                unresolved_diseases.append(disease_name)
                grounded.append(record)
                continue
            record[disease_id_key] = entry["normalized_id"]
            if label_key in record:
                record[label_key] = entry["normalized_label"]
            _count_regrounding(entry["normalized_id"], bool(existing_id))
            grounded.append(record)
            continue

        # Lexical path: ground from the name, then normalize toward MONDO. Attach the
        # structured decision under the association's ``disease_grounding`` /
        # ``disease_normalization`` slots (the merge funnels them to the product record);
        # the full decision is also logged to the SSSOM store via ``_resolve``.
        from medic.mention import mint_mention_id
        r = _resolve("diseases", disease_name, grounding_service,
                     mention_id=mint_mention_id(disease_name, "diseases"))
        if r.status == "unresolved" or not r.effective_id.startswith(("MONDO:", "HP:")):
            counts["unresolved"] += 1
            unresolved_diseases.append(disease_name)
            grounded.append(record)
            continue

        record[disease_id_key] = r.effective_id
        if label_key in record:
            record[label_key] = r.effective_label or disease_name
        record["disease_grounding"] = r.grounding_obj
        if r.normalization_obj is not None:
            record["disease_normalization"] = r.normalization_obj
        _count_regrounding(r.effective_id, bool(existing_id))
        grounded.append(record)

    if lexical:
        grounding_service.flush()
        _flush_normalizers()

    report = {**counts, "unresolved_diseases": unresolved_diseases}
    return grounded, report

"""The pinned Biolink vocabulary MeDIC's KGX export is allowed to emit.

Every category, predicate, enum value and un-namespaced slot the exporter writes is named
here once, and :mod:`medic.export.kgx.validate` checks each of them against the *installed*
Biolink model. That is the whole point of the module: a mapping table that is checked
against the standard cannot quietly drift into terms Biolink does not define, which is how
``biolink:contraindicated_for`` — a predicate that has never existed in Biolink 4.x — ended
up on every contraindication edge MeDIC shipped.

Anything MeDIC knows that Biolink has no slot for is emitted under the ``medic_`` prefix
(:data:`EXTENSION_SLOTS`), so a strict consumer can drop the whole extension layer with one
rule and still have a valid graph.
"""

from __future__ import annotations

import re
from functools import lru_cache

import yaml

from medic.source_isolation import SOURCE_JURISDICTION as _SOURCE_JURISDICTION

#: The Biolink Model release this export targets. Kept in lockstep with the
#: ``biolink-model`` distribution pinned in the project's ``export`` dependency group; a
#: test fails if the two drift apart, because a silent model upgrade can change which
#: predicates exist.
BIOLINK_VERSION = "4.3.7"

_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")


# ---------------------------------------------------------------------------
# Access to the installed model
# ---------------------------------------------------------------------------
def installed_biolink_version() -> str:
    """Version of the installed ``biolink-model`` distribution."""
    from importlib.metadata import version

    return version("biolink-model")


@lru_cache(maxsize=1)
def _model() -> dict:
    """The installed Biolink model, parsed once.

    ``biolink_model`` is a namespace package (no ``__file__``), so the schema is located
    through ``importlib.resources`` rather than by walking up from the module path.
    """
    from importlib.resources import files

    path = files("biolink_model") / "schema" / "biolink_model.yaml"
    with path.open() as handle:
        return yaml.safe_load(handle)


def model_name(curie: str) -> str:
    """The Biolink model's own name for a CURIE.

    Biolink names classes and slots in lowercase words separated by spaces; KGX writes them
    as CamelCase classes and snake_case slots. ``biolink:DiseaseOrPhenotypicFeature`` is
    ``disease or phenotypic feature``; ``biolink:has_side_effect`` is ``has side effect``.
    """
    local = curie.split(":", 1)[-1]
    if "_" in local or local[:1].islower():
        return local.replace("_", " ")
    return _CAMEL.sub(" ", local).lower()


@lru_cache(maxsize=1)
def biolink_classes() -> frozenset[str]:
    return frozenset(_model().get("classes", {}))


@lru_cache(maxsize=1)
def biolink_slots() -> frozenset[str]:
    return frozenset(_model().get("slots", {}))


def enum_values(enum_name: str) -> frozenset[str]:
    return frozenset(_model()["enums"][enum_name]["permissible_values"])


@lru_cache(maxsize=None)
def is_predicate(curie: str) -> bool:
    """Whether a CURIE names a Biolink predicate (a descendant of ``related to``)."""
    slots = _model().get("slots", {})
    name = model_name(curie)
    seen: set[str] = set()
    while name in slots and name not in seen:
        if name == "related to":
            return True
        seen.add(name)
        name = slots[name].get("is_a", "")
    return name == "related to"


@lru_cache(maxsize=None)
def id_prefixes(category: str) -> frozenset[str]:
    """Identifier prefixes the model declares for a category (empty if it declares none)."""
    cls = _model().get("classes", {}).get(model_name(category), {})
    return frozenset(cls.get("id_prefixes") or ())


# ---------------------------------------------------------------------------
# Node categories
# ---------------------------------------------------------------------------
DRUG_CATEGORIES = ["biolink:Drug", "biolink:ChemicalEntity"]
DISEASE_CATEGORIES = ["biolink:Disease"]

#: Category for an edge endpoint that no product describes (spec §4.3). MeDIC genuinely
#: emits such endpoints — research associations carry ``UNII:`` drugs and ``ORPHA:``/
#: ``UMLS:`` diseases that Stage-2 normalization could not map — and a KGX graph with a
#: dangling edge is invalid, so they become stub nodes typed by prefix.
PREFIX_CATEGORY = {
    "MONDO": "biolink:Disease",
    "ORPHA": "biolink:Disease",
    "Orphanet": "biolink:Disease",
    "UMLS": "biolink:Disease",
    "DOID": "biolink:Disease",
    "OMIM": "biolink:Disease",
    "MESH": "biolink:Disease",
    "HP": "biolink:PhenotypicFeature",
    "MedDRA": "biolink:DiseaseOrPhenotypicFeature",
    "CHEBI": "biolink:ChemicalEntity",
    "DRON": "biolink:ChemicalEntity",
    "UNII": "biolink:ChemicalEntity",
    "RXNORM": "biolink:ChemicalEntity",
    "DRUGBANK": "biolink:ChemicalEntity",
    "PUBCHEM.COMPOUND": "biolink:ChemicalEntity",
    "CHEMBL.COMPOUND": "biolink:ChemicalEntity",
}
FALLBACK_CATEGORY = "biolink:NamedThing"


def category_for_prefix(curie: str) -> str:
    """Category for a stub node, inferred from its identifier prefix."""
    return PREFIX_CATEGORY.get(curie.split(":", 1)[0], FALLBACK_CATEGORY)


def all_categories() -> list[str]:
    return sorted({*DRUG_CATEGORIES, *DISEASE_CATEGORIES, *PREFIX_CATEGORY.values(),
                   FALLBACK_CATEGORY})


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------
#: An approved regulatory indication. Biolink restricts asserted ``treats`` edges to cases
#: with strong supporting evidence and names this one explicitly: "in some population(s) the
#: intervention is approved for the condition".
INDICATION_PREDICATE = "biolink:treats"
#: An indication whose regulatory status is not APPROVED does not meet that bar, so it drops
#: to the grouping predicate rather than overclaiming.
INDICATION_UNAPPROVED_PREDICATE = "biolink:treats_or_applied_or_studied_to_treat"
CONTRAINDICATION_PREDICATE = "biolink:contraindicated_in"
#: A vaccine does not treat the disease it immunises against. Biolink separates the two, and
#: a graph that conflates them answers "what treats pertussis?" with the vaccine (#99).
PREVENTION_PREDICATE = "biolink:preventative_for_condition"

#: Real-world use observed in case reports (CURE-ID), which is what ``applied to treat``
#: means: "actually taken by one or more patients with the intent of treating the condition".
RESEARCH_APPLIED_PREDICATE = "biolink:applied_to_treat"
RESEARCH_TRIAL_PREDICATE = "biolink:in_clinical_trials_for"
RESEARCH_STUDIED_PREDICATE = "biolink:studied_to_treat"

#: Biolink separates a label-listed side effect ("Side effects are listed on drug labels")
#: from a spontaneously reported adverse event ("may be caused by something other than the
#: drug"). That is exactly the PVLens/FAERS distinction, so the two sources do not collapse
#: onto one predicate.
AE_LABEL_PREDICATE = "biolink:has_side_effect"
AE_REPORT_PREDICATE = "biolink:has_adverse_event"

#: Knowledge level per predicate (Biolink ``KnowledgeLevelEnum``).
KNOWLEDGE_LEVELS = {
    INDICATION_PREDICATE: "knowledge_assertion",
    INDICATION_UNAPPROVED_PREDICATE: "not_provided",
    CONTRAINDICATION_PREDICATE: "knowledge_assertion",
    PREVENTION_PREDICATE: "knowledge_assertion",
    RESEARCH_APPLIED_PREDICATE: "observation",
    RESEARCH_TRIAL_PREDICATE: "observation",
    RESEARCH_STUDIED_PREDICATE: "observation",
    AE_LABEL_PREDICATE: "knowledge_assertion",
    AE_REPORT_PREDICATE: "observation",
}


def all_predicates() -> list[str]:
    return sorted(KNOWLEDGE_LEVELS)


# ---------------------------------------------------------------------------
# Agent type — derived from recorded provenance, never asserted (spec §5.3)
# ---------------------------------------------------------------------------
#: What actually produced the assertion. MeDIC records this per assertion
#: (``assertion.method`` / ``assertion.agent.agent_type``); the previous export hard-coded
#: ``manual_agent`` on every edge, including DailyMed indications an LLM extracted from SPL
#: text.
AGENT_TYPE_BY_METHOD = {
    "LLM": "text_mining_agent",
    "STRUCTURED_FIELD": "data_analysis_pipeline",
    "DETERMINISTIC_RULE": "data_analysis_pipeline",
    "LEXICAL_MATCH": "data_analysis_pipeline",
    "MANUAL": "manual_agent",
}
AGENT_TYPE_BY_AGENT = {
    "AI_AGENT": "text_mining_agent",
    "HUMAN": "manual_agent",
    "PIPELINE": "data_analysis_pipeline",
}
DEFAULT_AGENT_TYPE = "not_provided"


def agent_type(method: str = "", agent_kind: str = "") -> str:
    """The Biolink ``agent_type`` implied by how an assertion was produced."""
    if agent_kind and agent_kind.upper() in AGENT_TYPE_BY_AGENT:
        return AGENT_TYPE_BY_AGENT[agent_kind.upper()]
    return AGENT_TYPE_BY_METHOD.get((method or "").upper(), DEFAULT_AGENT_TYPE)


# ---------------------------------------------------------------------------
# Knowledge sources
# ---------------------------------------------------------------------------
AGGREGATOR = "infores:medic"

#: ``DataSourceNameEnum`` -> infores. Keyed on the concrete source artifact rather than the
#: authority, because one authority surfaces through several distinct artifacts (an FDA
#: approval appears in DailyMed, the Orange Book and the Purple Book) and a consumer needs to
#: know which one attested the edge.
SOURCE_INFORES = {
    "DAILYMED": "infores:fda-dailymed",
    "ORANGEBOOK": "infores:fda-orange-book",
    "PURPLEBOOK": "infores:fda-purple-book",
    "EMA": "infores:ema",
    "EMA_EPAR": "infores:ema",
    "PMDA": "infores:pmda",
    "CDSCO": "infores:cdsco",
    "GRLS": "infores:grls",
    "CDE_CHINA": "infores:nmpa",
    "PVLENS": "infores:pvlens",
    "FAERS": "infores:faers",
    "PUBMED": "infores:pubmed",
    "CUREID": "infores:cure-id",
    "CLINICAL_TRIAL": "infores:clinicaltrials",
}
#: Fallback when only the authority is known.
AUTHORITY_INFORES = {
    "FDA": "infores:fda",
    "EMA": "infores:ema",
    "PMDA": "infores:pmda",
    "CDSCO": "infores:cdsco",
    "MOH_RUSSIA": "infores:grls",
    "NMPA_CHINA": "infores:nmpa",
}
#: Primary source for a claim MeDIC's own curation asserts without an external citable
#: source (a deep-research row whose only reference is a bare website). Deliberately NOT
#: :data:`AGGREGATOR` — naming the aggregator as the primary source implies MeDIC merely
#: republished something it in fact asserted itself.
MEDIC_CURATION = "infores:medic-research-curation"

#: Fallback when a *regulatory* source cannot be mapped to a known infores. Falling back to
#: the aggregator is honest ("we don't know the primary") rather than inventing an
#: unregistered infores id, and :mod:`medic.export.kgx.validate` warns with a count so the
#: gap is visible instead of silently mislabelled.
UNKNOWN_SOURCE = AGGREGATOR


def primary_knowledge_source(source: str = "", authority: str = "") -> str:
    """The single infores that primarily attests an edge (Biolink allows exactly one)."""
    if source and source.upper() in SOURCE_INFORES:
        return SOURCE_INFORES[source.upper()]
    if authority and authority.upper() in AUTHORITY_INFORES:
        return AUTHORITY_INFORES[authority.upper()]
    return UNKNOWN_SOURCE


#: The jurisdiction each source is allowed to speak for (invariant I-1). ``None`` means the
#: source is not jurisdiction-bound (literature, curated databases).
# Re-exported from `medic.source_isolation`, the single statement of invariant I-1. The
# export used to keep its own copy, which is how it came to disagree with what the
# exporter writes and exempt every India edge.
SOURCE_JURISDICTION = _SOURCE_JURISDICTION


# ---------------------------------------------------------------------------
# Slots the exporter writes
# ---------------------------------------------------------------------------
#: Un-namespaced node properties.
#:
#: ``highest_FDA_approval_status`` is deliberately absent. Its range is Biolink's
#: ``ApprovalStatusEnum``, which enumerates FDA *review pathways* (``fda_fast_track``,
#: ``fda_accelerated_approval``, ``regular_fda_approval``) — not marketing status. MeDIC
#: knows RX/OTC/DISCN and does not know which pathway a drug took, so filling the slot would
#: mean inventing knowledge. The real value stays on ``medic_marketing_status_usa``.
CORE_NODE_SLOTS = (
    "id", "name", "category", "description", "synonym", "xref", "provided_by",
)

#: Un-namespaced edge properties. Several of these were nearly namespaced before a sweep of
#: the model found Biolink already defines them — the verbatim source strings
#: (``original_subject``/``original_object``), the quoted label text (``supporting_text``),
#: the mention character offsets (``*_location_in_text``) and the approval/phase enums. Using
#: the standard slot is always preferred; ``medic_*`` is the residue Biolink cannot express.
CORE_EDGE_SLOTS = (
    "id", "subject", "predicate", "object",
    "primary_knowledge_source", "aggregator_knowledge_source",
    "knowledge_level", "agent_type", "publications",
    "original_subject", "original_object", "has_evidence",
    "supporting_text", "supporting_text_section_type",
    "subject_location_in_text", "object_location_in_text",
    "clinical_approval_status", "max_research_phase", "has_confidence_score",
)

#: Biolink defines these as single-valued. Emitting a list is the defect that collapsing
#: several jurisdictions onto one edge used to force.
SINGLE_VALUED_EDGE_SLOTS = ("primary_knowledge_source", "knowledge_level", "agent_type",
                            "subject", "object", "predicate", "id")

EXTENSION_PREFIX = "medic_"

#: Everything MeDIC knows that Biolink has no slot for. Named exhaustively so the collision
#: check (a ``medic_x`` shadowing a Biolink slot ``x``) can run over the whole vocabulary.
EXTENSION_SLOTS = (
    # node — drug
    "medic_smiles", "medic_atc_codes", "medic_atc_main", "medic_atc_level1",
    "medic_atc_level2", "medic_atc_level3", "medic_atc_level4", "medic_atc_level5",
    "medic_features", "medic_is_combination_therapy", "medic_combination_ingredients",
    "medic_combination_ingredient_ids", "medic_approved_authorities",
    "medic_approved_jurisdictions", "medic_earliest_approval_date",
    "medic_application_numbers", "medic_registration_numbers",
    "medic_marketing_status_usa",
    "medic_regulatory_document_urls",
    # node — disease
    "medic_subsets",
    # node — shared
    "medic_reliability", "medic_mention_id", "medic_original_literal",
    "medic_mention_source", "medic_grounding_quality", "medic_resolution_confidence",
    "medic_stub",
    # edge — source and document
    "medic_source", "medic_document", "medic_jurisdiction", "medic_authority",
    "medic_source_role", "medic_regulatory_status", "medic_document_url",
    "medic_source_document_url", "medic_approval_date", "medic_setid",
    "medic_application_number", "medic_reference", "medic_reference_url",
    "medic_reference_title", "medic_withdrawn",
    # edge — claim
    "medic_supporting_text_truncated", "medic_explanation",
    "medic_trigger_cue", "medic_trigger_span", "medic_negation_detected",
    "medic_assertion_flags", "medic_assertion_method", "medic_agent_name",
    "medic_agent_version", "medic_tool", "medic_tool_version", "medic_curation_status",
    "medic_evidence_source", "medic_study_status", "medic_support",
    # edge — MeDIC values Biolink's enums cannot express (kept alongside the mapped slot)
    "medic_approval_status_raw", "medic_research_phase_raw",
    # edge — adverse events (Biolink's severity/frequency qualifiers take ontology terms,
    # MeDIC has an enum and free text, so mapping them would require a term table)
    "medic_severity", "medic_frequency", "medic_label_section",
    # edge — confidence and quality
    "medic_confidence_subject", "medic_confidence_object",
    "medic_confidence_relationship", "medic_confidence_overall", "medic_confidence_basis",
    # edge — resolution join keys
    "medic_subject_mention_id", "medic_object_mention_id",
    "medic_subject_grounding_quality", "medic_object_grounding_quality",
    "medic_subject_applied_rules", "medic_object_applied_rules",
    "medic_subject_grounding_flags", "medic_object_grounding_flags",
    "medic_subject_translated", "medic_object_translated",
    # edge — pair aggregates (repeated on every edge of the pair, spec §2.1)
    "medic_pair_confidence", "medic_pair_n_assertions", "medic_pair_reliability",
    "medic_pair_jurisdictions", "medic_pair_authorities",
    # edge — contraindication specifics
    "medic_is_allergen", "medic_is_diagnostic_agent",
)

#: The regulatory-label evidence class. Only this one has an unambiguous ECO term; the slot
#: is omitted elsewhere rather than populated with a plausible guess (spec §10).
#:
#: `ECO:0006156` — "documented statement evidence used in automatic assertion". A regulatory
#: label *is* a documented statement, and MeDIC reads it without human review.
#:
#: This was `ECO:0000218` ("manual assertion"), which was wrong twice over: it sits under
#: `assertion method` rather than `evidence`, so it does not belong in `has_evidence` at all,
#: and it claims "an assertion method that involves human review" on 12,694 LLM-mined edges.
#: The PR that built this export records fixing exactly that overclaim for `agent_type`; the
#: same false claim survived here. A curator-confirmed statement would warrant the manual
#: sibling under `documented statement evidence`, but nothing sets that today.
ECO_REGULATORY_LABEL = "ECO:0006156"


# ---------------------------------------------------------------------------
# MeDIC enum -> Biolink enum
# ---------------------------------------------------------------------------
#: ``ApprovalStatusEnum`` -> ``ClinicalApprovalStatusEnum``. FDA gets the FDA-specific value
#: because Biolink offers one and losing that distinction would be a downgrade.
#:
#: ``DISCONTINUED`` maps to ``not_provided`` on purpose. Biolink's nearest value is
#: ``post_approval_withdrawal``, which reads as a safety withdrawal; MeDIC's DISCONTINUED
#: means marketing ceased, which is usually commercial. Asserting the former would tell
#: consumers a drug was pulled for safety when it was not. The true value survives in
#: ``medic_approval_status_raw``.
CLINICAL_APPROVAL_STATUS = {
    "APPROVED": "approved_for_condition",
    "APPROVED_FDA": "fda_approved_for_condition",
    "INVESTIGATIONAL": "not_approved_for_condition",
    "WITHDRAWN": "post_approval_withdrawal",
    "DISCONTINUED": "not_provided",
    "OFF_LABEL": "off_label_use",
}

#: ``ResearchPhaseEnum`` (MeDIC) -> ``ResearchPhaseEnum`` (Biolink). MeDIC's CASE_REPORT,
#: IN_VITRO and COMPUTATIONAL have no Biolink counterpart; they degrade to ``not_provided``
#: and keep their real value in ``medic_research_phase_raw``.
RESEARCH_PHASE = {
    "PRE_CLINICAL": "pre_clinical_research_phase",
    "PHASE_I": "clinical_trial_phase_1",
    "PHASE_II": "clinical_trial_phase_2",
    "PHASE_III": "clinical_trial_phase_3",
    "PHASE_IV": "clinical_trial_phase_4",
}


def clinical_approval_status(status: str, authority: str = "") -> str:
    key = (status or "").upper()
    if key == "APPROVED" and (authority or "").upper() == "FDA":
        key = "APPROVED_FDA"
    return CLINICAL_APPROVAL_STATUS.get(key, "not_provided")


def research_phase(phase: str) -> str:
    return RESEARCH_PHASE.get((phase or "").upper(), "not_provided")

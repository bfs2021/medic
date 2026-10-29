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
#: ``biolink-model`` distribution pinned in ``pyproject.toml``; a
#: test fails if the two drift apart, because a silent model upgrade can change which
#: predicates exist.
BIOLINK_VERSION = "4.4.6"

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
# The pydantic model — the guard rail
# ---------------------------------------------------------------------------
#: Every node and edge the exporter writes is built by instantiating the matching class of
#: the ``biolink_model`` pydantic model and dumping it. The model is generated from the same
#: schema :func:`_model` reads, and it is strict: ``extra="forbid"`` rejects a property the
#: class does not declare, enum slots reject values outside their permissible set, required
#: slots cannot be omitted, and ``category`` is fixed to the class. A term that is not in
#: Biolink 4.4.6 therefore cannot reach the output file, which is a stronger guarantee than
#: the gate's post-hoc check that each name exists somewhere in the model.
@lru_cache(maxsize=1)
def pydantic_model():
    from biolink_model.datamodel import pydanticmodel_v2

    return pydanticmodel_v2


def model_class(category: str):
    """The pydantic class for a Biolink category CURIE (``biolink:Drug`` -> ``Drug``)."""
    return getattr(pydantic_model(), category.split(":", 1)[-1])


@lru_cache(maxsize=None)
def predicate_ancestors(curie: str) -> frozenset[str]:
    """Every predicate a CURIE specialises, through ``is_a`` *and* ``mixins``.

    Both matter: ``preventative for condition`` is a ``treats`` only through its mixin, and
    ``applied to treat`` reaches ``treats or applied or studied to treat`` the same way.
    """
    slots = _model().get("slots", {})
    out: set[str] = set()
    todo = [model_name(curie)]
    while todo:
        name = todo.pop()
        if name in out or name not in slots:
            continue
        out.add(name)
        parent = slots[name].get("is_a")
        if parent:
            todo.append(parent)
        todo.extend(slots[name].get("mixins") or [])
    return frozenset(out)


@lru_cache(maxsize=None)
def class_predicate_constraint(category: str) -> str:
    """The predicate an association class restricts itself to (``""`` if unrestricted).

    Read from the class's ``slot_usage`` and its ancestors'. This is the one rule the
    pydantic class cannot enforce — it types ``predicate`` as a plain string — so the gate
    checks it against :func:`predicate_ancestors`.
    """
    classes = _model().get("classes", {})
    name = model_name(category)
    seen: set[str] = set()
    while name in classes and name not in seen:
        seen.add(name)
        usage = (classes[name].get("slot_usage") or {}).get("predicate") or {}
        if usage.get("subproperty_of"):
            return usage["subproperty_of"]
        name = classes[name].get("is_a", "")
    return ""


# ---------------------------------------------------------------------------
# Node categories
# ---------------------------------------------------------------------------
#: One category per node — the pydantic class fixes ``category`` to its own name, and the
#: class hierarchy is the consumer's to expand. The old export listed
#: ``[Drug, ChemicalEntity]``, which the model rejects (``Drug.category`` is a literal).
DRUG_CATEGORY = "biolink:Drug"
DISEASE_CATEGORY = "biolink:Disease"

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
    return sorted({DRUG_CATEGORY, DISEASE_CATEGORY, *PREFIX_CATEGORY.values(),
                   FALLBACK_CATEGORY})


#: ``DrugAvailabilityEnum``. MeDIC's US marketing status is RX/OTC/DISCN; the first two are
#: exactly Biolink's two values. DISCN (marketing ceased) is not an availability and stays
#: on ``medic_marketing_status_usa`` only.
AVAILABLE_FROM = {"RX": "prescription", "OTC": "over_the_counter"}


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
#: A contrast agent or radiotracer does not treat the disease it images. The audit found
#: secretin, flutemetamol, 5-ALA and edotreotide exported as `treats` (#99).
DIAGNOSTIC_PREDICATE = "biolink:diagnoses"

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
    DIAGNOSTIC_PREDICATE: "knowledge_assertion",
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
# Association classes
# ---------------------------------------------------------------------------
#: Predicate -> the Biolink association class an edge is built as. Chosen by what the class
#: *constrains*: ``ChemicalOrDrugOrTreatmentToDiseaseOrPhenotypicFeatureAssociation``
#: restricts its predicate to ``treats or applied or studied to treat`` and every
#: treatment-family predicate here specialises that (``preventative for condition`` through
#: its ``treats`` mixin); the two adverse-event classes restrict to ``has side effect`` /
#: ``has adverse event``. ``contraindicated in`` and ``diagnoses`` specialise nothing a
#: class restricts to, so they take ``ChemicalEntityToDiseaseOrPhenotypicFeatureAssociation``
#: — the unrestricted chemical-to-disease class, and the one Biolink hangs
#: ``clinical approval status`` on — rather than the bare ``Association``.
ASSOCIATION_CLASS = {
    "biolink:treats":
        "biolink:ChemicalOrDrugOrTreatmentToDiseaseOrPhenotypicFeatureAssociation",
    "biolink:treats_or_applied_or_studied_to_treat":
        "biolink:ChemicalOrDrugOrTreatmentToDiseaseOrPhenotypicFeatureAssociation",
    "biolink:preventative_for_condition":
        "biolink:ChemicalOrDrugOrTreatmentToDiseaseOrPhenotypicFeatureAssociation",
    "biolink:applied_to_treat":
        "biolink:ChemicalOrDrugOrTreatmentToDiseaseOrPhenotypicFeatureAssociation",
    "biolink:in_clinical_trials_for":
        "biolink:ChemicalOrDrugOrTreatmentToDiseaseOrPhenotypicFeatureAssociation",
    "biolink:studied_to_treat":
        "biolink:ChemicalOrDrugOrTreatmentToDiseaseOrPhenotypicFeatureAssociation",
    "biolink:contraindicated_in":
        "biolink:ChemicalEntityToDiseaseOrPhenotypicFeatureAssociation",
    "biolink:diagnoses":
        "biolink:ChemicalEntityToDiseaseOrPhenotypicFeatureAssociation",
    "biolink:has_side_effect": "biolink:ChemicalOrDrugOrTreatmentSideEffectAssociation",
    "biolink:has_adverse_event": "biolink:ChemicalOrDrugOrTreatmentAdverseEventAssociation",
}


def association_category(predicate: str) -> str:
    return ASSOCIATION_CLASS[predicate]


# ---------------------------------------------------------------------------
# Slots the exporter writes
# ---------------------------------------------------------------------------
#: Biolink slots the exporter writes on an edge that the model defines but does **not**
#: attach to the association class the edge is built as, so they bypass the pydantic
#: constructor and are checked by the gate for existence and enum range only. Each is here
#: for a stated reason, and a test pins the list so it cannot grow quietly:
#:
#: * ``clinical_approval_status`` / ``max_research_phase`` — 4.4.6 moved them onto the
#:   ``treats`` class (via the clinical-trial-and-regulatory-approval-context mixin), where
#:   they are now validated by the constructor, and off the contraindication class, where
#:   they still bypass it. The slots' own definitions are about a chemical–condition pair,
#:   which is what every edge here is.
#: * ``temporal_context_qualifier`` / ``sex_qualifier`` — attached only to exposure-outcome
#:   and phenotype associations. They carry the label's stated context (#90) and DAKP
#:   ships them on ``treats`` edges in the same graph. (``population_context_qualifier``
#:   left this list in 4.4.6, which attached it to every class the exporter writes.)
#: * ``subject_location_in_text`` / ``object_location_in_text`` — attached only to
#:   ``TextMiningStudyResult``; they are offsets into ``supporting_text``, which *is* on
#:   the association.
#:
#: Everything else un-namespaced goes through the class constructor and cannot be a slot
#: the class lacks.
OFF_CLASS_EDGE_SLOTS = (
    "clinical_approval_status", "max_research_phase",
    "temporal_context_qualifier", "sex_qualifier",
    "subject_location_in_text", "object_location_in_text",
)

#: Enum-ranged off-class slots, checked by the gate since pydantic never sees them.
OFF_CLASS_ENUMS = {
    "clinical_approval_status": "ClinicalApprovalStatusEnum",
    "max_research_phase": "ResearchPhaseEnum",
}

#: ``sex_qualifier`` has range ``biological sex``, a class whose ids are PATO terms. The
#: label's word maps to the term rather than shipping ``"women"`` in a class-ranged slot.
SEX_QUALIFIER_TERMS = {"female": "PATO:0000383", "male": "PATO:0000384"}

#: The only prefix ``disease_context_qualifier`` may carry (#110).
#:
#: The slot's range is ``disease``, and neither guard on the way out catches a value that is
#: not one: UMLS and HP are on that class's ``id_prefixes``, so the pydantic constructor
#: accepts them, and the slot is typed as a plain ``str``, so it accepts free text too. On
#: the 95,170-edge build the slot was populated 20,342 times and only 10,314 of those were
#: MONDO — 3,702 UMLS, 1,618 HP and 4,708 free text over 1,022 distinct strings, several of
#: them wording variants of one context ("non-neutropenic", "non-neutropenic patients")
#: that split otherwise identical edges.
#:
#: Narrowed to MONDO rather than to the class's whole prefix list so the context is a node
#: in the same disease space the edge's own object sits in, which is what a consumer joins
#: on. The non-MONDO ids are not discarded: every grounded id stays on
#: ``medic_disease_context_ids`` and the verbatim names on ``medic_disease_context``.
DISEASE_CONTEXT_PREFIX = "MONDO"

EXTENSION_PREFIX = "medic_"

#: Everything MeDIC knows that Biolink has no slot for **and** that a graph consumer needs
#: on the node or edge itself. Named exhaustively so the collision check (a ``medic_x``
#: shadowing a Biolink slot ``x``) can run over the whole vocabulary.
#:
#: This is deliberately the residue, not the inventory. The previous export namespaced 62
#: edge and 30 node properties; most were either derivable from a Biolink slot already on
#: the record, constant across every edge of a build (so belong in the metadata file), or
#: detail that is replayable from the products through the ``MEDICNE:`` join keys (I-8).
#: What is kept: the I-1 source/jurisdiction pair the gate checks, the join keys, the
#: pair-level aggregates a ``GROUP BY`` cannot rebuild, quality flags a consumer should
#: weigh, and claim context Biolink has no qualifier for.
EXTENSION_SLOTS = (
    # node — drug
    "medic_smiles", "medic_atc_codes", "medic_features",
    "medic_is_combination_therapy", "medic_combination_ingredient_ids",
    "medic_approved_authorities", "medic_approved_jurisdictions",
    "medic_earliest_approval_date", "medic_marketing_status_usa",
    "medic_application_numbers", "medic_registration_numbers",
    # node — disease
    # node — shared
    "medic_reliability", "medic_mention_id", "medic_stub",
    # edge — source (invariant I-1 is checked on these two)
    "medic_source", "medic_jurisdiction",
    # edge — regulatory facts Biolink's enum cannot carry
    "medic_approval_status_raw", "medic_approval_date",
    # edge — quality
    "medic_assertion_flags", "medic_supporting_text_truncated",
    # edge — resolution join keys into the shipped mappings/ decision stores (spec §2.3)
    "medic_subject_mention_id", "medic_object_mention_id",
    # edge — pair aggregates (repeated on every edge of the pair, spec §2.1)
    "medic_pair_confidence", "medic_pair_n_assertions", "medic_pair_reliability",
    # edge — claim context with no Biolink qualifier. `medic_disease_context` carries the
    # label's own wording: once `disease_context_qualifier` is MONDO-only (#110) nothing
    # else on the record holds the context for the 4,708 edges whose context did not ground,
    # so by this tuple's own bar — not on a Biolink slot here, not constant, not replayable
    # through a mention id, since contexts are not minted one — it has to stay.
    "medic_disease_context", "medic_disease_context_ids",
    "medic_co_administered_with", "medic_claim_relation",
    # edge — contraindication specifics
    "medic_is_allergen", "medic_is_diagnostic_agent",
    # edge — research
    "medic_research_phase_raw", "medic_curation_status",
    # edge — adverse events (Biolink's severity/frequency qualifiers take ontology terms,
    # MeDIC has an enum and free text, so mapping them would require a term table)
    "medic_severity", "medic_frequency", "medic_label_section",
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

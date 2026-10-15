"""KGX edge builders.

**One edge per source assertion**, not per drug–disease pair. Each edge is then
single-sourced by construction — the same property invariant I-10 enforces on
``SourceAssertion`` — so it carries exactly one ``primary_knowledge_source``, one document,
one quoted span and one confidence. Collapsing several jurisdictions onto one edge is what
forced the previous export to emit ``primary_knowledge_source`` as a list, which Biolink
defines as single-valued.

The pair-level view is not lost: every edge repeats the pair's aggregates
(``medic_pair_*``), so ``GROUP BY (subject, predicate, object)`` reconstructs it without
re-deriving anything.

Predicate choice is driven by *recorded data* — regulatory status, research phase, source —
never by a source's name. That mirrors the rule ``medic.reliability`` already follows: the
idiosyncrasies of DailyMed vs GRLS vs CDE must not leak into the semantics.
"""

from __future__ import annotations

import re
import uuid

from medic import product_view as pv
from medic.export.kgx import biolink as bl
from medic.reliability import (
    StatementReviewStore,
    StatementType,
    default_review_store,
    score_reliability,
)

#: Quoted label text is truncated here. Whole SPL sections run to tens of kilobytes, and an
#: edge property is not a document store; the full text stays in the products.
MAX_SUPPORTING_TEXT = 2000

#: Fixed namespace for MeDIC edge ids, derived deterministically from a stdlib namespace
#: constant (never random) exactly like ``medic.mention.MEDICNE_NAMESPACE``. Do not change
#: it — every already-minted edge id depends on it.
MEDICEDGE_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://w3id.org/monarch-initiative/medic/MEDICEDGE"
)

#: Reference prefixes that name a citable publication. A bare URL is not one, and putting it
#: in ``publications`` would hand consumers an unresolvable "publication" id.
PUBLICATION_PREFIXES = ("PMID", "PMC", "NCT", "DOI")


def _clean(edge: dict) -> dict:
    return {k: v for k, v in sorted(edge.items()) if v not in ("", [], {}, None)}


def _uniq(values) -> list[str]:
    return sorted({v.strip() for v in values if isinstance(v, str) and v.strip()})


def edge_id(subject: str, predicate: str, obj: str, source: str, document: str) -> str:
    """Deterministic edge id, so two builds of unchanged products diff cleanly."""
    key = "\t".join((subject, predicate, obj, source, document))
    return f"MEDICEDGE:{uuid.uuid5(MEDICEDGE_NAMESPACE, key)}"


def _step(mention: dict, category: str) -> dict:
    for step in ((mention.get("resolution") or {}).get("pipeline")) or []:
        if isinstance(step, dict) and step.get("category") == category:
            return step
    return {}


def _offsets(mention: dict, supporting_text: str) -> list[int]:
    """Character offsets of the mention within ``supporting_text``, if they hold.

    Biolink defines ``*_location_in_text`` as offsets into ``supporting_text``, so they are
    only emitted when they actually select the mention's recorded literal in the string being
    shipped. They are computed at merge against the *span* the extraction read
    (``provenance_build._extraction_step``), and the export used to ship the evidence snippet
    instead — a different, separately truncated string. 1,843 of 10,150 edges carried offsets
    that pointed outside it or at the wrong words; the rest were right by luck of the mention
    falling inside the first 500 characters.

    Verifying against the literal rather than just the length also catches drift: if the two
    strings ever diverge again, the offsets disappear instead of quietly lying.
    """
    step = _step(mention, "EXTRACTION")
    start, end = step.get("char_start"), step.get("char_end")
    if not (isinstance(start, int) and isinstance(end, int)):
        return []
    if not supporting_text or end > len(supporting_text):
        return []
    literal = (step.get("output_value") or mention.get("original_literal") or "").strip()
    if literal and supporting_text[start:end].strip() != literal:
        return []
    return [start, end]


def _mention_properties(mention: dict, role: str) -> dict:
    """The resolution join keys for one side of an edge (spec §2.3)."""
    grounding = _step(mention, "GROUNDING")
    return {
        f"medic_{role}_mention_id": mention.get("id") or "",
        f"medic_{role}_grounding_quality": grounding.get("quality") or "",
        f"medic_{role}_applied_rules": list(grounding.get("applied_rules") or []),
        f"medic_{role}_grounding_flags": list(grounding.get("flags") or []),
        f"medic_{role}_translated": bool(_step(mention, "TRANSLATION")) or "",
    }


def _publications(references) -> list[str]:
    return _uniq(
        ref for ref in references
        if isinstance(ref, str) and ref.split(":", 1)[0].upper() in PUBLICATION_PREFIXES
    )


def _supporting_text(text: str) -> tuple[str, bool]:
    """The quoted span, and whether it was cut.

    Returns a **string**. Biolink's ``supporting_text`` is single-valued, and wrapping it in a
    one-element list is the same single-valued-discipline violation this export already fixed
    for ``primary_knowledge_source`` — a consumer reading the slot per the model gets
    ``["..."]`` where it expects text.
    """
    if not text:
        return "", False
    if len(text) <= MAX_SUPPORTING_TEXT:
        return text, False
    return text[:MAX_SUPPORTING_TEXT], True


def _assertion_reliability(
    pair: dict, assertion: dict, review: StatementReviewStore
) -> str:
    """Reliability of *this* assertion, scored by the existing gates.

    Built by handing the scorer a synthetic single-assertion record, so the export reuses
    ``medic.reliability`` unchanged rather than reimplementing its gates.

    The curator verdict is keyed on the *pair*, not the assertion, because that is what
    ``statement_key`` addresses — a human rejects a drug-disease claim, not one regulator's
    attestation of it. Omitting it here published a REJECTED statement as HIGH (#62).
    """
    scalars = {k: v for k, v in pair.items() if k != "assertions"}
    return score_reliability(
        {**scalars, "assertions": [assertion]},
        review_status=review.status(pair),
    ).value


# ---------------------------------------------------------------------------
# Indications and contraindications
# ---------------------------------------------------------------------------
def _source_literal(pair: dict, role: str) -> str:
    """The source's own wording for an endpoint, as a name of last resort.

    A CUI whose preferred atoms are all in label-restricted vocabularies (MedDRA, ICD-10)
    has no label we may ship, so its node went out as a bare id: 91 stubs used by 5,086
    edges, and the audit could not judge those edges at all. The literal the source wrote is
    already recorded on the mention, and it is the one name that is always available (#96).
    """
    for assertion in pv.assoc_assertions(pair):
        mention = assertion.get(role) or {}
        literal = (mention.get("original_literal") or "").strip()
        if literal:
            return literal
    return ""


def _withheld(assertion: dict) -> bool:
    """Has the pipeline decided the source states this relation *negatively*?

    `negated_inversion` is the one verdict that means "the claim is false as written", and
    an edge carrying it should not leave here wearing the same predicate as everything
    else. 14 did in the 2026-10-13 export (#93) — acetylsalicylic acid to acute MI, six
    antidiabetics to diabetic ketoacidosis ("not for the treatment of diabetic
    ketoacidosis").

    Dropped rather than flagged with Biolink's `negated` slot: a consumer that ignores the
    slot sees a positive `treats` edge, which is the same failure as leaving them to read
    `medic_pair_reliability`. Nothing becomes unauditable — the claims stay in `kb/` and in
    the products with their reason attached, which is what #66 traded the ingest-time drop
    for, and a curator can still overturn the verdict through the review store.

    **Deliberately narrower than "drop EXCLUDED pairs".** EXCLUDED is reachable several
    ways, including an unresolved grounding and a curator's REJECT, and #62 decided that a
    REJECT should *mark* the edge rather than delete it so the verdict stays observable in
    the export. Over the current products the two sets are the same 14 assertions, so this
    narrower rule achieves #93's result without reversing #62. If they ever diverge — a
    REJECT that is not a negation — whether a human verdict deletes or marks is a policy
    call, not something to settle by widening a gate.

    Judged per assertion, not per pair, so one source inverting a claim cannot delete
    another regulator's good attestation of it.
    """
    claim = assertion.get("assertion") or {}
    return "negated_inversion" in (claim.get("flags") or [])


#: Product-name tokens that identify a vaccine or toxoid. ATC J07 would be the principled
#: selector, but it is unpopulated for every one of these drugs (0 of 8,085 pairs), so the
#: name is all there is. Measured against the corpus rather than guessed: these catch 37
#: pairs, all genuine vaccine products, and no non-vaccine drug name in the indication
#: corpus matches.
#:
#: Immune globulins are deliberately absent. For `Rho(D) immune globulin -> ectopic
#: pregnancy` the object is the exposure *setting*, not a condition the product prevents, so
#: relabelling the predicate would swap one wrong statement for another. That object is
#: #89's error class and belongs there.
_VACCINE_TOKENS = re.compile(
    r"(?i)\b(vaccine|toxoid|capsular polysaccharide|live antigen|purified antigen"
    r"|surface protein antigen|polysaccharide conjugated)\b"
)


def _indication_predicate(status: str, drug_label: str = "") -> str:
    """Biolink reserves an asserted ``treats`` for approved or established use.

    An indication the regulator records as investigational or off-label does not meet that
    bar, so it drops to the grouping predicate rather than overclaiming.
    """
    if (status or "").upper() != "APPROVED":
        return bl.INDICATION_UNAPPROVED_PREDICATE
    if _VACCINE_TOKENS.search(drug_label or ""):
        return bl.PREVENTION_PREDICATE
    return bl.INDICATION_PREDICATE


def association_edges(
    pair: dict, review: StatementReviewStore | None = None
) -> list[dict]:
    """One edge per ``SourceAssertion`` on an indication/contraindication pair."""
    review = review or default_review_store()
    subject = pv.assoc_drug_id(pair)
    obj = pv.assoc_disease_id(pair)
    if not subject or not obj:
        return []

    relationship = (pair.get("relationship_type") or "INDICATION").upper()
    pair_confidence = pair.get("confidence") or {}
    pair_aggregates = {
        "medic_pair_confidence": pair_confidence.get("overall"),
        "medic_pair_n_assertions": pair_confidence.get("n_assertions")
        or len(pv.assoc_assertions(pair)) or "",
        "medic_pair_reliability": pair.get("reliability") or "",
        "medic_pair_jurisdictions": sorted(pv.assoc_jurisdictions(pair)),
        "medic_pair_authorities": sorted(pv.assoc_authorities(pair)),
    }

    built = []
    for assertion in pv.assoc_assertions(pair):
        if _withheld(assertion):
            continue
        regulatory = assertion.get("regulatory_status") or {}
        evidence = assertion.get("evidence") or {}
        claim = assertion.get("assertion") or {}
        drug_mention = assertion.get("drug") or {}
        disease_mention = assertion.get("disease") or {}
        agent = claim.get("agent") or {}
        confidence = claim.get("confidence") or {}

        status = regulatory.get("status") or evidence.get("approval_status") or ""
        predicate = (
            bl.CONTRAINDICATION_PREDICATE if relationship == "CONTRAINDICATION"
            else _indication_predicate(status, pv.assoc_drug_label(pair))
        )
        source = assertion.get("source") or regulatory.get("source") or ""
        authority = regulatory.get("authority") or ""
        primary = bl.primary_knowledge_source(regulatory.get("source") or source, authority)
        document = assertion.get("document") or ""
        spans = assertion.get("spans") or []
        index = claim.get("span_index")
        span = spans[index] if isinstance(index, int) and 0 <= index < len(spans) else None
        span_role = (span or {}).get("role") or ""

        # The span the extraction read, not the evidence snippet. The character offsets are
        # computed against this string at merge, and Biolink defines them as offsets into
        # `supporting_text` — so shipping a different string made them wrong for 1,843 edges.
        # The snippet remains the fallback for records that anchor to no span.
        text, truncated = _supporting_text(
            (span or {}).get("text") or evidence.get("snippet")
            or claim.get("input_value") or ""
        )

        edge = {
            "id": edge_id(subject, predicate, obj, primary, document),
            "subject": subject,
            "predicate": predicate,
            "object": obj,
            "primary_knowledge_source": primary,
            "aggregator_knowledge_source": [bl.AGGREGATOR],
            "knowledge_level": bl.KNOWLEDGE_LEVELS.get(predicate, "not_provided"),
            "agent_type": bl.agent_type(claim.get("method") or "",
                                        agent.get("agent_type") or ""),

            # verbatim source strings — standard Biolink slots, no namespacing needed
            "original_subject": drug_mention.get("original_literal") or "",
            "original_object": disease_mention.get("original_literal") or "",
            "supporting_text": text,
            "supporting_text_section_type": span_role,
            "subject_location_in_text": _offsets(drug_mention, text),
            "object_location_in_text": _offsets(disease_mention, text),
            "publications": _publications([evidence.get("reference") or ""]),
            "has_evidence": [bl.ECO_REGULATORY_LABEL]
            if (evidence.get("source_type") or "").upper() == "REGULATORY" else [],
            # Only on an indication. Biolink defines this slot as approval *for treating the
            # object*, so on a `contraindicated_in` edge it asserts the inverse of the claim —
            # all 2,978 contraindications shipped stamped `fda_approved_for_condition`. The
            # drug's approval status is not lost: it stays on `medic_regulatory_status`, where
            # it means what it says.
            "clinical_approval_status": (
                bl.clinical_approval_status(status, authority)
                if relationship != "CONTRAINDICATION" else ""
            ),
            "has_confidence_score": confidence.get("overall"),

            # source and document
            "medic_source": source,
            "medic_document": document,
            "medic_jurisdiction": assertion.get("jurisdiction")
            or evidence.get("jurisdiction") or "",
            "medic_authority": authority,
            "medic_source_role": regulatory.get("source_role") or "",
            "medic_regulatory_status": status,
            "medic_approval_status_raw": evidence.get("approval_status") or "",
            "medic_document_url": regulatory.get("regulatory_document_url")
            or evidence.get("reference") or "",
            "medic_source_document_url": regulatory.get("source_document_url")
            or evidence.get("source_document_url") or "",
            "medic_approval_date": regulatory.get("approval_date")
            or evidence.get("approval_date") or "",
            "medic_setid": regulatory.get("setid") or evidence.get("setid") or "",
            "medic_application_number": regulatory.get("application_number") or "",
            "medic_reference": evidence.get("reference") or "",
            "medic_reference_title": evidence.get("reference_title") or "",
            "medic_withdrawn": (status or "").upper() == "WITHDRAWN" or "",

            # claim
            "medic_supporting_text_truncated": truncated or "",
            "medic_explanation": evidence.get("explanation") or "",
            "medic_trigger_cue": claim.get("trigger_cue") or "",
            "medic_trigger_span": claim.get("trigger_span") or "",
            # Whether negation was FOUND, not whether the check ran. `negation_scope` is the
            # list of spans the check looked at, which is non-empty on essentially every edge —
            # so this slot read `true` on 12,694 of 12,694, meaning a reader taking it at its
            # name would conclude every claim in MeDIC is negated.
            "medic_negation_detected": ("negated_inversion" in (claim.get("flags") or [])) or "",
            "medic_assertion_flags": list(claim.get("flags") or []),
            "medic_assertion_method": claim.get("method") or "",
            "medic_agent_name": agent.get("agent_name") or "",
            "medic_agent_version": agent.get("agent_version") or "",
            "medic_tool": claim.get("tool") or "",
            "medic_tool_version": claim.get("tool_version") or "",
            "medic_support": evidence.get("support") or "",
            "medic_evidence_source": evidence.get("evidence_source") or "",

            # confidence
            "medic_confidence_subject": confidence.get("subject"),
            "medic_confidence_object": confidence.get("object"),
            "medic_confidence_relationship": confidence.get("relationship"),
            "medic_confidence_overall": confidence.get("overall"),
            "medic_confidence_basis": confidence.get("basis") or "",
            "medic_reliability": _assertion_reliability(pair, assertion, review),

            **_mention_properties(drug_mention, "subject"),
            **_mention_properties(disease_mention, "object"),
            **pair_aggregates,
        }

        if relationship == "CONTRAINDICATION":
            edge["medic_is_allergen"] = pair.get("is_allergen") or ""
            edge["medic_is_diagnostic_agent"] = pair.get("is_diagnostic_agent") or ""

        built.append(_clean(edge))
    return built


# ---------------------------------------------------------------------------
# Research associations
# ---------------------------------------------------------------------------
#: Canonical source name -> infores, for research evidence that carries a ``Source`` object.
_RESEARCH_SOURCES = {
    "CURE-ID": "infores:cure-id",
    "CUREID": "infores:cure-id",
    "PUBMED": "infores:pubmed",
    "CLINICALTRIALS.GOV": "infores:clinicaltrials",
}
_TRIAL_PHASES = {"PHASE_I", "PHASE_II", "PHASE_III", "PHASE_IV"}


def _research_predicate(evidence: dict) -> str:
    """Chosen from the recorded research phase, not from the source's name.

    ``CASE_REPORT`` means a patient actually took the drug for the condition, which is
    exactly what Biolink's ``applied to treat`` reports; a numbered trial phase means a
    clinical trial was run; anything else is a study without demonstrated efficacy.
    """
    phase = (evidence.get("max_research_phase") or "").upper()
    if phase == "CASE_REPORT":
        return bl.RESEARCH_APPLIED_PREDICATE
    if phase in _TRIAL_PHASES or evidence.get("study_status"):
        return bl.RESEARCH_TRIAL_PREDICATE
    return bl.RESEARCH_STUDIED_PREDICATE


def _research_source(evidence: dict) -> str:
    name = ((evidence.get("source") or {}).get("name") or "").upper()
    if name in _RESEARCH_SOURCES:
        return _RESEARCH_SOURCES[name]
    reference = evidence.get("reference") or ""
    if reference.split(":", 1)[0].upper() in ("PMID", "PMC"):
        return "infores:pubmed"
    if "cure.ncats" in reference or "cureid" in reference.lower():
        return "infores:cure-id"
    return bl.MEDIC_CURATION


def research_edges(
    record: dict, review: StatementReviewStore | None = None
) -> list[dict]:
    """Research edges for one association, one per (predicate, knowledge source) group.

    Several evidence items citing the same kind of study from the same source are one claim
    with several citations — which is what Biolink's multivalued ``publications`` is for —
    so they are not fanned out into near-identical edges.
    """
    subject = record.get("drug_id") or ""
    obj = record.get("disease_id") or ""
    if not subject or not obj:
        return []

    groups: dict[tuple[str, str], list[dict]] = {}
    for evidence in pv.assoc_evidence(record):
        groups.setdefault(
            (_research_predicate(evidence), _research_source(evidence)), []
        ).append(evidence)

    review = review or default_review_store()
    reliability = score_reliability(
        record, StatementType.RESEARCH_ASSOCIATION,
        review_status=review.status(record)).value

    built = []
    for (predicate, primary), items in sorted(groups.items()):
        first = items[0]
        text, truncated = _supporting_text(first.get("snippet") or "")
        phases = [e.get("max_research_phase") or "" for e in items]
        urls = [
            e.get("reference") or "" for e in items
            if (e.get("reference") or "").startswith("http")
        ]
        edge = {
            "id": edge_id(subject, predicate, obj, primary, ""),
            "subject": subject,
            "predicate": predicate,
            "object": obj,
            "primary_knowledge_source": primary,
            "aggregator_knowledge_source": [bl.AGGREGATOR],
            "knowledge_level": bl.KNOWLEDGE_LEVELS.get(predicate, "not_provided"),
            "agent_type": bl.agent_type(agent_kind="AI_AGENT"),
            "publications": _publications(e.get("reference") or "" for e in items),
            "supporting_text": text,
            "max_research_phase": bl.research_phase(phases[0]),
            "clinical_approval_status": bl.clinical_approval_status(
                first.get("approval_status") or ""),
            "original_subject": first.get("original_drug_label") or "",
            "original_object": first.get("original_disease_label") or "",

            "medic_research_phase_raw": _uniq(phases),
            "medic_approval_status_raw": first.get("approval_status") or "",
            "medic_evidence_source": first.get("evidence_source") or "",
            "medic_study_status": first.get("study_status") or "",
            "medic_support": first.get("support") or "",
            "medic_explanation": first.get("explanation") or "",
            "medic_reference": first.get("reference") or "",
            "medic_reference_title": first.get("reference_title") or "",
            "medic_reference_url": urls[0] if urls else "",
            "medic_supporting_text_truncated": truncated or "",
            "medic_curation_status": record.get("curation_status") or "",
            "medic_reliability": reliability,
        }
        # A single raw phase reads better as a scalar than a one-element list.
        if len(edge["medic_research_phase_raw"]) == 1:
            edge["medic_research_phase_raw"] = edge["medic_research_phase_raw"][0]
        built.append(_clean(edge))
    return built


# ---------------------------------------------------------------------------
# Adverse events
# ---------------------------------------------------------------------------
#: Biolink separates a label-listed side effect from a spontaneously reported adverse event;
#: PVLens mines labels and FAERS collects reports, so they do not share a predicate.
_AE_PREDICATE_BY_SOURCE = {
    "PVLENS": bl.AE_LABEL_PREDICATE,
    "FAERS": bl.AE_REPORT_PREDICATE,
}


def adverse_event_edges(
    record: dict, review: StatementReviewStore | None = None
) -> list[dict]:
    """One edge per contributing adverse-event source."""
    subject = record.get("drug_id") or ""
    obj = record.get("adverse_event_hpo_id") or record.get("adverse_event_id") or ""
    if not subject or not obj:
        return []

    evidence = pv.assoc_evidence(record)
    first = evidence[0] if evidence else {}
    review = review or default_review_store()
    reliability = score_reliability(
        record, StatementType.ADVERSE_EVENT,
        review_status=review.status(record)).value

    built = []
    for source in record.get("sources") or []:
        predicate = _AE_PREDICATE_BY_SOURCE.get(
            (source or "").upper(), bl.AE_REPORT_PREDICATE)
        primary = bl.primary_knowledge_source(source)
        text, truncated = _supporting_text(first.get("snippet") or "")
        built.append(_clean({
            "id": edge_id(subject, predicate, obj, primary, source),
            "subject": subject,
            "predicate": predicate,
            "object": obj,
            "primary_knowledge_source": primary,
            "aggregator_knowledge_source": [bl.AGGREGATOR],
            "knowledge_level": bl.KNOWLEDGE_LEVELS.get(predicate, "not_provided"),
            "agent_type": bl.agent_type(agent_kind="PIPELINE"),
            "publications": _publications([first.get("reference") or ""]),
            "supporting_text": text,
            "original_object": record.get("adverse_event_label") or "",

            "medic_source": source,
            "medic_jurisdiction": first.get("jurisdiction") or "",
            "medic_label_section": record.get("label_section") or "",
            "medic_frequency": record.get("frequency") or "",
            "medic_severity": record.get("severity") or "",
            "medic_supporting_text_truncated": truncated or "",
            "medic_reliability": reliability,
        }))
    return built


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
def build_edges(
    indications: list[dict],
    contraindications: list[dict],
    research: list[dict],
    adverse_events: list[dict],
    review: StatementReviewStore | None = None,
) -> tuple[list[dict], dict[str, str]]:
    """All edges, sorted deterministically, plus the labels of every endpoint they touch.

    The endpoint map feeds ``nodes.build_nodes`` so unknown endpoints become stubs and the
    graph stays referentially closed.
    """
    review = review or default_review_store()
    built: list[dict] = []
    referenced: dict[str, str] = {}

    def note(curie: str, label: str) -> None:
        if curie and not referenced.get(curie):
            referenced[curie] = label or ""

    for pair in [*indications, *contraindications]:
        built.extend(association_edges(pair, review))
        note(pv.assoc_drug_id(pair),
             pv.assoc_drug_label(pair) or _source_literal(pair, "drug"))
        note(pv.assoc_disease_id(pair),
             pv.assoc_disease_label(pair) or _source_literal(pair, "disease"))

    for record in research:
        built.extend(research_edges(record, review))
        note(record.get("drug_id") or "", record.get("drug_label") or "")
        note(record.get("disease_id") or "", record.get("disease_label") or "")

    for record in adverse_events:
        edges_for_record = adverse_event_edges(record, review)
        built.extend(edges_for_record)
        note(record.get("drug_id") or "", record.get("drug_label") or "")
        for edge in edges_for_record:
            note(edge["object"], record.get("adverse_event_label") or "")

    built.sort(key=lambda e: (e["subject"], e["predicate"], e["object"],
                             e["primary_knowledge_source"], e.get("medic_document", ""),
                             e["id"]))
    return built, referenced

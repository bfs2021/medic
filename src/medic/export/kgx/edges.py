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

**Every edge is an instance of a Biolink association class.** The core of each record is
built by instantiating the pydantic class :data:`medic.export.kgx.biolink.ASSOCIATION_CLASS`
names for its predicate and dumping it, so the class fixes ``category``, enum slots reject
values outside their permissible set, and a property the class does not declare cannot be
written at all (see :func:`_build`). The ``medic_`` extension layer is attached afterwards
and is kept to what a consumer needs on the edge itself; the rest of MeDIC's provenance is
reachable through the ``MEDICNE:`` mention ids (I-8) and the products.

**Where the source record lives.** The document a claim was read from — its CURIE, the
landing page, the PDF — is Biolink's ``source_record_urls``, a property of
``RetrievalSource``. So every edge carries one nested ``sources`` entry for its primary
knowledge source holding those references. That is the one nested payload Biolink defines
for an association's provenance, and it replaces six flat ``medic_*`` URL properties.

Predicate choice is driven by *recorded data* — regulatory status, research phase, source —
never by a source's name. That mirrors the rule ``medic.reliability`` already follows: the
idiosyncrasies of DailyMed vs GRLS vs CDE must not leak into the semantics.
"""

from __future__ import annotations

import re
import uuid

from pydantic import ValidationError

from medic import product_view as pv
from medic.curie_utils import get_prefix
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


def _empty(value) -> bool:
    """``None`` or an empty str/list/dict — the shapes a builder uses for "not set".

    Written as a type check rather than ``v in ("", [], {}, None)``: that form runs ``==``
    against the value, which raises on a pandas/numpy object ("truth value of a Series is
    ambiguous"). Everything reaching the builders comes out of ``yaml.safe_load`` and is a
    builtin, but this holds either way.
    """
    return value is None or (isinstance(value, (str, list, dict)) and not value)


def _clean(edge: dict) -> dict:
    return {k: v for k, v in sorted(edge.items()) if not _empty(v)}


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
    """The resolution join key for one side of an edge (spec §2.3).

    Only the ``MEDICNE:`` id ships. The grounding quality, applied rules and flags it used
    to carry beside it are the SSSOM row's own columns, reachable through this key — which
    is the I-8 design: the chain is replayable from the decision store, not duplicated onto
    every edge that cites it.
    """
    return {f"medic_{role}_mention_id": mention.get("id") or ""}


def _publications(references) -> list[str]:
    return _uniq(
        ref for ref in references
        if isinstance(ref, str) and ref.split(":", 1)[0].upper() in PUBLICATION_PREFIXES
    )


def _supporting_text(text: str) -> tuple[str, bool]:
    """The quoted span, and whether it was cut."""
    if not text:
        return "", False
    if len(text) <= MAX_SUPPORTING_TEXT:
        return text, False
    return text[:MAX_SUPPORTING_TEXT], True


def _sources(resource_id: str, record_references) -> list[dict]:
    """The edge's primary ``RetrievalSource``, carrying the source record references.

    Biolink's ``source_record_urls`` ("a URL linking to a specific web page or document
    provided by the source, that contains a record of the knowledge expressed in the Edge")
    has domain ``retrieval source``, so the document CURIE and URLs live on a nested source
    entry rather than flat on the edge. Only the primary source is listed: the aggregator is
    already stated by ``aggregator_knowledge_source``, and repeating it here would add a
    second object to 70,000 edges to say nothing new. Omitted entirely when there is no
    record reference, so the entry never restates ``primary_knowledge_source`` alone.

    ``id`` is the resource id. The class requires one, the entries are inlined, and the
    retrieval source *is* that resource as consulted for this edge.
    """
    references = _uniq(record_references)
    if not references:
        return []
    return [{
        "id": resource_id,
        "resource_id": resource_id,
        "resource_role": "primary_knowledge_source",
        "source_record_urls": references,
    }]


def _build(record: dict) -> dict:
    """Construct one edge: the Biolink core through its association class, then the rest.

    The un-namespaced keys of ``record`` are handed to the pydantic class
    :data:`bl.ASSOCIATION_CLASS` names for the predicate. The class is strict — an
    undeclared property, an enum value outside the permissible set, a list on a scalar slot
    or a missing required slot all raise — so nothing Biolink 4.4.6 does not define can
    reach the output. The class also stamps ``category``. The few Biolink slots the model
    defines but does not attach to the class (:data:`bl.OFF_CLASS_EDGE_SLOTS`) and the
    ``medic_`` extension layer are merged in after the dump.

    Two failures here raise and must keep raising: a ``ValidationError`` from the class,
    and an extension key missing from :data:`bl.EXTENSION_SLOTS`. Both are a bug in a
    builder, not bad data, and both are load-bearing for the property cull this module
    went through — if either were swallowed, a property could silently vanish from a record
    with nothing downstream to notice.
    """
    record = {k: v for k, v in record.items() if not _empty(v)}
    cls = bl.model_class(bl.association_category(record["predicate"]))
    extension = {k: v for k, v in record.items() if k.startswith(bl.EXTENSION_PREFIX)}
    # Only a slot *this* class lacks bypasses the constructor: `clinical_approval_status`
    # is on the contraindication class and goes through it there.
    off_class = {k: v for k, v in record.items()
                 if k in bl.OFF_CLASS_EDGE_SLOTS and k not in cls.model_fields}
    core = {k: v for k, v in record.items() if k not in extension and k not in off_class}
    unknown = set(extension) - set(bl.EXTENSION_SLOTS)
    if unknown:
        raise ValueError(f"extension properties not declared in EXTENSION_SLOTS: {unknown}")

    try:
        built = cls(**core).model_dump(mode="json", exclude_none=True)
    except ValidationError as exc:
        raise ValueError(f"edge {core.get('id')} is not a valid {cls.__name__}: {exc}") from exc
    return _clean({**built, **off_class, **extension})


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


#: Sex terms a label uses to restrict a claim. Biolink has a dedicated `sex_qualifier`,
#: which DAKP fills on 1,991 edges, so a population that names a sex belongs there as well
#: as in the general population slot — a consumer asking "which claims are sex-restricted?"
#: should not have to parse prose.
#: Matched on word boundaries, not as substrings. "men" occurs inside "postmenopausal" and
#: "management", both of which appear in real population phrases — a naive containment test
#: tags them male. The female alternatives are tried first so "women" is not read as "men".
#: The slot's range is the class ``biological sex``, so the value is its PATO term.
_SEX_PATTERNS = (
    (bl.SEX_QUALIFIER_TERMS["female"], re.compile(r"(?i)\b(women|woman|female|females)\b")),
    (bl.SEX_QUALIFIER_TERMS["male"], re.compile(r"(?i)\b(men|man|male|males)\b")),
)


def _sex_qualifier(population: str) -> str:
    for term, pattern in _SEX_PATTERNS:
        if pattern.search(population or ""):
            return term
    return ""


def _mondo_context(context_ids: list[str]) -> str:
    """The first MONDO term among the grounded contexts, or ``""``.

    The *first MONDO* rather than "the first id, if it happens to be MONDO". Both rules
    guarantee a conformant slot, but the positional one blanks the slot on 481 edges of the
    95,170-edge build that do have a MONDO context sitting behind a CUI — the ids arrive in
    the order the label named the contexts, which carries no priority, so reading position 0
    as the preferred context would be inventing a meaning it does not have.

    ``get_prefix`` rather than a ``split(":")``: the project's CURIE contract, and it also
    makes free text fall out here instead of being matched as a prefix (it returns ``""``
    for a string that is not a CURIE).
    """
    for context_id in context_ids:
        if get_prefix(context_id) == bl.DISEASE_CONTEXT_PREFIX:
            return context_id
    return ""


def _claim_qualifiers(evidence: dict) -> dict:
    """Map the label's recorded context onto Biolink qualifier slots (#90).

    Every indication edge used to be a bare triple, so context was either promoted to the
    object (#89's 13.1%) or dropped — and the audit scores a dropped context as correct, so
    that half was never counted. Biolink carries these on the `treats` association and DAKP
    already populates them on 5,686 of its 16,655 treats edges.

    `disease_context_qualifier` is single-valued, so only one context goes on it and the
    full set goes on the namespaced `medic_disease_context_ids`. Co-administration
    has no Biolink slot on this association, so it stays in the `medic_` extension layer
    rather than being invented as a bare property — the class constructor would reject it,
    correctly.

    `medic_disease_context` and `medic_disease_context_ids` stay **positionally aligned**,
    which is the contract `record_claim_context` writes in the products: the id at index *i*
    is the grounding of the name at index *i*, and `""` where that name did not ground. The
    ids used to be compacted here, which was harmless while the names were not on the edge;
    now that they are, compacting would silently re-pair every name after an ungrounded one
    with the wrong CURIE.
    """
    contexts = [c for c in (evidence.get("disease_context") or []) if c]
    context_ids = list(evidence.get("disease_context_ids") or [])
    population = (evidence.get("population_context") or "").strip()
    return {
        # Biolink types this slot `disease` — a class, not a string — so only a MONDO term
        # goes on it (#110). A UMLS or HP id passes the constructor, because both are on the
        # class's `id_prefixes`, and free text passes because the field is typed `str`; both
        # satisfy the slot's name rather than its range. Nothing is lost by the narrowing:
        # every grounded id is on `medic_disease_context_ids` and the label's own wording is
        # on `medic_disease_context`, so an ungrounded context is still readable and still
        # searchable as text from the export itself. `_mondo_context` skips the `""`
        # placeholders by construction, since they have no prefix.
        "disease_context_qualifier": _mondo_context(context_ids),
        "medic_disease_context": contexts,
        "medic_disease_context_ids": context_ids,
        "population_context_qualifier": population,
        "temporal_context_qualifier": (evidence.get("temporal_context") or "").strip(),
        "sex_qualifier": _sex_qualifier(population),
        "medic_co_administered_with": list(evidence.get("co_administered_with") or []),
        # Kept because two relations share `treats` and two share
        # `preventative_for_condition`; without this the label's finer distinction is lost.
        "medic_claim_relation": (
            (evidence.get("claim_predicate") or "").strip().lower()
            if (evidence.get("claim_predicate") or "").strip().lower() not in ("", "treats")
            else ""
        ),
    }


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


#: The label's relation -> the Biolink predicate. Two collapses are deliberate:
#: risk reduction *is* prevention, and maintenance of a treated state is still treatment of
#: the named condition. `medic_claim_relation` keeps the finer distinction the label drew,
#: so nothing is lost where Biolink has no separate slot.
_RELATION_PREDICATE = {
    "treats": bl.INDICATION_PREDICATE,
    "maintenance": bl.INDICATION_PREDICATE,
    "prevents": bl.PREVENTION_PREDICATE,
    "reduces_risk_of": bl.PREVENTION_PREDICATE,
    "diagnoses": bl.DIAGNOSTIC_PREDICATE,
}


def _indication_predicate(status: str, drug_label: str = "", relation: str = "") -> str:
    """Biolink reserves an asserted ``treats`` for approved or established use.

    An indication the regulator records as investigational or off-label does not meet that
    bar, so it drops to the grouping predicate rather than overclaiming.
    """
    if (status or "").upper() != "APPROVED":
        return bl.INDICATION_UNAPPROVED_PREDICATE
    # The label's own account of the relation, when the extraction recorded one (#99).
    if relation in _RELATION_PREDICATE:
        return _RELATION_PREDICATE[relation]
    # Fallback for rows with no recorded relation — which is every row until the
    # re-extraction lands. Dropping it would regress the vaccine edges already fixed.
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
            else _indication_predicate(
                status, pv.assoc_drug_label(pair),
                (evidence.get("claim_predicate") or "").strip().lower())
        )
        source = assertion.get("source") or regulatory.get("source") or ""
        authority = regulatory.get("authority") or ""
        primary = bl.primary_knowledge_source(regulatory.get("source") or source, authority)
        document = assertion.get("document") or ""
        spans = assertion.get("spans") or []
        index = claim.get("span_index")
        span = spans[index] if isinstance(index, int) and 0 <= index < len(spans) else None

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
            # No `description`: a regulatory assertion's explanation is the ingester's
            # boilerplate per source ("FDA-approved indication from DailyMed structured
            # product label"), one value across tens of thousands of edges. It is recorded
            # once in the metadata (`source_explanations`); research edges, whose
            # explanation is per claim, keep theirs.

            # the source record: document CURIE, landing page, PDF — on the retrieval source
            "sources": _sources(primary, [
                document,
                regulatory.get("regulatory_document_url") or evidence.get("reference") or "",
                regulatory.get("source_document_url")
                or evidence.get("source_document_url") or "",
            ]),

            # verbatim source strings — standard Biolink slots, no namespacing needed
            "original_subject": drug_mention.get("original_literal") or "",
            "original_object": disease_mention.get("original_literal") or "",
            "supporting_text": [text] if text else [],
            "subject_location_in_text": _offsets(drug_mention, text),
            "object_location_in_text": _offsets(disease_mention, text),
            "publications": _publications([evidence.get("reference") or ""]),
            "has_evidence": [bl.ECO_REGULATORY_LABEL]
            if (evidence.get("source_type") or "").upper() == "REGULATORY" else [],
            # Only on an indication. Biolink defines this slot as approval *for treating the
            # object*, so on a `contraindicated_in` edge it asserts the inverse of the claim —
            # all 2,978 contraindications shipped stamped `fda_approved_for_condition`. The
            # drug's approval status is not lost: it stays on `medic_approval_status_raw`,
            # where it means what it says.
            "clinical_approval_status": (
                bl.clinical_approval_status(status, authority)
                if relationship != "CONTRAINDICATION" else ""
            ),
            "has_confidence_score": confidence.get("overall"),

            # source — the pair invariant I-1 is checked on at the export boundary
            "medic_source": source,
            "medic_jurisdiction": assertion.get("jurisdiction")
            or evidence.get("jurisdiction") or "",
            "medic_approval_status_raw": status,
            "medic_approval_date": regulatory.get("approval_date")
            or evidence.get("approval_date") or "",

            # quality
            "medic_supporting_text_truncated": truncated or "",
            "medic_assertion_flags": list(claim.get("flags") or []),
            "medic_reliability": _assertion_reliability(pair, assertion, review),

            **_claim_qualifiers(evidence),
            **_mention_properties(drug_mention, "subject"),
            **_mention_properties(disease_mention, "object"),
            **pair_aggregates,
        }

        if relationship == "CONTRAINDICATION":
            edge["medic_is_allergen"] = pair.get("is_allergen") or ""
            edge["medic_is_diagnostic_agent"] = pair.get("is_diagnostic_agent") or ""

        built.append(_build(edge))
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
        # A citable reference is a publication; a bare URL is the record the claim was read
        # from and belongs on the retrieval source.
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
            "description": first.get("explanation") or "",
            "sources": _sources(primary, urls),
            "publications": _publications(e.get("reference") or "" for e in items),
            "supporting_text": [text] if text else [],
            "max_research_phase": bl.research_phase(phases[0]),
            "clinical_approval_status": bl.clinical_approval_status(
                first.get("approval_status") or ""),
            "original_subject": first.get("original_drug_label") or "",
            "original_object": first.get("original_disease_label") or "",

            "medic_research_phase_raw": _uniq(phases),
            "medic_approval_status_raw": first.get("approval_status") or "",
            "medic_supporting_text_truncated": truncated or "",
            "medic_curation_status": record.get("curation_status") or "",
            "medic_reliability": reliability,
        }
        # A single raw phase reads better as a scalar than a one-element list.
        if len(edge["medic_research_phase_raw"]) == 1:
            edge["medic_research_phase_raw"] = edge["medic_research_phase_raw"][0]
        built.append(_build(edge))
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
        built.append(_build({
            "id": edge_id(subject, predicate, obj, primary, source),
            "subject": subject,
            "predicate": predicate,
            "object": obj,
            "primary_knowledge_source": primary,
            "aggregator_knowledge_source": [bl.AGGREGATOR],
            "knowledge_level": bl.KNOWLEDGE_LEVELS.get(predicate, "not_provided"),
            "agent_type": bl.agent_type(agent_kind="PIPELINE"),
            "publications": _publications([first.get("reference") or ""]),
            "supporting_text": [text] if text else [],
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

    # The id is minted from (subject, predicate, object, source, document), so sorting on
    # it last keeps two builds of unchanged products byte-identical.
    built.sort(key=lambda e: (e["subject"], e["predicate"], e["object"],
                             e["primary_knowledge_source"], e["id"]))
    return built, referenced

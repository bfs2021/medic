"""Statement typing + a lightweight, source-uniform reliability score.

The soft-launch enabler (see specs/2026-07-26-soft-launch-reliability-design.md): give a
downstream consumer a one-line way to import just the part of MeDIC that is already
fairly trustworthy. Two orthogonal knobs:

* :class:`StatementType` — *what kind* of claim a record makes. ``CORE_TYPES`` (drug
  approval + indication + contraindication) are the regulatory backbone; adverse events
  and research associations are non-core.
* :class:`ReliabilityTier` — *how trustworthy* the record is, scored the **same way for
  every source** from normalized per-record signals (never by source name), so the
  idiosyncrasies of DailyMed vs GRLS vs CDE don't leak into the score. The score is the
  worst (most conservative) of a few independent gates, each mapping one failure-mode
  family (FAILURE_MODES.md) to a tier:

  1. **grounding** — is the entity resolution solid? (FM §grounding)
  2. **recognition** — was the entity itself correctly recognised in the source text?
     (the Mention's ExtractionStep flags: hallucination / truncated / coreference; FM §5)
  2b. **assertion** — does the source actually claim this *relation*, and not negate it?
     (the association's Assertion: confidence + negated_inversion / over_extraction /
     wrong_section / wrong_pairing; FM §4-5, via medic.validation.extraction_fidelity).
     Kept separate from recognition on purpose: an entity can be recognised perfectly while
     the asserted relation is wrong (the VITAMIN A -> hyperthyroidism case).

  **Not every flag these gates read has a machine emitter, and the difference matters when
  reading a tier.** Emitted today: `truncated_snippet` (recognition), and
  `negated_inversion` / `over_extraction` / `wrong_section` / `wrong_pairing` (assertion —
  the last only by the enabler-partner class rule below, not in general). Curator-supplied
  only: `hallucination`, `coreference_ambiguity`, `scope_narrowed` — no deterministic
  detector exists for them, and inferring them from lexical entailment would mislabel
  synonymy as fabrication.
  2c. **enabler** — is the drug an ingredient that never owns a treatment indication
     (beta-lactamase inhibitors, cilastatin, cobicistat; `mappings/enabler_partners.tsv`)?
     An INDICATION on one is the combination partner's claim credited to the wrong active
     moiety (FM §5.4, #88) → LOW. Contraindications are untouched: they are real claims
     about the component. So a HIGH tier means "nothing we can detect is wrong",
  not "a human checked it"; that stronger claim is what `review_status: CONFIRMED` in
  `mappings/statement_review.tsv` is for. Every one of these read zero across all 12,694
  assertions until the detectors above were wired in, which made two of the five gates
  decorative — see `_recognition_gate`.
  3. **translation** — did the name survive machine translation unreviewed? (FM §7)
  4. **provenance** — does the claim have *any* verifiable provenance (snippet, resolvable
     reference/URL, or registry application id)? Only *absence* is penalised — a direct
     document deep link is a source publishing convention, NOT a reliability signal, so
     sources that don't publish one (Orange Book, GRLS, CDE) are never structurally capped.

A gate returns ``None`` when it does not apply (a structured approval has no extraction
gate). **HIGH requires at least one of the quality gates above to have reached a verdict.**
The provenance gate alone cannot earn it: it passes on any one of five weak signals and
returned HIGH for 9,000 of 9,000 pairs when measured, so it discriminates nothing. This
used to read "the provenance gate always applies, so nothing reaches HIGH by absence of
signal", which was false in both halves — ``_worst`` over an all-abstaining list returned
HIGH, and the backstop did not backstop (#60).

**Two invariants (see the design spec §7):**

* *Every statement can reach HIGH on its own merits* — no gate caps a whole source below
  HIGH for a reason a good record can't overcome.
* *Human review mitigates all concerns* — a curator marking a statement ``review_status:
  CONFIRMED`` forces HIGH, overriding every automated gate (the statement-level twin of the
  per-decision hatches already honoured: grounding ``curated`` and translation ``OFFICIAL``).

The "mostly reliable" subset a consumer imports = ``statement_type in CORE_TYPES`` and
``reliability in {HIGH, MEDIUM}``. Curation (REVIEW.md) moves records upward automatically
on rebuild.
"""

from __future__ import annotations

import csv
import glob
import os
from collections import Counter
from enum import Enum

import typer
import yaml

from medic import product_view as pv
from medic.enabler_partners import enabler_class
from medic.validation.extraction_fidelity import assertion_negated, entailment_score

app = typer.Typer(add_completion=False)


class StatementType(str, Enum):
    DRUG_APPROVAL = "DRUG_APPROVAL"
    INDICATION = "INDICATION"
    CONTRAINDICATION = "CONTRAINDICATION"
    ADVERSE_EVENT = "ADVERSE_EVENT"
    RESEARCH_ASSOCIATION = "RESEARCH_ASSOCIATION"


#: The regulatory backbone a soft-launch consumer imports by default.
CORE_TYPES = frozenset({
    StatementType.DRUG_APPROVAL,
    StatementType.INDICATION,
    StatementType.CONTRAINDICATION,
})


class ReliabilityTier(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    EXCLUDED = "EXCLUDED"  # do not import (unresolved / hallucinated / inverted)


_TIER_ORDER = {
    ReliabilityTier.EXCLUDED: 0,
    ReliabilityTier.LOW: 1,
    ReliabilityTier.MEDIUM: 2,
    ReliabilityTier.HIGH: 3,
}
RELIABLE_TIERS = frozenset({ReliabilityTier.HIGH, ReliabilityTier.MEDIUM})


def _worst(tiers: list[ReliabilityTier | None]) -> ReliabilityTier:
    """The most conservative tier among those that reached a verdict.

    The identity element is LOW, not HIGH. HIGH is what ``min`` wants arithmetically — the
    top of the lattice, so it disappears under the fold — and it is the opposite of what a
    conservative score wants: it means "no gate had anything to say, therefore excellent"
    (#60). Every in-tree caller guards against the empty case already, so this is a floor
    rather than a behaviour change, but the floor is the point.
    """
    applicable = [t for t in tiers if t is not None]
    if not applicable:
        return ReliabilityTier.LOW
    return min(applicable, key=lambda t: _TIER_ORDER[t])


def _aggregate_pair(tiers: list[ReliabilityTier]) -> ReliabilityTier:
    """Fold per-assertion tiers into the pair's tier: the strongest attestation wins.

    The counterpart to :func:`_worst`, and deliberately its opposite. ``_worst`` is right
    *within* one assertion — a claim is only as good as its weakest link, and each gate
    there asks a different way the same claim could be wrong. Across assertions it inverts
    the meaning: three regulators independently stating the same drug-disease relationship
    is the strongest signal MeDIC has, and scoring the pair by whichever read worst turned
    corroboration into a liability (#61). Measured over the products, 82.2% of
    single-source pairs reached HIGH against 0% of four-source pairs.

    **Adding an attestation must never lower a pair's tier.** That is why this is `max` and
    not something cleverer: a "best, floored one tier above the worst" rule reads as more
    cautious, but a new bad attestation drags the floor down and demotes the pair — the
    exact property this issue exists to remove. A weak attestation is still visible per
    edge, where ``export/kgx/edges.py`` scores each one on its own.
    """
    return max(tiers, key=lambda t: _TIER_ORDER[t]) if tiers else ReliabilityTier.LOW


# ---------------------------------------------------------------------------
# Statement typing
# ---------------------------------------------------------------------------
def classify_statement(record: dict) -> StatementType:
    """Classify a MeDIC product/kb record into a :class:`StatementType`."""
    rel = (record.get("relationship_type") or "").upper()
    if rel == "CONTRAINDICATION":
        return StatementType.CONTRAINDICATION
    if rel == "INDICATION":
        return StatementType.INDICATION
    if record.get("meddra_id") or record.get("adverse_event") or record.get("reaction"):
        return StatementType.ADVERSE_EVENT
    # Research associations carry a curation lifecycle / literature provenance.
    if record.get("deep_research_used") or record.get("curation_status") or record.get("pmid"):
        return StatementType.RESEARCH_ASSOCIATION
    # A Drug record carries an `identity` Mention (+ `approvals`); an approved one is a
    # DRUG_APPROVAL, an identity-only one (e.g. EveryCure) is still a drug statement.
    if record.get("approvals"):
        return StatementType.DRUG_APPROVAL
    if record.get("drug_id") and record.get("disease_id"):
        return StatementType.RESEARCH_ASSOCIATION
    if record.get("identity") is not None:
        return StatementType.DRUG_APPROVAL
    return StatementType.RESEARCH_ASSOCIATION


# ---------------------------------------------------------------------------
# Reliability gates (each returns a tier, or None when not applicable)
# ---------------------------------------------------------------------------
# Grounding qualities that are exact (or curator-asserted) — trustworthy regardless of
# the numeric confidence the rule weight happens to carry.
_EXACT_QUALITIES = frozenset({
    "curated", "lexical_exact", "lexical_exact_normalized", "asserted_exact",
    "deprecated_replacement",
})


def _steps_of(record: dict, category: str) -> list[dict]:
    """Collect transformation steps of a category from a record's mention(s).

    A Drug carries its identity trail on ``mention``; an IndicationAssociation carries the
    inlined disease trail on ``disease`` (the drug side is a light DrugRef with no trail).
    """
    out: list[dict] = []
    # A Drug carries `identity`; an on-label pair carries BOTH entities as inlined Mentions on
    # each of its source assertions, so a pair's resolution quality reflects every source that
    # attests it. `assoc_mentions` hides which shape the record has.
    for m in pv.assoc_mentions(record):
        resolution = m.get("resolution")
        pipeline = resolution.get("pipeline", []) if isinstance(resolution, dict) else []
        for s in pipeline or []:
            if isinstance(s, dict) and s.get("category") == category:
                out.append(s)
    return out


def _confidence_or_none(raw, basis) -> float | None:
    """A step's confidence as a float, or ``None`` when nothing actually measured it.

    ``ConfidenceBasis.DETERMINISTIC`` is the one case where an absent value legitimately
    implies 1.0 — the schema defines it as "the step cannot be wrong" (an identity
    normalization, a verbatim read of a structured field). Every other absence is an
    unknown, and reading it as 1.0 is how a record with no measurement scored HIGH (#60).
    """
    if raw is None:
        return 1.0 if (basis or "").upper() == "DETERMINISTIC" else None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


#: The only mapping predicate that asserts the transformed string still denotes the same
#: concept. `skos:closeMatch` and `skos:broadMatch` say, in the vocabulary's own terms, that
#: it does not — a salt stripped to its parent moiety or a qualifier dropped from a disease
#: name lands on a *neighbouring* concept, deliberately.
_IDENTITY_PREDICATE = "skos:exactmatch"


def _caps_at_medium(obj: dict) -> bool:
    """Does this grounding's own predicate say it is not an identity match?

    Certainty alone cannot carry this. `salt_ester_strip` is scored 0.90 and the threshold
    below reads `conf >= 0.9` as HIGH, so a closeMatch has been reaching the top tier
    although the comment two lines down says a salt match should be MEDIUM — a boundary
    collision, 174 steps' worth while the rule only fired when the index lacked the salt.
    PR #102's tier 0 makes the same rule fire for every salt that *does* have an entry
    (617 drug nodes), which would have promoted all of them. Reading the predicate fixes the
    cause rather than nudging the number: how confident we are *within* a kind of match is a
    separate question from which kind it is.

    A missing predicate means the record predates the field, not that the match was exact,
    so it is left to the certainty thresholds rather than demoted on a guess.
    """
    pred = str(obj.get("predicate_id") or obj.get("mapping_predicate") or "").strip().lower()
    return bool(pred) and pred != _IDENTITY_PREDICATE


def _grounding_tier_from_step(step: dict) -> ReliabilityTier | None:
    """Reliability tier from a GroundingStep (the transformation-provenance model)."""
    quality = (step.get("quality") or "").lower()
    grounded_id = step.get("output_value")
    if quality == "unresolved" or not grounded_id:
        return ReliabilityTier.EXCLUDED
    # The source handed us the id and MeDIC made no decision — there is no matching evidence
    # to audit, so it cannot claim HIGH however confident the source seemed.
    if quality == "source_asserted":
        return ReliabilityTier.MEDIUM
    if quality in _EXACT_QUALITIES:
        return ReliabilityTier.HIGH
    conf = _confidence_or_none(step.get("confidence"), step.get("confidence_basis"))
    if conf is None:
        # Inexact quality and nothing measured it. Defaulting to 1.0 made "we never scored
        # this" indistinguishable from "we scored this perfectly" (#60). We do know the
        # match was inexact, so MEDIUM, not HIGH and not LOW.
        return ReliabilityTier.MEDIUM
    if conf >= 0.9:
        return ReliabilityTier.MEDIUM if _caps_at_medium(step) else ReliabilityTier.HIGH
    if conf >= 0.7:
        return ReliabilityTier.MEDIUM
    return ReliabilityTier.LOW


# Regulatory authorities whose source strings are native English/Latin (no machine
# translation): a drug approved via any of these has its identity anchored without MT.
_NATIVE_AUTHORITIES = frozenset({"FDA", "EMA", "PMDA", "CDSCO"})


def _has_native_approval(record: dict) -> bool:
    """Is the claim anchored by a non-translated (native-script) authority?

    Reads a Drug's ``approvals`` **and** an association's authorities: an FDA-backed
    indication must not be capped merely because the drug's inlined identity trail happens
    to come from a machine-translated zh/ru source.

    The association side goes through :func:`product_view.assoc_authorities`. It used to read
    ``record["regulatory_status"]`` directly, a pair-level slot the provenance re-model
    removed — regulatory status now lives on ``assertions[].regulatory_status``, singular. So
    this returned False for every pair in the products and the escape hatch never opened. It
    did not bite only because the two translated sources (China, Russia) are drug-list-only,
    which is also why no test caught it. ``product_view`` exists precisely so a read like this
    cannot go stale again.
    """
    if any(isinstance(a, dict) and (a.get("authority") or "") in _NATIVE_AUTHORITIES
           for a in pv.approvals(record)):
        return True
    return bool(pv.assoc_authorities(record) & _NATIVE_AUTHORITIES)


def _grounding_tier(grounding: dict | None) -> ReliabilityTier | None:
    if not grounding:
        return None
    quality = (grounding.get("grounding_quality") or "").lower()
    grounded_id = grounding.get("grounded_id") or grounding.get("normalized_id")
    if quality == "unresolved" or not grounded_id:
        return ReliabilityTier.EXCLUDED
    if quality in _EXACT_QUALITIES:
        return ReliabilityTier.HIGH
    # Inexact (surgery / salt / formulation / transliteration / fuzzy): let the rule
    # weight decide — fuzzy edit-1 (~0.6) is LOW, salt/formulation (~0.8-0.9) is MEDIUM,
    # with a non-exactMatch predicate capped at MEDIUM however high the rule's certainty
    # (see :func:`_caps_at_medium`).
    conf = _confidence_or_none(
        grounding.get("confidence"), grounding.get("confidence_basis"))
    if conf is None:
        return ReliabilityTier.MEDIUM
    if conf >= 0.9:
        return ReliabilityTier.MEDIUM if _caps_at_medium(grounding) else ReliabilityTier.HIGH
    if conf >= 0.7:
        return ReliabilityTier.MEDIUM
    return ReliabilityTier.LOW


def _grounding_gate(record: dict) -> ReliabilityTier | None:
    # Prefer the transformation-provenance trail (mention/disease steps); fall back to the
    # flat grounding objects during the additive migration. Worst grounding step wins.
    gsteps = _steps_of(record, "GROUNDING")
    if gsteps:
        return _worst([_grounding_tier_from_step(s) for s in gsteps])
    tiers = [_grounding_tier(record.get(k)) for k in ("grounding", "disease_grounding")]
    return _worst(tiers) if any(t is not None for t in tiers) else None


def _first_evidence(record: dict) -> dict:
    """The first evidence row backing a record.

    Reads through ``product_view`` so this survived the flat-``evidence`` -> per-assertion
    move: evidence now lives on ``assertions[].evidence``, one row per source document.
    """
    ev = pv.assoc_evidence(record)
    return ev[0] if ev else {}


def _assertion_gate(record: dict, statement_type: StatementType) -> ReliabilityTier | None:
    """Is the *claim* supported by the source? (reads the association's Assertion)

    The claim-level counterpart to the grounding gate: negation inverts the claim, and a
    weakly-supported claim caps the tier. Entity *recognition* quality is not judged here —
    that is the Mention's ExtractionStep (see :func:`_recognition_gate`).
    """
    if statement_type not in (StatementType.INDICATION, StatementType.CONTRAINDICATION):
        return None
    # A pair carries one Assertion per source document. The pair is only as good as its worst
    # attestation: one source that inverted the claim taints the pair regardless of the others.
    claims = pv.assoc_claims(record)
    tiers: list[ReliabilityTier | None] = []
    for assertion in claims:
        flags = assertion.get("flags") or []
        # A negated INDICATION recorded positively is an inversion -> exclude.
        if statement_type == StatementType.INDICATION and "negated_inversion" in flags:
            return ReliabilityTier.EXCLUDED
        # Relation-level mis-extractions the source does not support.
        if {"over_extraction", "wrong_section", "wrong_pairing"} & set(flags):
            tiers.append(ReliabilityTier.LOW)
            continue
        score = assertion.get("confidence")
        if isinstance(score, dict):
            score = score.get("overall")
        if score is None:
            tiers.append(None)
            continue
        try:
            score = float(score)
        except (TypeError, ValueError):
            tiers.append(None)
            continue
        if score >= 0.5:
            tiers.append(ReliabilityTier.HIGH)
        elif score > 0.0:
            tiers.append(ReliabilityTier.MEDIUM)
        else:
            tiers.append(ReliabilityTier.LOW)
    if claims:
        return _worst(tiers) if any(x is not None for x in tiers) else None
    # Flat fallback: recompute from the raw label + section text.
    ev = _first_evidence(record)
    disease_label = (ev.get("original_disease_label") or "").strip()
    snippet = ev.get("snippet") or ""
    section = " ".join([
        snippet,
        str(record.get("indications_text") or ""),
        str(record.get("contraindications_text") or ""),
    ]).strip()
    if not disease_label or not section:
        return None  # structured / no verbatim text to check against
    # Polarity: a negated INDICATION is an inversion -> exclude (defense in depth).
    if statement_type == StatementType.INDICATION:
        neg, total, _ = assertion_negated(disease_label, section, head_fallback=False)
        if total and neg == total:
            return ReliabilityTier.EXCLUDED
    score = entailment_score(disease_label, section)
    if score >= 0.5:
        return ReliabilityTier.HIGH
    if score > 0.0:
        return ReliabilityTier.MEDIUM
    return ReliabilityTier.LOW


def _drug_labels(record: dict) -> set[str]:
    """Every name the record gives its drug: the pair label and each attestation's mention.

    The mention's ``original_literal`` is only consulted when it has no ``resolved_label``:
    the resolved name is the one the pair is keyed on, and a literal that resolved to
    something else (a combination product) must not be judged on its own spelling.
    """
    labels = {pv.assoc_drug_label(record)}
    mentions = [record.get("drug")] + [a.get("drug") for a in pv.assoc_assertions(record)]
    for m in mentions:
        if isinstance(m, dict):
            labels.add(m.get("resolved_label") or m.get("original_literal") or "")
    return {lbl for lbl in labels if lbl}


def _enabler_gate(record: dict, statement_type: StatementType) -> ReliabilityTier | None:
    """Is the drug an enabler partner that cannot own this indication? (#88, FM §5.4)

    Beta-lactamase inhibitors, cilastatin and cobicistat ship only alongside the active
    moiety they enable, so "avibactam treats pyelonephritis" is ceftazidime's claim credited
    to the wrong component of the label. LOW rather than EXCLUDED: the pair is right for the
    *product* and wrong for the *component*, which is a pairing fault, not a fabrication.
    Only INDICATIONs: a contraindication on cobicistat is a real claim about cobicistat.

    The merge stamps the same verdict as the ``wrong_pairing`` assertion flag (see
    ``on_label_merge._build_disease_provenance``), which :func:`_assertion_gate` already
    reads as LOW; this gate makes the rule hold for records scored without that flag.
    """
    if statement_type != StatementType.INDICATION:
        return None
    if any(enabler_class(lbl) for lbl in _drug_labels(record)):
        return ReliabilityTier.LOW
    return None


def _recognition_gate(record: dict) -> ReliabilityTier | None:
    """Was the entity itself correctly recognised in the source? (the Mention's ExtractionStep)

    Purely about the mention: a hallucinated string is unusable (EXCLUDED); a truncated or
    coreference-ambiguous span is usable but weaker.
    """
    tiers: list[ReliabilityTier | None] = []
    for step in _steps_of(record, "EXTRACTION"):
        flags = set(step.get("flags") or [])
        if "hallucination" in flags:
            tiers.append(ReliabilityTier.EXCLUDED)
        elif flags & {"truncated_snippet", "coreference_ambiguity"}:
            tiers.append(ReliabilityTier.MEDIUM)
    return _worst(tiers) if tiers else None


def _translation_gate(record: dict) -> ReliabilityTier | None:
    # Prefer the mention's TranslationStep; fall back to the flat translation object.
    tsteps = _steps_of(record, "TRANSLATION")
    if tsteps:
        # Anchored by a native-script approval -> the MT does not cap identity.
        if _has_native_approval(record):
            return None
        step = tsteps[0]
        status = (step.get("status") or "").upper()
        if status == "CONFIRMED":
            return ReliabilityTier.HIGH
        if not step.get("output_value"):
            return ReliabilityTier.LOW
        return ReliabilityTier.MEDIUM
    trans = record.get("translation")
    if not isinstance(trans, dict):
        return None  # English source
    # A merged drug carries ONE representative translation. If the drug is also approved via
    # a non-translated (native-script) source, its identity does not rest on the machine
    # translation, so that translation must not cap it — otherwise a US drug that happens to
    # also be registered in China/Russia would be wrongly dragged to MEDIUM.
    #
    # Same test as the step-trail branch above. It used to check the flat `approved_usa` /
    # `approved_europe` / … booleans, which SPEC §9 records as removed.
    if _has_native_approval(record):
        return None
    status = (trans.get("translation_status") or "").upper()
    if status == "OFFICIAL":
        return ReliabilityTier.HIGH
    if not trans.get("translation_value"):
        return ReliabilityTier.LOW  # registered but never translated -> won't ground
    return ReliabilityTier.MEDIUM  # machine translation, curator-unreviewed


def _attribution_gate(record: dict) -> ReliabilityTier | None:
    """Could this claim have been mis-attributed to one ingredient of a combination?

    For a multi-ingredient SPL the extraction runs once per ingredient over the same
    section text, and the prompt never names the ingredient, so every component carries the
    whole product's indications — esomeprazole for osteoarthritis out of "the naproxen
    component …", every amino acid in a parenteral-nutrition bag for ileus.

    The audit put a number on it: pairs supported *only* by combination labels are **65%
    wrong** (40 judged) against 19.5% for pairs with no combination-label support, and 1,101
    pairs (14%) sit in that slice at HIGH, because the lexical gates all pass — the disease
    name really is in the text (#88, #91).

    An interim gate, not the fix. Attributing the claim properly needs the extraction to
    name the ingredient it read; until then this says only "this attestation could be a
    fan-out", which is worth LOW rather than HIGH. Per assertion, so #61's
    max-across-assertions fold supplies the "unless a single-ingredient label or another
    regulator supports it" clause for free.

    Returns ``None`` when the count is absent, which is every non-DailyMed source and every
    record merged before it was recorded — an unknown must not read as a combination.
    """
    counts = [
        ev.get("label_ingredient_count") for ev in pv.assoc_evidence(record)
    ]
    usable = [c for c in counts if isinstance(c, int) and c > 0]
    if not usable:
        return None
    return ReliabilityTier.LOW if min(usable) > 1 else None


def _context_gate(record: dict) -> ReliabilityTier | None:
    """Is the object the population or setting rather than the treated condition?

    The largest single error class: 38 of 291 audited edges (13.1%), half of all MeDIC
    errors — eplerenone for acute MI out of "heart failure post-MI", posaconazole for AML
    out of "prophylaxis ... in patients with AML" (#89).

    The verdict is reached at ingest, where the sibling diseases from the same section are
    in hand; this gate only reads it. LOW rather than EXCLUDED: a preposition is weaker
    evidence than a deletion needs, and some of these objects are genuinely co-indicated.
    """
    for ev in pv.assoc_evidence(record):
        if ev.get("context_only_disease"):
            return ReliabilityTier.LOW
    return None


def _provenance_gate(record: dict) -> ReliabilityTier:
    """Is the claim's provenance verifiable *at all*? (never caps on link format)

    A statement is provenance-HIGH if it carries any checkable provenance — a supporting
    snippet, a resolvable reference/URL, or a registry application id. Only the *absence*
    of provenance is penalised (LOW). Crucially this does NOT require a direct document
    URL: whether a source publishes per-record deep links is a publishing convention, not
    a reliability signal, so Orange Book / GRLS / CDE are not structurally capped. Always
    applies (returns a tier, never ``None``) so a record with no provenance can never
    default to HIGH.

    Judged across **every** evidence row, worst-first, like the other gates. It used to read
    only ``assoc_evidence(record)[0]``, so a pair attested by five documents was scored on
    whichever one happened to sort first: an unverifiable DailyMed row could hide behind a
    well-referenced EMA one, or drag it down, depending purely on alphabetical order.
    """
    rows = pv.assoc_evidence(record) or [{}]
    application_numbers = pv.application_numbers(record)
    tiers = []
    for ev in rows:
        verifiable = bool(
            ev.get("snippet") or ev.get("source_document_url") or ev.get("reference")
            or application_numbers or ev.get("original_drug_id")
        )
        tiers.append(ReliabilityTier.HIGH if verifiable else ReliabilityTier.LOW)
    return _worst(tiers)


def _approval_gate(record: dict) -> ReliabilityTier | None:
    """A DRUG_APPROVAL must actually assert an approval, else it is not that statement."""
    approved_somewhere = pv.is_approved_anywhere(record)
    ev = _first_evidence(record)
    approved_ev = (ev.get("approval_status") or "").upper() == "APPROVED"
    return None if (approved_somewhere or approved_ev) else ReliabilityTier.EXCLUDED


# Statement-level human review — the escape hatch that makes "human review mitigates all
# concerns" literal. A curator's verdict overrides every automated gate, both ways:
# CONFIRM forces HIGH, REJECT forces EXCLUDED. Verdicts live in a hand-editable store
# (``mappings/statement_review.tsv``, keyed by statement id) — the same curation pattern
# as the SSSOM grounding/normalization and Babelon translation stores. Mirrors the
# per-decision hatches the gates already honour (grounding ``curated``, translation
# ``OFFICIAL``).
_CONFIRM_STATUSES = frozenset({"CONFIRMED", "OFFICIAL", "REVIEWED", "CURATED"})
_REJECT_STATUSES = frozenset({"REJECTED", "EXCLUDED", "WRONG"})


def statement_key(record: dict) -> str:
    """Stable id for a statement, used to key the review store.

    ``<drug_id>|<disease_id>|<TYPE>`` for disease-linked statements; ``<drug_id>|<TYPE>``
    for approvals. Uses the canonical (final normalized) ids.
    """
    st = classify_statement(record)
    drug = (pv.assoc_drug_id(record) or pv.drug_id(record)
            or record.get("drug_id") or "")
    disease = pv.assoc_disease_id(record) or record.get("disease_id") or ""
    if st == StatementType.DRUG_APPROVAL:
        return f"{drug}|{st.value}"
    return f"{drug}|{disease}|{st.value}"


def _review_verdict(record: dict, review_status: str = "") -> str:
    """Return 'confirm' / 'reject' / '' from an explicit status or the record's own field."""
    for raw in (review_status, record.get("review_status"),
                _first_evidence(record).get("review_status")):
        status = (raw or "").upper()
        if status in _REJECT_STATUSES:
            return "reject"
        if status in _CONFIRM_STATUSES:
            return "confirm"
    return ""


def score_reliability(
    record: dict,
    statement_type: StatementType | None = None,
    *,
    review_status: str = "",
) -> ReliabilityTier:
    """Reliability tier for a record.

    A human verdict wins first (CONFIRM → HIGH, REJECT → EXCLUDED); otherwise the tier is
    the worst of the applicable automated gates. Every statement runs the provenance gate,
    so nothing reaches HIGH by absence of signal. ``review_status`` is normally supplied by
    the caller from the review store (see :class:`StatementReviewStore`).
    """
    verdict = _review_verdict(record, review_status)
    if verdict == "reject":
        return ReliabilityTier.EXCLUDED
    if verdict == "confirm":
        return ReliabilityTier.HIGH
    st = statement_type or classify_statement(record)

    # A pair carries one assertion per attesting source. Score each in isolation and take
    # the strongest, so corroboration moves the tier the same direction as
    # `confidence.corroboration()` rather than against it (#61). Within one assertion the
    # gates still fold with `_worst`.
    assertions = pv.assoc_assertions(record)
    if len(assertions) > 1:
        scalars = {k: v for k, v in record.items() if k != "assertions"}
        return _aggregate_pair([
            _score_one({**scalars, "assertions": [a]}, st) for a in assertions
        ])
    return _score_one(record, st)


def _score_one(record: dict, st: StatementType) -> ReliabilityTier:
    """The automated gates for a single attestation (or a record with no assertions)."""
    # The gates that speak to whether the record is *right*: was the entity grounded, was
    # it recognised in the source, does the source assert this relation, did the name
    # survive translation. Each returns None when it has nothing to say.
    quality_gates = [
        _grounding_gate(record),
        _recognition_gate(record),
        _assertion_gate(record, st),
        _enabler_gate(record, st),
        _translation_gate(record),
        _attribution_gate(record),
        _context_gate(record),
    ]
    if st == StatementType.DRUG_APPROVAL:
        quality_gates.append(_approval_gate(record))
    provenance = _provenance_gate(record)

    tier = _worst([*quality_gates, provenance])
    if tier is ReliabilityTier.HIGH and all(g is None for g in quality_gates):
        # Only the provenance gate spoke, and it passes on any one of five weak signals —
        # a snippet, a URL, a reference, an application id, a source drug id. It was never
        # able to be the backstop this docstring claimed it was: a record carrying one
        # evidence field and nothing else cleared it and reached HIGH (#60). Absence of
        # signal is not evidence of quality, so cap it.
        return ReliabilityTier.LOW
    return tier


def is_reliable(record: dict, *, core_only: bool = True, review_status: str = "") -> bool:
    """Would a default soft-launch consumer import this record?"""
    st = classify_statement(record)
    if core_only and st not in CORE_TYPES:
        return False
    return score_reliability(record, st, review_status=review_status) in RELIABLE_TIERS


# ---------------------------------------------------------------------------
# Curated statement-review store (mappings/statement_review.tsv)
# ---------------------------------------------------------------------------
REVIEW_STORE_PATH = "mappings/statement_review.tsv"
REVIEW_COLUMNS = [
    "statement_key", "statement_type", "drug_id", "disease_id",
    "review_status", "reviewer", "comment",
]


class StatementReviewStore:
    """Hand-editable curator verdicts, keyed by :func:`statement_key`.

    A plain header-row TSV under ``mappings/`` (same curation pattern as the SSSOM/Babelon
    stores). ``review_status`` is ``CONFIRMED`` (→ HIGH) or ``REJECTED`` (→ EXCLUDED); rows
    survive rebuilds and win over the automated gates.
    """

    def __init__(self, path: str = REVIEW_STORE_PATH):
        self.path = path
        self._rows: dict[str, str] = {}

    def load(self) -> StatementReviewStore:
        self._rows.clear()
        if os.path.exists(self.path):
            with open(self.path, newline="") as fh:
                reader = csv.DictReader(
                    (ln for ln in fh if not ln.startswith("#")), delimiter="\t"
                )
                for r in reader:
                    key = (r.get("statement_key") or "").strip()
                    if key:
                        self._rows[key] = (r.get("review_status") or "").strip()
        return self

    def status(self, record: dict) -> str:
        return self._rows.get(statement_key(record), "")


_DEFAULT_REVIEW_STORE: StatementReviewStore | None = None


def default_review_store() -> StatementReviewStore:
    """The process-wide review store, loaded once from :data:`REVIEW_STORE_PATH`.

    Exists so a caller that forgets to thread a store still gets the curator's verdicts
    rather than silently discarding them. That omission is what let a ``REJECTED``
    statement ship as ``medic_reliability: HIGH`` from the KGX export (#62): every other
    caller passed a status, the export did not, and nothing in the type signature made the
    difference visible.
    """
    global _DEFAULT_REVIEW_STORE
    if _DEFAULT_REVIEW_STORE is None:
        _DEFAULT_REVIEW_STORE = StatementReviewStore().load()
    return _DEFAULT_REVIEW_STORE


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
DEFAULT_GLOBS = [
    "products/drug_list.yaml",
    "products/indication_list.yaml",
    "products/contraindication_list.yaml",
    "products/research_list.yaml",
    "products/adverse_event_list.yaml",
]


def _load(path: str) -> list[dict]:
    """Load records from a bare-list kb file or a wrapped product file.

    Product files wrap their records under a single list-valued key
    (``drugs:`` / ``associations:``); kb/indications files are bare lists.
    """
    with open(path) as fh:
        data = yaml.safe_load(fh) or []
    if isinstance(data, dict):
        lists = [v for v in data.values() if isinstance(v, list)]
        data = lists[0] if lists else [data]
    return [r for r in data if isinstance(r, dict)]


@app.command()
def main(
    files: list[str] = typer.Argument(None, help="YAML files (default: products + kb/indications)."),
    core_only: bool = typer.Option(True, help="Restrict the reliable subset to core statement types."),
) -> None:
    """Tally MeDIC statements by (statement type × reliability tier)."""
    paths = list(files or [])
    if not paths:
        for pattern in DEFAULT_GLOBS:
            paths.extend(sorted(glob.glob(pattern)))

    review = StatementReviewStore().load()
    grid: Counter = Counter()
    for path in paths:
        for rec in _load(path):
            st = classify_statement(rec)
            grid[(st, score_reliability(rec, st, review_status=review.status(rec)))] += 1

    tiers = [ReliabilityTier.HIGH, ReliabilityTier.MEDIUM, ReliabilityTier.LOW, ReliabilityTier.EXCLUDED]
    header = f"{'statement_type':22}" + "".join(f"{t.value:>10}" for t in tiers) + f"{'total':>10}"
    typer.echo(header)
    typer.echo("-" * len(header))
    reliable = total = 0
    for st in StatementType:
        row = [grid[(st, t)] for t in tiers]
        if not any(row):
            continue
        rt = sum(row)
        total += rt
        core = (st in CORE_TYPES) or (not core_only)
        if core:
            reliable += grid[(st, ReliabilityTier.HIGH)] + grid[(st, ReliabilityTier.MEDIUM)]
        tag = "" if core else "  (non-core)"
        typer.echo(f"{st.value:22}" + "".join(f"{n:>10}" for n in row) + f"{rt:>10}{tag}")
    typer.echo("-" * len(header))
    typer.echo(
        f"\nTotal statements: {total}\n"
        f"Reliable subset ({'core, ' if core_only else ''}HIGH+MEDIUM): {reliable} "
        f"({(100*reliable/total if total else 0):.1f}%) — this is what a soft-launch consumer imports."
    )


if __name__ == "__main__":
    app()

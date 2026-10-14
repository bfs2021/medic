"""Extraction-fidelity validator — snippet-entailment for extracted diseases.

The three LinkML validators check *structure* (schema), *term existence* (CHEBI/MONDO
resolve), and *literature snippets* (PMID abstracts). None of them check that a disease
we extracted from a **regulatory** free-text section is actually *stated in that section*.
That gap is where LLM hallucination and indication↔contraindication inversion hide
(see FAILURE_MODES.md §4.1, §5.1, §8.1).

This validator closes the detection half: for every extracted disease it asks
*"does the source text the LLM was shown actually contain this disease?"* by lexical
entailment — the fraction of the disease label's content tokens that appear in the
source snippet/section. It is deterministic and offline (no network, no LLM).

Scores are intentionally lenient, and a low score means **review**, not **wrong**:

* score ~1.0  — the disease string appears (near) verbatim in the source. Entailed.
* 0 < score < threshold — partial overlap: usually LLM canonicalization
  ("type 2 diabetes" -> "type 2 diabetes mellitus") or a synonym the source spells
  differently ("high blood pressure" -> "hypertension"). Worth a curator's glance.
* score == 0 — none of the disease's content words appear in the source text. A likely
  hallucination or a mis-sectioned extraction (contraindication pulled into indications).

Because the stored snippet may be truncated (evidence snippets are capped at 500 chars),
the check also folds in the record's full section fields (``indications_text``,
``contraindications_text``, ``notes``) when present, to avoid false "not found" flags.

Alongside entailment it runs an **assertion-polarity** check (FAILURE_MODES §4.1-4.2): an
INDICATION whose disease sits inside a negated / excluded scope in the source ("should not
be used in type 1 diabetes", "except in active tuberculosis", "not indicated for asthma")
is flagged as a likely inversion — a disease the drug is contraindicated in, wrongly
recorded as an approval. Together these are the deterministic second-pass verifier.
"""

from __future__ import annotations

import glob
import re
import unicodedata
from typing import NamedTuple

import typer
import yaml

from medic.spans import readable_spans, spans_for_source, spans_with_role

app = typer.Typer(add_completion=False)

# Default corpus: the per-source on-label extraction files (records with evidence snippets).
DEFAULT_GLOBS = [
    "kb/indications/*/indications.yaml",
    "kb/indications/*/contraindications.yaml",
]

# Non-semantic words dropped before token comparison. Deliberately small — clinical words
# like "disease", "syndrome", "type", "chronic" are meaning-bearing and are kept.
_STOPWORDS = frozenset({
    "of", "the", "in", "and", "with", "a", "an", "to", "for", "or", "as", "at",
    "by", "on", "from",
})

# Record-level fields that hold the full source section text (fuller than the 500-char
# evidence snippet), folded into the text a disease is checked against.
_SECTION_TEXT_KEYS = ("indications_text", "contraindications_text", "notes", "indication")


def _normalize(text: str) -> str:
    """Lowercase, strip diacritics, and reduce non-alphanumerics to single spaces."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    out = []
    for ch in text.lower():
        out.append(ch if ch.isalnum() else " ")
    return " ".join("".join(out).split())


def _content_tokens(text: str) -> list[str]:
    return [t for t in _normalize(text).split() if t not in _STOPWORDS]


def entailment_score(disease_label: str, source_text: str) -> float:
    """How well ``disease_label`` is lexically supported by ``source_text`` (0..1).

    1.0 when the normalized disease string is a substring of the normalized source, else
    the fraction of the disease's content tokens that appear in the source. 0.0 means no
    content word overlaps (likely hallucinated / mis-sectioned).
    """
    d_tokens = _content_tokens(disease_label)
    if not d_tokens:
        return 0.0
    norm_source = _normalize(source_text)
    if not norm_source:
        return 0.0
    if f" {_normalize(disease_label)} " in f" {norm_source} ":
        return 1.0
    source_tokens = set(norm_source.split())
    hits = sum(1 for t in d_tokens if t in source_tokens)
    return hits / len(d_tokens)


# --- assertion polarity (negation / exclusion) ----------------------------
# A disease extracted as a positive INDICATION but sitting inside a negated /
# excluded scope in the source is a polarity error (FAILURE_MODES §4.1-4.2):
# "not indicated for asthma" must not become an approved indication for asthma.
# These cues, appearing *before* the disease within its sentence, negate it.
_NEGATION_CUES = (
    "not indicated", "not be used", "not for use", "not for the treatment",
    "not recommended", "should not", "must not", "do not use", "does not treat",
    "contraindicat",          # contraindicated / contraindication
    "limitation of use", "limitations of use",
    "except", "unless", "other than", "but not", "rather than",
)
# The contraindication-side cue list. Disjoint from _NEGATION_CUES by necessity: in a
# contraindications section "contraindicated in X" / "should not be used in X" is the
# *positive* assertion, so those cues would negate every row. What negates a
# contraindication is the source denying it, or excepting the condition from a class.
_CONTRA_NEGATION_CUES = (
    "no known contraindication", "no contraindications", "no contraindication",
    "not contraindicated", "no absolute contraindication", "none known",
    "except", "unless", "other than",
)

# Sentence boundaries we scan back to (keep the raw text; _normalize drops these).
_SENTENCE_BOUNDARY = re.compile(r"[.;:\n]")


def assertion_negated(
    disease_label: str,
    source_text: str,
    *,
    head_fallback: bool = True,
    cues: tuple[str, ...] = _NEGATION_CUES,
) -> tuple[int, int, str]:
    """Count how many of the disease's mentions sit in a negated/excluded scope.

    Returns ``(negated, total, reason)``. ``total`` is how many times the disease is
    locatable in the source; ``negated`` how many of those occurrences are preceded
    (within the same sentence) by a negation/exception cue. ``total == 0`` means the
    disease could not be located, so polarity is *not evaluable* (no judgement).

    ``head_fallback`` — when the full disease phrase is not a substring, fall back to
    anchoring on the head content word. Sensitive but unsafe for destructive use: the
    head word can match a *different* disease that shares it (e.g. "vertebral fractures"
    borrowing the negation of "hip fractures"). Callers that only *flag for review* keep
    it on; callers that *drop* records (see :func:`screen_indications`) turn it off so a
    drop only ever fires on a full-phrase match.

    ``cues`` — which phrases count as negating. Defaults to the indication cue list. A
    contraindication section needs a *different* one: "contraindicated in X" is how that
    section states its claim, not how it negates it (see :data:`_CONTRA_NEGATION_CUES`).
    """
    low = (source_text or "").lower()
    phrase = (disease_label or "").lower().strip()
    if not low or not phrase:
        return 0, 0, ""

    anchors: list[int] = []
    idx = low.find(phrase)
    while idx != -1:
        anchors.append(idx)
        idx = low.find(phrase, idx + 1)
    if not anchors and head_fallback:  # last-resort: the head content word
        tokens = _content_tokens(disease_label)
        if tokens:
            anchors = [m.start() for m in re.finditer(rf"\b{re.escape(tokens[-1])}\b", low)]
    if not anchors:
        return 0, 0, ""

    negated, reason = 0, ""
    for pos in anchors:
        boundaries = [m.end() for m in _SENTENCE_BOUNDARY.finditer(low, 0, pos)]
        window = low[(boundaries[-1] if boundaries else 0):pos]
        cue = next((c for c in cues if c in window), None)
        if cue:
            negated += 1
            reason = reason or cue
    return negated, len(anchors), reason


#: Prepositions that introduce the patient population, a background condition or a
#: setting rather than the condition being treated. Each is the head of a phrase the audit
#: found promoted to the object: "in patients with AML", "associated with congenital heart
#: disease", "post myocardial infarction", "following hip replacement", "due to anemia".
_CONTEXT_CUES = (
    "in patients with", "in patients receiving", "in patients undergoing",
    "in adults with", "in children with", "in subjects with",
    "associated with", "secondary to", "due to", "related to",
    "following", "after", "post", "concomitant with", "in combination with",
    "who have", "with a history of", "at risk of", "at risk for",
)

#: How far back from an occurrence a cue may sit and still govern it. Long enough for
#: "in patients receiving chronic", short enough that the previous clause's cue does not
#: reach across.
_CONTEXT_WINDOW = 40


def _occurrences(phrase: str, text: str) -> list[int]:
    low, needle = text.lower(), (phrase or "").lower().strip()
    if not needle:
        return []
    out, idx = [], low.find(needle)
    while idx != -1:
        out.append(idx)
        idx = low.find(needle, idx + 1)
    return out


def _is_context_introduced(pos: int, text: str) -> bool:
    window = text[max(0, pos - _CONTEXT_WINDOW):pos].lower()
    return any(cue in window for cue in _CONTEXT_CUES)


def context_only_diseases(disease_names: list[str], source_text: str) -> list[str]:
    """Which of these diseases is named only as a population, background or setting?

    38 of 291 audited edges (13.1%) promote the context to the object — the largest single
    error class, and half of all MeDIC errors (#89). "Prophylaxis of invasive aspergillosis
    **in patients with** acute myeloid leukemia" yields an edge saying the antifungal treats
    AML.

    Decided by comparison with the siblings extracted from the same text, which is evidence
    the ingest holds and the merge does not. A disease whose every occurrence sits behind a
    context cue, while another extracted disease occurs as a plain target, is the setting and
    the sibling is the claim. With no plain sibling nothing is returned: a lone "in patients
    with X" may be all the label says, and there is then no second condition to prefer.

    Reported, never dropped. A preposition is weaker evidence than #66 requires for a
    destructive verdict, and the reliability gate can say "probably the setting" without
    deleting the row.
    """
    text = source_text or ""
    plain, context_only = set(), []
    positions = {name: _occurrences(name, text) for name in disease_names}
    for name, where in positions.items():
        if where and any(not _is_context_introduced(p, text) for p in where):
            plain.add(name)
    if not plain:
        return []
    for name, where in positions.items():
        if not where or name in plain:
            continue
        if all(_is_context_introduced(p, text) for p in where):
            context_only.append(name)
    return context_only


def _claim_and_limitation_text(
    source_text: str, source: str, parts: list[str] | None = None
) -> tuple[str, str]:
    """Split ``source_text`` into (text a claim may be read from, limitation text).

    The ingest gate used to run over the whole concatenated section, so a cue inside a
    ``Limitations of Use`` subsection was in scope for the indication sentence — the §4.3
    span-scoping bug, fixed for the reporting half in ``on_label_merge`` but never for the
    destructive half (issue #59). Both halves now read scope from
    :func:`medic.spans.readable_span_indices`.
    """
    spans = spans_for_source(
        source, source_text, document="", section_code="", parts=parts)
    if not spans:
        return (source_text or "").strip(), ""
    claim = " ".join(s["text"] for s in readable_spans(spans))
    limitation = " ".join(
        s["text"] for s in spans_with_role(spans, "LIMITATION_STATEMENT"))
    return claim or (source_text or "").strip(), limitation


def locatable_anchor(
    name: str, verbatim: str | None, claim_text: str, limitation_text: str
) -> str:
    """Which string to search the source for: the source's own wording, or the name.

    Public because both halves of the polarity check must agree on it. The ingest screen
    and ``on_label_merge._polarity_flags`` searching for different strings is the same
    class of drift #59 fixed for span scope, and it has the same consequence: #66 keeps a
    limitation-only row on the understanding that the merge will reach the same verdict
    and flag it, which only holds if the merge is looking for the same thing.

    Prefers ``verbatim`` — it is what the extractor copied out of the text, so it is
    findable where a canonicalised name is not. Falls back to ``name`` when no verbatim was
    recorded (a cached answer predating #64, or a model that ignored the format) or when
    the recorded one cannot be found either, which is the hallucinated-quote case. Falling
    back matters: an unfindable verbatim must never make a row *less* evaluable than the
    name alone would have made it.
    """
    quote = (verbatim or "").strip()
    if not quote:
        return name
    haystack = f"{claim_text} {limitation_text}".lower()
    return quote if quote.lower() in haystack else name


class ScreenResult(NamedTuple):
    """Outcome of a destructive screen, one bucket per verdict.

    ``unlocatable`` is the bucket that did not exist: a disease the screen could not find
    in the text stayed in ``kept`` with nothing recorded, so "not checked" was
    indistinguishable from "checked and clean". It is ~12% of rows (1414 of 11696 shipped
    INDICATIONs), because the extraction prompt asks the LLM to canonicalise names and a
    canonicalised name is not a substring of the source. Those names are still kept —
    dropping them on a check that never ran would be far worse — but they are enumerable
    now, and the merge marks them ``polarity_unverified``.

    ``limitation_only`` is a *verdict* rather than a drop: the disease has no positive
    support in the claim spans and its only mention is a strictly-negated scope
    restriction. That used to be dropped. It is not any more, because the evidence is too
    weak for a destructive, silent, unrecoverable action — 1 of the 3 drops it produced
    over the DailyMed cache was a genuine approval (aspirin/omeprazole →
    ``myocardial infarction``, where the label abbreviates to "MI" in the indication and
    spells it out in the restriction, so entailment scores zero). These names are kept and
    reported; ``on_label_merge._polarity_flags`` reaches the same verdict and marks the
    claim ``negated_inversion``, which the reliability gate turns into EXCLUDED. The row
    therefore still leaves the published subset, but it stays in the knowledge base with
    its reason attached and a curator can overturn it through the review store (#66).
    """

    kept: list[str]
    dropped: list[dict]
    unlocatable: list[str]
    limitation_only: list[dict] = []


def _screen(
    disease_names: list[str],
    source_text: str,
    *,
    source: str,
    cues: tuple[str, ...],
    check_limitations: bool,
    parts: list[str] | None = None,
    verbatims: dict[str, str] | None = None,
) -> ScreenResult:
    """Shared body of :func:`screen_indications` and :func:`screen_contraindications`."""
    claim_text, limitation_text = _claim_and_limitation_text(source_text, source, parts)
    verbatims = verbatims or {}
    kept: list[str] = []
    dropped: list[dict] = []
    unlocatable: list[str] = []
    limitation_only: list[dict] = []

    for name in disease_names:
        # Anchor on the source's own wording when the extractor recorded it. The prompt
        # asks for a canonicalised name, so the name itself is often absent from the text
        # and polarity is then not evaluable at all (#64). The verbatim form is present by
        # construction — but a model can hallucinate a quote as easily as a name, so fall
        # back to the name whenever the quote cannot be found either.
        anchor = locatable_anchor(name, verbatims.get(name), claim_text, limitation_text)

        # Strict, full-phrase matching only — a destructive drop must never fire on a
        # head-word that belongs to a different disease.
        neg, total, reason = assertion_negated(
            anchor, claim_text, head_fallback=False, cues=cues)
        if total and neg == total:
            dropped.append({"disease": name, "reason": reason or "negated",
                            "scope": "claim"})
            continue
        if total:
            kept.append(name)
            continue

        # Not locatable in the spans the claim was read from. Check the scope restrictions
        # the claim's own span excluded: a disease with no positive support whose only
        # mention is a strictly-negated Limitations-of-Use sentence was probably never
        # indicated. Guarded on zero entailment, so "indicated for X" plus "Limitations of
        # Use: not indicated for X under 12" stays a legitimate restricted indication.
        #
        # Reported, not dropped. Zero entailment only means the claim span does not use
        # these words, which a label that abbreviates defeats — aspirin/omeprazole writes
        # "MI" in the indication and "myocardial infarction" in the restriction, and 1 of
        # the 3 drops this produced over the DailyMed cache was that genuine approval.
        # `head_fallback=False` above exists precisely so a destructive drop needs strong
        # evidence; this verdict does not meet that bar (#66). The merge reaches the same
        # conclusion and marks the claim `negated_inversion` -> EXCLUDED, which removes it
        # from the published subset while leaving it auditable and curator-overridable.
        if check_limitations and limitation_text and entailment_score(anchor, claim_text) == 0.0:
            lneg, ltotal, lreason = assertion_negated(
                anchor, limitation_text, head_fallback=False, cues=cues)
            if ltotal and lneg == ltotal:
                limitation_only.append({"disease": name, "reason": lreason or "negated"})
                kept.append(name)
                continue
        kept.append(name)
        unlocatable.append(name)
    return ScreenResult(kept, dropped, unlocatable, limitation_only)


def screen_indications(
    disease_names: list[str], source_text: str, *, source: str = "DAILYMED",
    parts: list[str] | None = None, verbatims: dict[str, str] | None = None,
) -> ScreenResult:
    """Split extracted *indication* disease names into kept / dropped / unlocatable.

    Prevention half of the second-pass verifier, called at **ingest** time: a disease whose
    every locatable mention sits inside a negated/excluded scope ("should not be used in…",
    "except…", "not indicated for…", "contraindicated in…") is a near-certain inversion —
    the source states it negatively, so it must not be recorded as a positive indication.

    Scope is the spans a claim may be read from, not the whole concatenated section: a cue
    in a ``Limitations of Use`` subsection no longer reaches across and kills the real
    approval in the sentence above it. ``source`` selects the split
    (:func:`medic.spans.spans_for_source`); only DailyMed has inner structure to recover,
    so this is a deliberate no-op for EMA/PMDA/India.

    Conservative by design: a disease mentioned both positively and negatively, or not
    locatable at all, is kept — but an unlocatable one is now reported in
    :attr:`ScreenResult.unlocatable` rather than passing as clean.

    Entailment is intentionally *not* a drop criterion here — a score of 0 is often just
    LLM canonicalization / a synonym, so it stays a review flag (see the validator), not
    a silent ingest drop.
    """
    return _screen(disease_names, source_text, source=source,
                   cues=_NEGATION_CUES, check_limitations=True, parts=parts,
                   verbatims=verbatims)


def screen_contraindications(
    disease_names: list[str], source_text: str, *, source: str = "DAILYMED",
    parts: list[str] | None = None, verbatims: dict[str, str] | None = None,
) -> ScreenResult:
    """The contraindication-side screen. Same shape, opposite polarity.

    The contraindication extractor was never screened at all (issue #59). It cannot reuse
    :data:`_NEGATION_CUES`: that list contains "contraindicat", "should not" and "not be
    used", which in a contraindications section are how the claim is *stated*. Screening
    with it would drop nearly every contraindication.

    What negates a contraindication is the source saying the condition is *not* one —
    "no known contraindications", "not contraindicated in…" — or excepting it from a
    broader class (see :data:`_CONTRA_NEGATION_CUES`).

    Limitations-of-Use splitting does not apply: it is a structure of SPL *indications*
    sections, so the limitation pass is off here.
    """
    return _screen(disease_names, source_text, source=source,
                   cues=_CONTRA_NEGATION_CUES, check_limitations=False, parts=parts,
                   verbatims=verbatims)


def _source_text_for(record: dict, evidence: dict) -> str:
    parts = [str(evidence.get("snippet") or "")]
    parts += [str(record.get(k) or "") for k in _SECTION_TEXT_KEYS]
    return " ".join(p for p in parts if p)


def check_record(record: dict) -> list[dict]:
    """Return one finding per (evidence item) with an extracted disease label."""
    findings: list[dict] = []
    relationship = record.get("relationship_type", "")
    evidence_items = record.get("evidence") or []
    for ev in evidence_items:
        disease_label = (ev.get("original_disease_label") or "").strip()
        if not disease_label:
            # Fall back to the grounded original string when the raw label is absent.
            disease_label = ((record.get("disease_grounding") or {}).get("original_string") or "").strip()
        if not disease_label:
            continue
        source_text = _source_text_for(record, ev)
        if not source_text.strip():
            continue  # nothing to check against; not a fidelity judgement
        score = entailment_score(disease_label, source_text)
        # Polarity only matters for positive claims: an INDICATION whose disease is
        # negated/excluded in the source is a likely inversion (should be dropped or
        # recorded as a contraindication).
        negated = False
        negation_reason = ""
        if relationship == "INDICATION":
            neg, total, reason = assertion_negated(disease_label, source_text)
            if total and neg == total:  # every locatable mention is negated
                negated, negation_reason = True, reason
        findings.append({
            "relationship_type": relationship,
            "drug_label": (ev.get("original_drug_label")
                           or record.get("final_normalized_drug_label")  # source-record shape
                           or (record.get("drug") or {}).get("label", "")),  # product shape
            "disease_label": disease_label,
            "grounded_disease": (record.get("final_normalized_disease_label")
                                 or (record.get("disease") or {}).get("resolved_label", "")),
            "score": round(score, 3),
            "negated": negated,
            "negation_reason": negation_reason,
            "reference": ev.get("reference", ""),
        })
    return findings


def validate_file(path: str, threshold: float) -> dict:
    with open(path) as fh:
        records = yaml.safe_load(fh) or []
    if not isinstance(records, list):
        records = [records]
    findings: list[dict] = []
    for rec in records:
        if isinstance(rec, dict):
            findings.extend(check_record(rec))
    flagged = [f for f in findings if f["score"] < threshold]
    negated = [f for f in findings if f.get("negated")]
    return {"path": path, "checked": len(findings), "flagged": flagged, "negated": negated}


@app.command()
def main(
    files: list[str] = typer.Argument(None, help="YAML files to check (default: kb/indications/*)."),
    threshold: float = typer.Option(0.5, help="Entailment score below which an extraction is flagged."),
    out: str = typer.Option("", help="Optional TSV path to write flagged extractions for curation."),
    strict: bool = typer.Option(False, help="Exit non-zero if any extraction is flagged (CI mode)."),
    limit_examples: int = typer.Option(15, help="How many flagged examples to print."),
) -> None:
    """Check that extracted diseases are lexically stated in their source snippet/section."""
    paths: list[str] = list(files or [])
    if not paths:
        for pattern in DEFAULT_GLOBS:
            paths.extend(sorted(glob.glob(pattern)))
    if not paths:
        typer.echo("No input files found (looked for kb/indications/*/{indications,contraindications}.yaml).")
        raise typer.Exit(0)

    total_checked = 0
    all_flagged: list[dict] = []
    all_negated: list[dict] = []
    for path in paths:
        result = validate_file(path, threshold)
        total_checked += result["checked"]
        for f in result["flagged"] + result["negated"]:
            f["file"] = path
        all_flagged.extend(result["flagged"])
        all_negated.extend(result["negated"])
        typer.echo(
            f"{path}: {result['checked']} extractions, {len(result['flagged'])} not-entailed "
            f"(score < {threshold}), {len(result['negated'])} negated-polarity"
        )

    zeros = [f for f in all_flagged if f["score"] == 0.0]
    typer.echo(
        f"\nTOTAL: {total_checked} extractions checked."
        f"\n  Entailment: {len(all_flagged)} flagged · {len(zeros)} with score 0 "
        f"(no content-word overlap — likely hallucinated / mis-sectioned)."
        f"\n  Polarity:   {len(all_negated)} INDICATION(s) whose disease is negated/excluded "
        f"in the source (likely inversion)."
    )

    if all_negated:
        typer.echo("\nNegated-polarity indications (should be dropped or re-polarised):")
        for f in all_negated[:limit_examples]:
            typer.echo(
                f"  [neg: {f['negation_reason']!r}] {f['drug_label']!r} -> {f['disease_label']!r}  "
                f"({f['file'].split('/')[-2]})"
            )

    # Entailment offenders, worst (score 0) first.
    if all_flagged:
        typer.echo("\nNot-entailed extractions (lowest score first):")
        for f in sorted(all_flagged, key=lambda x: x["score"])[:limit_examples]:
            typer.echo(
                f"  [{f['score']:.2f}] {f['relationship_type']:15} {f['drug_label']!r} -> "
                f"{f['disease_label']!r}  ({f['file'].split('/')[-2]})"
            )

    if out:
        cols = ["file", "relationship_type", "drug_label", "disease_label",
                "grounded_disease", "score", "negated", "negation_reason", "reference"]
        rows = {id(f): f for f in all_flagged + all_negated}.values()  # de-dup shared findings
        with open(out, "w") as fh:
            fh.write("\t".join(cols) + "\n")
            for f in sorted(rows, key=lambda x: (not x.get("negated"), x["score"])):
                fh.write("\t".join(str(f.get(c, "")) for c in cols) + "\n")
        typer.echo(f"\nWrote {len(list(rows))} flagged rows -> {out}")

    if strict and (all_flagged or all_negated):
        raise typer.Exit(1)


if __name__ == "__main__":
    app()

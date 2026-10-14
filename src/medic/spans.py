"""Turn a source's raw extracted text into typed TextSpans (design spec D6).

Sources hand MeDIC one flat string per record. That string is often several semantically
distinct chunks concatenated: a section header, the indication sentence, a subsection header,
and a scope-limiting sentence. Conflating them put a *negated* sentence in scope for the
*positive* claim's negation check — see the UBRELVY case in the design spec §4.3.

Splitting is deliberately conservative. When the structure is not recoverable, one
`SECTION_TEXT` (or the source's default role) is emitted — degraded, not wrong. Every role
returned is a `TextSpanRoleEnum` value; nothing here invents a vocabulary.
"""

from __future__ import annotations

import re

#: How many characters an ingester keeps when it stores a supporting snippet.
#:
#: Shared rather than repeated as a literal, because the merge has to be able to tell a
#: snippet that was *cut* from one that merely ended: a span sitting exactly on the cap is
#: a truncated span, which is the `truncated_snippet` recognition flag (FAILURE_MODES 5.6).
#: If an ingester's slice and this constant drift apart, that detection silently stops.
SNIPPET_CHAR_CAP = 500


def is_truncated(text: str) -> bool:
    """Was this span cut short by :data:`SNIPPET_CHAR_CAP`?

    A span landing exactly on the cap was almost certainly sliced — a source section whose
    length is exactly 500 characters is vanishingly rare, and the cost of the rare false
    positive is one record scored MEDIUM instead of HIGH.
    """
    return len(text or "") >= SNIPPET_CHAR_CAP


#: SPL section titles that appear inline at the head of an extracted section body.
_SPL_HEADERS = ("INDICATIONS AND USAGE", "INDICATIONS & USAGE", "CONTRAINDICATIONS")

#: The subsection marker introducing a scope restriction inside an SPL indications section.
_LIMITATION_MARKER = re.compile(r"\bLimitations? of Use\b")

#: Default span role per ingester, for sources whose extracted text has no inner structure.
_SOURCE_ROLE = {
    "DAILYMED": "SECTION_TEXT",
    "EMA": "STRUCTURED_FIELD",
    "PMDA": "SECTION_TEXT",
    "INDIA": "TABLE_CELL",
    "CDSCO": "TABLE_CELL",
}


#: A marker reached through a cross-reference — "[see Limitations of Use (1.2)]", or a bare
#: "see Limitations of Use below" — points at another section instead of opening one here.
_LIMITATION_XREF = re.compile(r"(?i)(\[\s*see[^\]]{0,40}|\bsee\s+)$")


def _first_limitation_header(body: str):
    """The first `Limitations of Use` that actually opens a subsection, or None.

    Matching the phrase anywhere treated a pointer to another section as a boundary. On the
    KABIVEN label that cut the contraindications section at `[see`, leaving 290 characters
    of shipped text ending mid-bracket and relabelling the following 1,154 characters of
    ordinary contraindication text as a scope restriction — which is what #100 observed as
    an unreliable truncation flag, and #65's span work could not see because the phrase
    really was there.

    Narrow by measurement: of 617 marker occurrences across the corpus's section chunks,
    exactly 1 is a cross-reference, so the 616 genuine headers are untouched.
    """
    for match in _LIMITATION_MARKER.finditer(body):
        if not _LIMITATION_XREF.search(body[max(0, match.start() - 60):match.start()]):
            return match
    return None


def _span(role: str, text: str, document: str, section_code: str) -> dict:
    span = {"role": role, "text": text, "document": document}
    if section_code:
        span["section_code"] = section_code
    return span


def split_dailymed_section(text: str, *, document: str, section_code: str) -> list[dict]:
    """Split an SPL section body into header / body / limitation spans.

    Lossless: joining the returned texts with a single space reproduces the input, so no source
    text is dropped or invented (I-7).
    """
    body = (text or "").strip()
    if not body:
        return []
    spans: list[dict] = []

    for header in _SPL_HEADERS:
        if body.upper().startswith(header):
            spans.append(_span("SECTION_HEADER", body[:len(header)], document, section_code))
            body = body[len(header):].strip()
            break

    match = _first_limitation_header(body)
    if match:
        before = body[:match.start()].strip()
        marker = body[match.start():match.end()]
        after = body[match.end():].strip()
        if before:
            spans.append(_span("SECTION_TEXT", before, document, section_code))
        spans.append(_span("SUBSECTION_HEADER", marker, document, section_code))
        if after:
            spans.append(_span("LIMITATION_STATEMENT", after, document, section_code))
    elif body:
        spans.append(_span("SECTION_TEXT", body, document, section_code))
    return spans


def spans_for_source(
    source: str, text: str, *, document: str, section_code: str,
    parts: list[str] | None = None,
) -> list[dict]:
    """Typed spans for one source record. Empty text yields no spans, never an empty span.

    ``parts`` — the source's own structural chunks, when the ingester kept them. For
    DailyMed these are the section's `<title>`/`<text>` elements
    (``_section_chunks``). Each is split independently, so a ``Limitations of Use``
    marker can only ever govern text inside the element that contains it.

    Splitting the concatenation instead put the Highlights summary, which restates the
    indication, inside a LIMITATION_STATEMENT span on 203 of the 306 labels carrying the
    marker; per-element splitting reduces that to 9 (#65). ``parts`` is optional because
    records mined before it existed only have the flat string, and a degraded split is
    better than no spans.
    """
    src = (source or "").upper()
    body = (text or "").strip()
    if not body:
        return []
    if src != "DAILYMED":
        return [_span(_SOURCE_ROLE.get(src, "UNKNOWN"), body, document, section_code)]
    if not parts:
        return split_dailymed_section(body, document=document, section_code=section_code)
    out: list[dict] = []
    for part in parts:
        out.extend(split_dailymed_section(
            part, document=document, section_code=section_code))
    return out


#: Roles a claim may NOT be read from. A SECTION_HEADER / SUBSECTION_HEADER names the
#: section rather than asserting anything in it; a LIMITATION_STATEMENT *restricts* a claim
#: made elsewhere, so reading it as the claim — or letting its cues bear on the claim's
#: negation check — is the §4.3 bug.
NON_CLAIM_ROLES = ("SECTION_HEADER", "SUBSECTION_HEADER", "LIMITATION_STATEMENT")


def readable_span_indices(spans: list[dict]) -> list[int]:
    """Positions in ``spans`` a claim may be read from (see :data:`NON_CLAIM_ROLES`).

    Shared by the *reporting* half (``on_label_merge._build_disease_provenance``, which
    records these as the assertion's ``negation_scope``) and the *destructive* half
    (``extraction_fidelity.screen_indications``, which drops records). They held different
    definitions of scope, and only the reporting half was ever fixed — issue #59.
    """
    return [i for i, s in enumerate(spans or []) if s.get("role") not in NON_CLAIM_ROLES]


def readable_spans(spans: list[dict]) -> list[dict]:
    """The spans themselves, for callers that do not need their positions."""
    return [(spans or [])[i] for i in readable_span_indices(spans)]


def spans_with_role(spans: list[dict], role: str, *, exclude: int | None = None) -> list[dict]:
    """Every span carrying ``role``, optionally skipping one index (the claim's own span)."""
    return [s for i, s in enumerate(spans or [])
            if s.get("role") == role and i != exclude]

"""Acquire real DailyMed SPL XML labels via the DailyMed v2 REST API.

This populates ``data/raw/dailymed/`` with one SPL XML per setid so the
SPL-XML mining path in ``medic.ingest.dailymed.__main__`` activates instead of
the legacy Excel fallback.

Acquisition strategy
--------------------
The full DailyMed bulk release (human prescription labels) is split across
multiple multi-GB ZIP archives — tens of GB total, most of it labels for
drug-formulation-manufacturer combinations we never ground. Downloading all of
it unattended is wasteful.

Instead we drive acquisition from the **merged drug list**
(``products/drug_list.yaml``): for each USA-approved drug, and for *every* name
that drug goes by (generic spelling first), we query

    https://dailymed.nlm.nih.gov/dailymed/services/v2/spls.json?drug_name=<NAME>&doctype=<LOINC>

once per human document type (prescription ``34391-3``, OTC ``34390-5``, so
animal labels never enter the pool), rank each name's hits (see
:func:`rank_label_candidates`), pool them, keep up to ``labels_per_drug`` of
them that actually carry an Indications section, and download the clean
per-setid SPL XML

    https://dailymed.nlm.nih.gov/dailymed/services/v2/spls/<SETID>.xml

into ``data/raw/dailymed/<SETID>.xml``. This fetches the labels most likely to
carry a drug's indications, for the drugs we actually care about, rather than
the entire corpus.

Two distinct bugs, two distinct fixes (#98)
-------------------------------------------
**Selection.** The first version kept the single most recently published SPL
among 20 hits for the first drug name that returned anything. That silently
picked narrow or wrong labels: hydromorphone → the extended-release tablets
(no renal-colic indication), doxorubicin → the liposomal product (no bone
sarcoma), albuterol → an ipratropium/albuterol combination, ivermectin → a
veterinary label, flurbiprofen → the ophthalmic drops, hydrocortisone and
lidocaine → combination products. Half of the previous release's
audited-correct indications went missing as a result, and the "most recent"
sort was comparing ``"Oct 14, 2026"`` as a string. Fixed by ranking: the
list endpoint only returns ``title``, ``published_date``, ``spl_version`` and
``setid``, so the ranking reads the SPL title (``BRAND (INGREDIENTS) DOSAGE
FORM [LABELER]``) for the ingredient list, dosage form and formulation
modifiers, and :func:`published_date` parses the date.

**Coverage.** Ranking a pool well does not help when the drug's indications
are split across several labels rather than ranked among them — lansoprazole's
GERD, duodenal ulcer and erosive esophagitis were in no label we had fetched
at all. Fixed separately, by keeping 20 ranked labels rather than one or three
(:data:`DEFAULT_LABELS_PER_DRUG`) and by querying every spelling of the drug
rather than the first productive one (:func:`_usa_drug_names`,
:func:`_resolve_candidates`).

Filtering the pool is what lets the two compose: with animal labels excluded
by ``doctype``, 100 hits per page instead of 20, and labels without a
``34067-9`` section dropped, keeping 20 of a ranked pool costs roughly what
keeping an arbitrary 20 per name used to and is a strictly better 20.

The module is resumable (existing setid files are skipped) and rate-limited
with a small inter-request delay. A manifest mapping drug name -> selected
setids is cached to ``cache/enrichment/dailymed_acquire.json``; entries
written before ranking existed (single ``setid``, no ``alternate_setids``) are
treated as stale and re-resolved on the next run.

Source isolation (docs/source-isolation.md): this only fetches FDA SPL labels
and only ever produces USA-jurisdiction evidence downstream.
"""

from __future__ import annotations

import argparse
import logging
import re
import time
from datetime import date, datetime
from pathlib import Path

import httpx
import yaml

from medic import product_view as pv
from medic.enrichment.cache import EnrichmentCache

logger = logging.getLogger(__name__)

SPLS_LIST_URL = "https://dailymed.nlm.nih.gov/dailymed/services/v2/spls.json"
SPL_XML_URL = "https://dailymed.nlm.nih.gov/dailymed/services/v2/spls/{setid}.xml"

DEFAULT_DATA_DIR = Path("data/raw/dailymed")
DEFAULT_DRUG_LIST = Path("products/drug_list.yaml")
ACQUIRE_CACHE_PATH = Path("cache/enrichment/dailymed_acquire.json")

#: LOINC document-type codes accepted by the list endpoint's ``doctype`` filter.
DOCTYPE_HUMAN_RX = "34391-3"
DOCTYPE_HUMAN_OTC = "34390-5"
HUMAN_DOCTYPES = (DOCTYPE_HUMAN_RX, DOCTYPE_HUMAN_OTC)

#: Largest page the v2 API serves in one request.
PAGESIZE = 100

#: How many ranked labels with an Indications section to keep per drug.
#:
#: 3 is enough to fix the *selection* bug (#98) — one good label beats one arbitrary one —
#: but it does not fix the *coverage* one. A drug's indications are genuinely split across
#: manufacturers and formulations, not merely ranked among them: lansoprazole had 2 labels
#: on disk and asserted exactly one indication, *Helicobacter pylori infection*, while GERD,
#: duodenal ulcer and erosive esophagitis were never in any text we had fetched. Measured on
#: 12 drugs over 221 SPLs: 118 distinct indication sections, ~9.8 per drug rather than 1 or
#: 3. Ranking and breadth are complementary, so this keeps both — rank a deep, vet-free pool
#: and then keep the top 20 of it rather than the top 3.
#:
#: Download volume stays about where it is. Today's acquirer takes up to 20 hits per *name*
#: with ~2 names per drug; this examines up to ``2 * labels_per_drug`` = 40 candidates per
#: *drug* pooled over all its names. Extraction is cached on the section-text hash and
#: roughly half of a drug's SPLs repeat another's indication text verbatim, so the LLM cost
#: tracks distinct sections rather than labels, and the `34067-9` check now drops labels
#: that could never have contributed one.
DEFAULT_LABELS_PER_DRUG = 20
#: LOINC section code for Indications & Usage (same as ``__main__.LOINC_INDICATIONS``;
#: repeated here so acquisition does not import the pandas-heavy ingester).
LOINC_INDICATIONS = "34067-9"

_cache: EnrichmentCache | None = None


def _get_cache() -> EnrichmentCache:
    global _cache
    if _cache is None:
        _cache = EnrichmentCache(ACQUIRE_CACHE_PATH)
    return _cache


def _usa_drug_names(drug_list_path: Path, limit: int = 0) -> list[list[str]]:
    """Return, per USA-approved drug, the names to query DailyMed with, in order.

    ``curie_label`` comes **first**, then the raw ``source_ingredients`` strings. The
    opposite order looks right — upstream label spelling ought to give the best hits on a
    label index — and was measured wrong: the drug list holds product-style strings like
    "Lansoprazole Enteric-Coated Capsules" that DailyMed's ``drug_name`` index does not
    match, while ``curie_label`` is "lansoprazole", which it does. 1,074 of 2,591
    source-first names (41%) returned no SPL at all, and lansoprazole's labels were never
    requested — which is why raising the per-name keep count recovered its indications on
    paper and not in fact. A salt or brand spelling occasionally hits where the generic
    misses, so the source strings stay, behind.

    The first name is the drug's cache key, which this ordering also makes the stable one.
    All of them are queried and their hits pooled (see :func:`_resolve_candidates`); a name
    already claimed by an earlier drug is not reused.
    """
    if not drug_list_path.exists():
        logger.warning("Merged drug list not found: %s", drug_list_path)
        return []

    data = yaml.safe_load(drug_list_path.read_text()) or {}
    drugs = data.get("drugs", [])
    names: list[list[str]] = []
    seen: set[str] = set()
    for d in drugs:
        if "FDA" not in pv.approved_authorities(d):
            continue
        candidates: list[str] = []
        label = pv.drug_label(d)
        if label:
            candidates.append(str(label))
        ingredients = d.get("source_ingredients") or []
        if isinstance(ingredients, list):
            candidates.extend(str(x) for x in ingredients if x)
        mine: list[str] = []
        for name in candidates:
            key = name.strip().lower()
            if key and key not in seen:
                seen.add(key)
                mine.append(name.strip())
        if mine:
            names.append(mine)
    if limit > 0:
        names = names[:limit]
    logger.info("Selected %d USA-approved drugs to acquire", len(names))
    return names


def _is_usa_approved(value) -> bool:
    if value is True:
        return True
    return str(value).strip().upper() in ("TRUE", "APPROVED", "1")


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #

_LABELER_RE = re.compile(r"\s*\[[^\]]*\]\s*$")
_PAREN_RE = re.compile(r"\(([^()]*)\)")
_WORD_RE = re.compile(r"[A-Z0-9][A-Z0-9%.\-]*")
#: Ingredient-list separators: "A AND B", "A, B", "A; B", "A/B", "A WITH B".
_COMBO_RE = re.compile(r"\b(AND|WITH|PLUS)\b|[,;/&]")
#: Title words that mark a veterinary product when no ``doctype`` is attached.
_VETERINARY_RE = re.compile(
    r"\b(VETERINARY|ANIMAL|CATTLE|BOVINE|SWINE|PORCINE|EQUINE|HORSES?|CANINE|DOGS?|"
    r"FELINE|CATS?|OVINE|SHEEP|GOATS?|POULTRY|CHICKENS?|POUR-ON)\b"
)
#: Formulation modifiers that narrow the label's indications relative to the
#: plain product (liposomal doxorubicin lacks bone sarcoma; ER hydromorphone
#: lacks renal colic).
_MODIFIED_FORM_RE = re.compile(
    r"\b(LIPOSOM\w*|LIPID COMPLEX|PEGYLATED|DEPOT|"
    r"(EXTENDED|DELAYED|SUSTAINED|CONTROLLED|PROLONGED)[- ]RELEASE)\b"
)
#: Dosage-form words from SPL titles, by route tier.
_SYSTEMIC_FORMS = {
    "TABLET", "TABLETS", "CAPSULE", "CAPSULES", "INJECTION", "INJECTABLE",
    "SYRUP", "ELIXIR", "SUSPENSION", "SOLUTION", "CONCENTRATE", "GRANULE",
    "GRANULES", "PELLET", "PELLETS", "LOZENGE", "TROCHE", "FILM", "WAFER",
    "KIT", "POWDER", "PACKET", "IMPLANT", "BEAD",
}
_LOCAL_FORMS = {
    "CREAM", "OINTMENT", "LOTION", "GEL", "PATCH", "SYSTEM", "SHAMPOO", "FOAM",
    "PASTE", "EMULSION", "SOAP", "STICK", "SWAB", "CLOTH", "DROPS", "SUPPOSITORY",
    "ENEMA", "RINSE", "MOUTHWASH", "SPRAY", "JELLY", "WASH", "CLEANSER", "PAD",
    "SPONGE", "DRESSING", "TINCTURE", "LINIMENT", "INSERT", "RING",
}
#: Route words that make an otherwise-systemic form local ("SOLUTION/ DROPS",
#: "OPHTHALMIC SOLUTION", "LIDOCAINE VISCOUS").
_LOCAL_ROUTE_WORDS = {
    "OPHTHALMIC", "OTIC", "TOPICAL", "IRRIGATION", "INTRAVESICAL", "NASAL",
    "VAGINAL", "RECTAL", "DENTAL", "VISCOUS", "DROPS", "DERMAL", "TRANSDERMAL",
}
_FORM_WORDS = _SYSTEMIC_FORMS | _LOCAL_FORMS | _LOCAL_ROUTE_WORDS | {
    "AEROSOL", "INHALANT", "INHALATION", "METERED",
}
#: Form words that name the product rather than qualify it ("INJECTION, SOLUTION"
#: and "INJECTION" are the same kind of label for diversity purposes).
_PRIMARY_FORMS = (_SYSTEMIC_FORMS | _LOCAL_FORMS) - {
    "SOLUTION", "SUSPENSION", "POWDER", "CONCENTRATE", "KIT", "PACKET", "FILM",
}
_DATE_FORMATS = ("%b %d, %Y", "%Y-%m-%d", "%m/%d/%Y", "%Y%m%d")

ROUTE_SYSTEMIC = 0
ROUTE_OTHER = 1  # inhalation, unknown
ROUTE_LOCAL = 2  # topical, ophthalmic, otic, rectal, ...


def _title_body(title: str) -> str:
    """Uppercased SPL title without the trailing ``[LABELER]``."""
    return _LABELER_RE.sub("", str(title or "")).upper().strip()


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text.upper())


def is_combination(name: str) -> bool:
    """True when *name* lists more than one ingredient."""
    return bool(_COMBO_RE.search(str(name or "").upper()))


def _ingredient_strings(body: str) -> list[str]:
    """The ingredient lists named in an SPL title.

    DailyMed titles read ``BRAND (INGREDIENTS) FORM`` or ``INGREDIENTS FORM``;
    multi-product labels concatenate several such products. Parenthesised
    groups are the ingredient lists; without any, the words before the first
    dosage-form word are.
    """
    groups = [g.strip() for g in _PAREN_RE.findall(body) if g.strip()]
    if groups:
        return groups
    head: list[str] = []
    for tok in body.replace(",", " , ").split():
        if tok in _FORM_WORDS or _MODIFIED_FORM_RE.match(tok):
            break
        head.append(tok)
    return [" ".join(head).strip(" ,")]


def published_date(hit: dict) -> date:
    """Parse the list endpoint's ``published_date`` ("Oct 14, 2026"); ``date.min`` if unparseable.

    The first version sorted the raw string, which orders "Sep 1, 2019" after
    "Jun 12, 2024".
    """
    raw = str(hit.get("published_date") or hit.get("publishing_date") or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return date.min


def label_features(hit: dict, drug_name: str) -> dict:
    """What the ranking knows about one list-endpoint hit.

    Keys: ``excluded`` (animal / veterinary label), ``combination`` (more than
    one ingredient), ``otc`` (human OTC document type), ``route_tier``
    (:data:`ROUTE_SYSTEMIC` < :data:`ROUTE_OTHER` < :data:`ROUTE_LOCAL`),
    ``modified`` (liposomal / extended-release style formulation),
    ``name_match`` (the drug's first word appears in the ingredient list),
    ``published`` (:class:`datetime.date`) and ``forms`` (the primary
    dosage-form words, used to keep the kept labels diverse).
    """
    body = _title_body(hit.get("title", ""))
    words = set(_words(body))
    doctype = str(hit.get("doctype") or "").strip()
    if doctype:
        excluded = doctype not in HUMAN_DOCTYPES
    else:
        excluded = bool(_VETERINARY_RE.search(body))

    ingredients = _ingredient_strings(body)
    combination = any(is_combination(s) for s in ingredients)

    if words & _LOCAL_ROUTE_WORDS:
        route_tier = ROUTE_LOCAL
    elif words & _SYSTEMIC_FORMS:
        route_tier = ROUTE_SYSTEMIC
    elif words & _LOCAL_FORMS:
        route_tier = ROUTE_LOCAL
    else:
        route_tier = ROUTE_OTHER

    query_words = _words(drug_name)
    core = query_words[0] if query_words else ""
    name_match = bool(core) and any(core in _words(s) for s in ingredients)

    return {
        "excluded": excluded,
        "combination": combination,
        "otc": doctype == DOCTYPE_HUMAN_OTC,
        "route_tier": route_tier,
        "modified": bool(_MODIFIED_FORM_RE.search(body)),
        "name_match": name_match,
        "published": published_date(hit),
        "forms": frozenset(words & _PRIMARY_FORMS) or frozenset(words & _FORM_WORDS),
    }


def rank_label_candidates(hits: list[dict], drug_name: str) -> list[dict]:
    """Order list-endpoint *hits* from the label most likely to carry the drug's
    full indications to the least, dropping animal labels.

    Criteria, in order:

    1. exclude non-human labels (``doctype`` outside :data:`HUMAN_DOCTYPES`, or
       veterinary words in the title when no ``doctype`` is attached);
    2. an ingredient list that matches the query's shape — single-ingredient
       labels for a single-ingredient drug, combination labels for a
       combination drug;
    3. human prescription over human OTC;
    4. systemic routes (tablet, capsule, injection, oral solution) over
       inhaled/unknown over local (topical, ophthalmic, otic, rectal);
    5. the plain formulation over liposomal / extended-release style variants;
    6. the drug's name appearing in the ingredient list;
    7. most recently published, then highest ``spl_version``, then setid.

    The list endpoint does not say whether a label has an Indications section;
    :func:`acquire` checks that on the downloaded XML. Pure: no network, and
    the result does not depend on the order of *hits*.
    """
    query_combo = is_combination(drug_name)
    keyed = []
    for hit in hits:
        if not isinstance(hit, dict) or not str(hit.get("setid") or "").strip():
            continue
        f = label_features(hit, drug_name)
        if f["excluded"]:
            continue
        try:
            version = int(hit.get("spl_version") or 0)
        except (TypeError, ValueError):
            version = 0
        key = (
            f["combination"] != query_combo,
            f["otc"],
            f["route_tier"],
            f["modified"],
            not f["name_match"],
            -f["published"].toordinal(),
            -version,
            str(hit["setid"]).strip(),
        )
        keyed.append((key, hit))
    keyed.sort(key=lambda kv: kv[0])
    return [hit for _, hit in keyed]


def select_labels(ranked: list[dict], drug_name: str, n: int) -> list[dict]:
    """Up to *n* of the *ranked* hits, preferring distinct dosage forms.

    Many drugs have dozens of near-identical repackager labels; taking the top
    *n* by rank alone would keep three hydromorphone injections and no tablet.
    The first pass takes, in rank order, each hit whose (route, modifier,
    forms) signature has not been taken yet; the second pass fills any
    remaining slots in rank order.
    """
    if n <= 0:
        return []
    chosen: list[dict] = []
    seen_sig: set[tuple] = set()
    for hit in ranked:
        f = label_features(hit, drug_name)
        sig = (f["route_tier"], f["modified"], f["forms"])
        if sig in seen_sig:
            continue
        seen_sig.add(sig)
        chosen.append(hit)
        if len(chosen) >= n:
            return chosen
    for hit in ranked:
        if len(chosen) >= n:
            break
        if hit not in chosen:
            chosen.append(hit)
    return chosen


# --------------------------------------------------------------------------- #
# Cache entries
# --------------------------------------------------------------------------- #

def cached_setids(entry: dict | None) -> list[str]:
    """Every setid a cache entry selects, top-ranked first.

    Reads both the original single-``setid`` shape and the ranked shape with
    ``alternate_setids``; a ``no_results`` entry yields ``[]``.
    """
    if not isinstance(entry, dict):
        return []
    setids: list[str] = []
    top = str(entry.get("setid") or "").strip()
    if top:
        setids.append(top)
    for alt in entry.get("alternate_setids") or []:
        alt = str(alt or "").strip()
        if alt and alt not in setids:
            setids.append(alt)
    return setids


def is_ranked_entry(entry: dict | None) -> bool:
    """True when the entry was written by the ranking acquirer (#98).

    Older entries hold the single most recently published setid and are
    re-resolved rather than trusted.
    """
    return isinstance(entry, dict) and "alternate_setids" in entry


def has_indications_section(content: bytes) -> bool:
    """True when SPL XML *content* carries an Indications & Usage section."""
    return re.search(rb'<code[^>]*\bcode="' + LOINC_INDICATIONS.encode() + rb'"', content) is not None


# --------------------------------------------------------------------------- #
# Network
# --------------------------------------------------------------------------- #

def _search_labels(
    client: httpx.Client, drug_name: str, delay: float = 0.0, pagesize: int = PAGESIZE
) -> list[dict]:
    """Human-label hits for *drug_name*: one page per human document type.

    Each hit is the endpoint's dict plus the ``doctype`` it was fetched under,
    so the ranking can tell OTC from prescription and the pool never holds an
    animal label. One request per doctype at the API's maximum page size; no
    page walking.
    """
    hits: list[dict] = []
    for doctype in HUMAN_DOCTYPES:
        try:
            resp = client.get(
                SPLS_LIST_URL,
                params={"drug_name": drug_name, "doctype": doctype, "pagesize": pagesize},
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as e:  # noqa: BLE001 — network/parse errors are expected
            logger.debug("SPL list lookup failed for %r (%s): %s", drug_name, doctype, e)
            payload = {}
        finally:
            if delay:
                time.sleep(delay)
        for entry in (payload.get("data") or []) if isinstance(payload, dict) else []:
            if isinstance(entry, dict):
                hits.append({**entry, "doctype": doctype})
    return hits


def _resolve_candidates(
    client: httpx.Client, names: list[str], delay: float
) -> tuple[list[dict], str]:
    """Ranked human-label hits pooled over **every** name in *names*, with the name that
    produced the top-ranked hit.

    Not "the first name that yields any". A drug's indications are split across its
    spellings as well as across its manufacturers: paracetamol and acetaminophen labels are
    different documents stating different things, and stopping at the first productive
    spelling keeps whichever subset that one happens to carry. Querying every candidate
    generic-first is what took the name pool from 2,591 to 5,008 and is the half of the
    recall fix that ranking does not replace.

    Each name's hits are ranked against *that* name before the pools are concatenated, so a
    synonym's labels are judged on their own ingredient match rather than against a
    spelling they do not contain. Name order carries through: generic spellings first, so
    an equally-ranked hit under the drug's own name wins the slot.
    """
    pooled: list[dict] = []
    seen: set[str] = set()
    top_name = names[0] if names else ""
    for name in names:
        ranked = rank_label_candidates(_search_labels(client, name, delay=delay), name)
        if ranked and not pooled:
            top_name = name
        for hit in ranked:
            setid = str(hit.get("setid") or "").strip()
            if setid and setid not in seen:
                seen.add(setid)
                pooled.append(hit)
    return pooled, top_name


def _fetch_spl_xml(client: httpx.Client, setid: str) -> bytes | None:
    """Download a single SPL XML by setid; None on failure or a non-SPL body."""
    try:
        resp = client.get(SPL_XML_URL.format(setid=setid))
        resp.raise_for_status()
        content = resp.content
    except Exception as e:  # noqa: BLE001
        logger.debug("SPL XML download failed for %s: %s", setid, e)
        return None
    if not content or b"<document" not in content[:5000]:
        logger.debug("SPL XML for %s missing <document> root; skipping", setid)
        return None
    return content


def _download_spl_xml(
    client: httpx.Client, setid: str, out_path: Path
) -> bool:
    """Download a single SPL XML by setid. Returns True on success."""
    content = _fetch_spl_xml(client, setid)
    if content is None:
        return False
    out_path.write_bytes(content)
    return True


def _secure_label(
    client: httpx.Client, setid: str, data_dir: Path, counts: dict, delay: float
) -> bytes | None:
    """The SPL XML for *setid*, from disk or downloaded; None when unavailable."""
    out_path = data_dir / f"{setid}.xml"
    if out_path.exists():
        counts["already_on_disk"] += 1
        return out_path.read_bytes()
    content = _fetch_spl_xml(client, setid)
    if delay:
        time.sleep(delay)
    if content is None:
        counts["errors"] += 1
        return None
    out_path.write_bytes(content)
    counts["downloaded"] += 1
    return content


def acquire(
    drug_list_path: Path = DEFAULT_DRUG_LIST,
    data_dir: Path = DEFAULT_DATA_DIR,
    limit: int = 0,
    delay: float = 0.2,
    timeout: float = 30.0,
    labels_per_drug: int = DEFAULT_LABELS_PER_DRUG,
    transport: httpx.BaseTransport | None = None,
) -> dict:
    """Acquire SPL XML for USA-approved drugs into *data_dir*.

    For each drug, keeps up to *labels_per_drug* ranked labels that carry an
    Indications section (trying at most twice that many candidates), and
    records them in the acquire cache as ``setid`` (top-ranked) plus
    ``alternate_setids``. Returns a summary dict with counts. Resumable:
    ranked cache entries are reused and setids whose XML already exists on
    disk are not re-downloaded. *transport* lets tests supply an
    :class:`httpx.MockTransport`.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    drugs = _usa_drug_names(drug_list_path, limit=limit)
    cache = _get_cache()

    counts = {"requested": len(drugs), "resolved": 0, "downloaded": 0,
              "cached_hit": 0, "stale_entry": 0, "no_spl": 0, "errors": 0,
              "already_on_disk": 0, "labels": 0, "no_indications": 0}

    with httpx.Client(timeout=timeout, follow_redirects=True, transport=transport) as client:
        for i, names in enumerate(drugs, 1):
            key = names[0].strip().lower()
            cached = cache.get(key)
            if is_ranked_entry(cached):
                setids = cached_setids(cached)
                if not setids:
                    counts["no_spl"] += 1
                    continue
                counts["cached_hit"] += 1
                counts["resolved"] += 1
                for setid in setids:
                    if _secure_label(client, setid, data_dir, counts, delay) is not None:
                        counts["labels"] += 1
                continue

            if cached is not None:
                counts["stale_entry"] += 1
            ranked, queried = _resolve_candidates(client, names, delay)
            if not ranked:
                cache.put(key, {"setid": "", "alternate_setids": [],
                                "match": "no_results", "queried": names})
                counts["no_spl"] += 1
                continue

            ordered = select_labels(ranked, queried, len(ranked))
            kept: list[dict] = []
            for hit in ordered[: 2 * labels_per_drug]:
                if len(kept) >= labels_per_drug:
                    break
                setid = str(hit["setid"]).strip()
                content = _secure_label(client, setid, data_dir, counts, delay)
                if content is None:
                    continue
                if not has_indications_section(content):
                    counts["no_indications"] += 1
                    logger.debug("No indications section in %s (%s)", setid, hit.get("title"))
                    continue
                kept.append(hit)
            if not kept:
                # Nothing usable among the candidates; record the top-ranked
                # label so the choice is visible, but it carries no indications.
                kept = ordered[:1]
            counts["resolved"] += 1
            counts["labels"] += len(kept)
            cache.put(key, {
                "setid": str(kept[0]["setid"]).strip(),
                "alternate_setids": [str(h["setid"]).strip() for h in kept[1:]],
                "queried_name": queried,
                "labels": [
                    {"setid": str(h["setid"]).strip(),
                     "title": str(h.get("title") or "")[:200],
                     "published_date": str(h.get("published_date") or ""),
                     "doctype": str(h.get("doctype") or "")}
                    for h in kept
                ],
            })

            if i % 100 == 0:
                logger.info(
                    "Acquire progress: %d/%d (downloaded=%d, cached=%d, no_spl=%d)",
                    i, len(drugs), counts["downloaded"],
                    counts["cached_hit"], counts["no_spl"],
                )
                cache.flush()

    cache.flush()
    logger.info("Acquire done: %s", counts)
    return counts


def main():
    parser = argparse.ArgumentParser(
        description="Acquire DailyMed SPL XML via the v2 API into data/raw/dailymed/"
    )
    parser.add_argument("--drug-list", type=Path, default=DEFAULT_DRUG_LIST)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Limit number of drugs to acquire (0 = all USA-approved)",
    )
    parser.add_argument("--delay", type=float, default=0.2,
                        help="Seconds to sleep between API requests")
    parser.add_argument(
        "--labels-per-drug", type=int, default=DEFAULT_LABELS_PER_DRUG,
        help="How many ranked labels with an Indications section to keep per drug",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    acquire(
        drug_list_path=args.drug_list,
        data_dir=args.data_dir,
        limit=args.limit,
        delay=args.delay,
        labels_per_drug=args.labels_per_drug,
    )


if __name__ == "__main__":
    main()

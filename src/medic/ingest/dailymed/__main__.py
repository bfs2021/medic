"""DailyMed indication/contraindication extraction pipeline.

Parses raw FDA SPL XML labels, extracts indications and contraindications,
grounds diseases to Mondo CURIEs and drugs to ChEBI CURIEs, filters
allergen/diagnostic agents, and writes structured YAML output.

The SPL-XML mining path is the single acquisition path. SPL XML is acquired
from the DailyMed v2 REST API by ``medic.ingest.dailymed.acquire`` (driven by
the USA-approved drugs in ``products/drug_list.yaml``) into ``data/raw/dailymed/``.
An empty SPL directory is a hard error — the ingester never degrades to any
legacy table.

Source isolation (docs/source-isolation.md): every evidence row emitted here is
USA-jurisdiction only.
"""

import argparse
import hashlib
import logging
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from typing import NamedTuple
from pathlib import Path

import pandas as pd
import yaml

from medic.enrichment.cache import EnrichmentCache, flush_all
from medic.ingest.common import (
    _clean_for_yaml,
    looks_like_disease_name,
    should_skip_expensive_calls,
)
from medic.mention import mint_mention_id

logger = logging.getLogger(__name__)

NS = {"v3": "urn:hl7-org:v3"}

# LOINC section codes
LOINC_INDICATIONS = "34067-9"
LOINC_CONTRAINDICATIONS = "34070-3"

# Output paths
KB_INDICATIONS_DIR = Path("kb/indications/dailymed")
PRODUCTS_DIR = Path("products")

# Cache paths
# v2 = the verbatim-substring contract (#64). The prompt changed, so the old answers are
# answers to a different question: they carry no verbatim half and cannot be upgraded
# without asking again. New files rather than new keys, so the v1 caches stay on disk and a
# rollback does not have to re-extract anything.
DISEASE_CACHE_PATH = Path("cache/enrichment/dailymed_diseases_v2.json")
CONTRA_DISEASE_CACHE_PATH = Path("cache/enrichment/dailymed_contra_diseases_v2.json")
ALLERGEN_CACHE_PATH = Path("cache/enrichment/dailymed_allergen.json")


# ---------------------------------------------------------------------------
# SPL XML Mining
# ---------------------------------------------------------------------------


def _flatten_section_text(section: ET.Element) -> str:
    """Flatten an SPL section to plain text, including nested subsections.

    Real SPLs keep only a lead-in sentence ("... indicated for the treatment of
    patients with:") in the LOINC-coded section's own <text>, and put the actual
    indication list in <component><section> children. Reading only the direct
    <text> child therefore dropped the whole list on 23% of labels and returned a
    fragment on most of the rest, which left the LLM to supply the missing
    diseases from prior knowledge.

    <title> elements are included because a subsection's disease name frequently
    appears only in its heading. Structural elements (<code>, <ingredient>) are
    skipped so ingredient names cannot leak into the indication text.
    """
    return " ".join(_section_chunks(section))


def _section_chunks(section: ET.Element) -> list[str]:
    """The section's <title>/<text> elements, one string each, in document order.

    The structured form of :func:`_flatten_section_text`, which is the same thing joined
    with a space. That join is worth avoiding where it can be: a real SPL indications
    section carries the Full Prescribing Information body and the Highlights summary as two
    sibling <text> elements, and flattening them makes a `Limitations of Use` marker in the
    first appear to govern the second. `medic.spans` then reconstructed the lost boundary
    with a regex over the concatenation and got it wrong on 203 of the 306 labels that carry
    the marker (#65). Callers that build TextSpans should pass these chunks; callers that
    show text to an LLM still want the flat form, so both exist.
    """
    wanted = (f"{{{NS['v3']}}}title", f"{{{NS['v3']}}}text")
    chunks: list[str] = []
    for elem in section.iter():
        if elem.tag in wanted:
            raw = ET.tostring(elem, encoding="unicode")
            clean = re.sub(r"<[^>]+>", " ", raw)
            chunk = " ".join(clean.split())
            if chunk:
                chunks.append(chunk)
    return chunks


def extract_section_text(xml_path: str | Path, loinc_code: str) -> str:
    """Extract free-text from an SPL section identified by LOINC code."""
    tree = ET.parse(xml_path)
    return _extract_section_text_from_root(tree.getroot(), loinc_code)


def extract_active_ingredients(xml_path: str | Path) -> list[str]:
    """Extract active ingredient names from an SPL XML file."""
    tree = ET.parse(xml_path)
    root = tree.getroot()
    names: set[str] = set()
    for moiety_elem in root.iter(f"{{{NS['v3']}}}activeMoiety"):
        name = moiety_elem.find("v3:name", NS)
        if name is not None and name.text:
            names.add(name.text.strip().upper())
    return sorted(names)


#: FDA marketing-category codes (NCI Thesaurus) that mark a label as homeopathic.
_HOMEOPATHIC_CATEGORIES = frozenset({"C73614"})  # "unapproved homeopathic"

#: Backstop for a label that states its homeopathic status in prose without carrying the
#: category code. `HPUS` is the Homeopathic Pharmacopoeia of the United States.
_HOMEOPATHIC_TEXT = re.compile(r"(?i)\b(homeopathic|hpus)\b")


def is_homeopathic(root: ET.Element) -> bool:
    """Does this SPL describe a homeopathic product?

    Homeopathic labels are not regulatory approvals in any useful sense, but they carry an
    indications section and a long list of "active ingredients", so the per-ingredient
    extraction fans each one out into drug-disease edges: `coumarin -> fatigue`,
    `arsenite -> insomnia`, `skim milk -> fatigue`. 235 canonical indication pairs in the
    2026-10-13 export came from 28 such labels, and several drug nodes (skim milk, silicon
    dioxide, barium cation) exist for no other reason (#92).

    Decided on the SPL's own marketing category rather than a text match. Over the
    1,976-label corpus the two signals agree exactly — 28 files, neither alone missing any —
    so the structured field leads and the prose is only a backstop.
    """
    for code in root.iter(f"{{{NS['v3']}}}code"):
        if (code.get("code") or "") in _HOMEOPATHIC_CATEGORIES:
            return True
        if _HOMEOPATHIC_TEXT.search(code.get("displayName") or ""):
            return True
    for tag in ("title", "text"):
        for elem in root.iter(f"{{{NS['v3']}}}{tag}"):
            chunk = re.sub(r"<[^>]+>", " ", ET.tostring(elem, encoding="unicode"))
            if _HOMEOPATHIC_TEXT.search(chunk):
                return True
    return False


def _row_from_spl_root(root: ET.Element) -> dict | None:
    """Build a mined row dict from an already-parsed SPL XML root, or None.

    Returns None when the label is homeopathic (#92), or has no active ingredient or no
    indications/contraindications text worth keeping.
    """
    if is_homeopathic(root):
        return None
    indication_parts = _extract_section_chunks_from_root(root, LOINC_INDICATIONS)
    contra_parts = _extract_section_chunks_from_root(root, LOINC_CONTRAINDICATIONS)
    indications = " ".join(indication_parts)
    contras = " ".join(contra_parts)
    ingredients = _extract_ingredients_from_root(root)

    # Extract set_id from SPL document element. Real SPL XML carries the setid
    # in the `root` attribute (the `extension` attribute is unused here), so we
    # prefer `root` and only fall back to `extension`.
    set_id_elem = root.find(f"{{{NS['v3']}}}setId")
    set_id = ""
    if set_id_elem is not None:
        set_id = set_id_elem.get("root", "") or set_id_elem.get("extension", "")

    if ingredients and (indications or contras):
        return {
            "drug_names": ingredients,
            "indications_text": indications,
            "contraindications_text": contras,
            # The same text with the SPL's own element boundaries kept, so span building
            # does not have to guess them back out of the concatenation (#65). The flat
            # fields stay authoritative for anything that shows text to an LLM.
            "indications_text_parts": indication_parts,
            "contraindications_text_parts": contra_parts,
            "set_id": set_id,
        }
    return None


def mine_spl_labels(data_dir: Path, max_labels: int = 0) -> pd.DataFrame:
    """Mine SPL labels in *data_dir* and return a DataFrame.

    Accepts two on-disk layouts, so both acquisition paths work:

    - **Per-setid XML files** (``<setid>.xml``) written by
      ``medic.ingest.dailymed.acquire`` (the primary path — DailyMed v2 API).
    - **Bulk-release ZIP archives** (``*.zip``), each containing one SPL XML
      (the DailyMed full-release download).

    Returns a DataFrame with columns:
        drug_names, indications_text, contraindications_text, set_id
    """
    if not data_dir.exists():
        logger.warning("DailyMed data directory not found: %s", data_dir)
        return pd.DataFrame()

    xml_files = sorted(data_dir.glob("*.xml"))
    zip_files = sorted(data_dir.glob("*.zip"))
    if not xml_files and not zip_files:
        logger.warning("No .xml or .zip SPL files found in %s", data_dir)
        return pd.DataFrame()

    if max_labels > 0:
        # Prefer XML files first, then top up with ZIPs, up to max_labels.
        xml_files = xml_files[:max_labels]
        remaining = max_labels - len(xml_files)
        zip_files = zip_files[:remaining] if remaining > 0 else []

    logger.info(
        "Processing %d SPL XML files and %d ZIP files from %s",
        len(xml_files), len(zip_files), data_dir,
    )

    rows: list[dict] = []

    for xml_path in xml_files:
        try:
            root = ET.fromstring(xml_path.read_bytes())
            row = _row_from_spl_root(root)
            if row is not None:
                rows.append(row)
        except ET.ParseError as exc:
            logger.warning("Skipping %s: %s", xml_path.name, exc)

    for zf_path in zip_files:
        try:
            with zipfile.ZipFile(zf_path, "r") as zf:
                xml_names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
                if not xml_names:
                    continue
                root = ET.fromstring(zf.read(xml_names[0]))
                row = _row_from_spl_root(root)
                if row is not None:
                    rows.append(row)
        except (zipfile.BadZipFile, ET.ParseError) as exc:
            logger.warning("Skipping %s: %s", zf_path.name, exc)

    logger.info(
        "Mined %d labels with data from %d XML + %d ZIP files",
        len(rows), len(xml_files), len(zip_files),
    )
    return pd.DataFrame(rows)


def _extract_section_text_from_root(root: ET.Element, loinc_code: str) -> str:
    """Extract section text directly from an already-parsed root element.

    Reads the whole matched section subtree — see `_flatten_section_text`.
    """
    section = _find_coded_section(root, loinc_code)
    return _flatten_section_text(section) if section is not None else ""


def _find_coded_section(root: ET.Element, loinc_code: str) -> ET.Element | None:
    """The first <section> carrying ``loinc_code``, or None.

    First, not all: only 3 of 1,976 acquired labels carry the code on more than one
    section, so the duplication that used to show up in the extracted text came from
    sibling <text> elements inside one section, not from two sections being joined.
    """
    for section in root.iter(f"{{{NS['v3']}}}section"):
        code = section.find("v3:code", NS)
        if code is not None and code.get("code") == loinc_code:
            return section
    return None


def _extract_section_chunks_from_root(root: ET.Element, loinc_code: str) -> list[str]:
    """The coded section's element-level chunks (see :func:`_section_chunks`)."""
    section = _find_coded_section(root, loinc_code)
    return _section_chunks(section) if section is not None else []


def _extract_ingredients_from_root(root: ET.Element) -> list[str]:
    """Extract active ingredients from an already-parsed root element."""
    names: set[str] = set()
    for moiety_elem in root.iter(f"{{{NS['v3']}}}activeMoiety"):
        name = moiety_elem.find("v3:name", NS)
        if name is not None and name.text:
            names.add(name.text.strip().upper())
    return sorted(names)


# ---------------------------------------------------------------------------
# LLM-based extraction
# ---------------------------------------------------------------------------

_disease_cache: EnrichmentCache | None = None
_contra_disease_cache: EnrichmentCache | None = None
_allergen_cache: EnrichmentCache | None = None


def _get_disease_cache() -> EnrichmentCache:
    global _disease_cache
    if _disease_cache is None:
        _disease_cache = EnrichmentCache(DISEASE_CACHE_PATH)
    return _disease_cache


def _get_contra_disease_cache() -> EnrichmentCache:
    global _contra_disease_cache
    if _contra_disease_cache is None:
        _contra_disease_cache = EnrichmentCache(CONTRA_DISEASE_CACHE_PATH)
    return _contra_disease_cache


def _get_allergen_cache() -> EnrichmentCache:
    global _allergen_cache
    if _allergen_cache is None:
        _allergen_cache = EnrichmentCache(ALLERGEN_CACHE_PATH)
    return _allergen_cache


def _text_hash(text: str) -> str:
    """Deterministic short hash for cache keying."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# A cache miss under MEDIC_SKIP_EXPENSIVE_CALLS used to return [] silently, so the
# "cheap" rebuild path *dropped rows* instead of failing — a build that looks like a
# success and quietly ships fewer indications. Count them and fail at the end of the
# ingest with the full tally, rather than raising on the first (same reasoning as
# `_report_failures` in on_label_merge: the count is the signal).
_skipped_uncached: dict[str, int] = {}

#: Flush the LLM caches to disk every N new entries, not only at the end of `main()`.
#:
#: `EnrichmentCache.put` mutates an in-memory dict; persistence happens in `flush()`. With the
#: only flush at the end of the run, a crash or an interrupt part-way through a multi-thousand
#: call ingest discarded every answer bought so far and the next run paid for them again. A
#: full DailyMed rebuild is ~3,578 calls, so the exposure is hours of wall clock and real
#: money. Checkpointing costs one small write per 100 answers.
_FLUSH_EVERY = 100
_writes_since_flush = 0


def _checkpoint(cache) -> None:
    """Count one new cache entry and flush periodically so work survives a crash."""
    global _writes_since_flush
    _writes_since_flush += 1
    if _writes_since_flush >= _FLUSH_EVERY:
        _writes_since_flush = 0
        flush_all()
        logger.info("cache checkpoint: flushed after %d new entries", _FLUSH_EVERY)


def _note_skipped_uncached(kind: str) -> None:
    _skipped_uncached[kind] = _skipped_uncached.get(kind, 0) + 1


def _raise_if_rows_were_silently_dropped() -> None:
    if not _skipped_uncached:
        return
    if os.environ.get("MEDIC_ALLOW_UNCACHED_DROPS", "").strip() in ("1", "true", "yes"):
        logger.warning(
            "MEDIC_ALLOW_UNCACHED_DROPS set: %s extraction(s) dropped uncached",
            _skipped_uncached,
        )
        return
    detail = ", ".join(f"{n} {kind}" for kind, n in sorted(_skipped_uncached.items()))
    raise RuntimeError(
        f"MEDIC_SKIP_EXPENSIVE_CALLS is set and {detail} extraction(s) were not in the "
        "committed cache, so those rows would be silently dropped. Either run without "
        "MEDIC_SKIP_EXPENSIVE_CALLS to populate the cache (and commit it), or accept a "
        "partial build explicitly with MEDIC_ALLOW_UNCACHED_DROPS=1."
    )


# Real disease names rarely exceed 200 chars; anything longer is almost
# certainly LLM prose (refusal explanation, hedge, instruction echo) that
# leaked past the "None" sentinel.
_MAX_DISEASE_NAME_LEN = 200


#: Separator between the canonical disease name and the verbatim source substring.
_VERBATIM_SEP = "::"


class Extracted(NamedTuple):
    """One extracted disease: the canonical name, and the source's own wording.

    ``verbatim`` is the substring the LLM copied out of the text it was shown, and it is
    the half that makes the downstream checks work. The prompt asks for a canonicalised
    name, so the name frequently does not occur in the source — 1,414 of 11,696 shipped
    INDICATIONs (12%) could not be located in their own section text, which left the
    negation screen unable to judge polarity and left `char_start`/`char_end` unrecordable
    (#64). The verbatim form is present by construction.

    Empty when the model ignored the format or when the answer came from a cache written
    before this existed. Callers must treat it as optional, never assume it is findable —
    a model can hallucinate a quote as easily as a name.
    """

    name: str
    verbatim: str = ""


def _parse_llm_disease_list(text: str) -> list[Extracted]:
    """Parse the LLM's pipe-separated disease list, robust to refusal prose.

    The prompts ask for `name :: verbatim|name :: verbatim|...` or the literal token
    `None`. A bare `name` with no separator still parses, with an empty verbatim: cached
    answers predate the format, and a model sometimes ignores it.

    In practice the model sometimes returns prose like
    `"None\\n\\nThe text lists diagnostic procedures..."` when the input has
    no diseases. The naive parser treats that whole prose as a single
    "disease" and sends it to the grounder. Three defenses here: (a) treat any
    response that *starts* with "none" as empty, (b) drop any item over
    `_MAX_DISEASE_NAME_LEN` since real disease names don't run that long, and
    (c) apply `looks_like_disease_name`, which rejects refusal-prefixed and
    sentence-shaped items. (c) used to live in `ema/__main__.py` and guarded only
    the EU path; running it here gives DailyMed, EMA, PMDA and India the same
    defence, for indications and contraindications alike (issue #59).

    All three defences judge the **name** only. The verbatim half is quoted source text,
    not a disease name, so the length cap and the sentence-shape guard would reject
    perfectly good quotes.
    """
    stripped = (text or "").strip()
    if not stripped or stripped.lower().startswith("none"):
        return []
    out: list[Extracted] = []
    for item in stripped.split("|"):
        name, sep, verbatim = item.partition(_VERBATIM_SEP)
        name = name.strip()
        verbatim = verbatim.strip() if sep else ""
        if not _is_usable_disease_name(name):
            continue
        out.append(Extracted(name, verbatim))
    return out


def _is_usable_disease_name(name: str) -> bool:
    return bool(
        name
        and name.lower() != "none"
        and len(name) <= _MAX_DISEASE_NAME_LEN
        and looks_like_disease_name(name)
    )




def _encode_extractions(items: list[Extracted]) -> list[list[str]]:
    """Cache form: ``[[name, verbatim], ...]``. JSON has no tuples, and a flat list of
    names could not carry the verbatim half."""
    return [[e.name, e.verbatim] for e in items]


def _decode_extractions(raw) -> list[Extracted]:
    """Rebuild from the cache, tolerating the v1 shape (a bare list of names).

    The v2 cache files are new, so a v1 payload should not appear — but a hand-edited or
    partially-migrated cache should degrade to "no verbatim" rather than crash the build.
    """
    out: list[Extracted] = []
    for item in raw or []:
        if isinstance(item, str):
            out.append(Extracted(item, ""))
        elif isinstance(item, (list, tuple)) and item:
            name = str(item[0])
            verbatim = str(item[1]) if len(item) > 1 and item[1] else ""
            out.append(Extracted(name, verbatim))
    return out


def _screen_negated_indications(
    diseases: list[Extracted], indication_text: str, source: str,
    parts: list[str] | None = None,
) -> list[Extracted]:
    """Drop extracted 'indications' the source actually negates/excludes (inversions).

    Deterministic prevention pass (FAILURE_MODES §4.1-4.2): a disease stated only inside
    a negation/exclusion scope ("should not be used in X", "except X") is not an approval
    and must not enter the indication records. Each drop is logged (nothing is dropped
    silently); the offline validator (`just validate-extraction`) is the detection net
    for anything that slips through. Raw LLM output is cached upstream, so re-screening a
    cache hit is free and stays correct if the cue list is tuned.

    ``source`` selects how the text is split into spans, so a cue inside a
    ``Limitations of Use`` subsection no longer reaches across into the indication
    sentence above it (issue #59).

    Names the screen could not locate are kept, but logged as a count: the check did not
    run on them, which is not the same as their passing it. The merge records the same
    fact per row as the ``polarity_unverified`` assertion flag.
    """
    from medic.validation.extraction_fidelity import screen_indications

    by_name = {e.name: e for e in diseases}
    result = screen_indications(
        [e.name for e in diseases], indication_text, source=source, parts=parts,
        verbatims={e.name: e.verbatim for e in diseases if e.verbatim})
    for d in result.dropped:
        logger.warning(
            "Dropping negated 'indication' %r (cue: %r, scope: %s) — source states it "
            "negatively, not as an approval",
            d["disease"], d["reason"], d.get("scope", "claim"),
        )
    for d in result.limitation_only:
        logger.warning(
            "Keeping %r although its only mention is a negated scope restriction "
            "(cue: %r) — evidence too weak to drop on, so the merge flags it "
            "negated_inversion and the reliability gate excludes it (#66)",
            d["disease"], d["reason"],
        )
    if result.unlocatable:
        logger.info(
            "Polarity not evaluable for %d/%d extracted indication(s) — not locatable in "
            "the source text (LLM canonicalisation): %s",
            len(result.unlocatable), len(diseases), result.unlocatable,
        )
    return [by_name[n] for n in result.kept]


def _screen_negated_contraindications(
    diseases: list[Extracted], contraindication_text: str, source: str,
    parts: list[str] | None = None,
) -> list[Extracted]:
    """The contraindication-side screen, which did not exist at all (issue #59).

    Opposite polarity to the indication screen: here "contraindicated in X" is the claim,
    and what negates it is the source denying it ("no known contraindications", "not
    contraindicated in X") or excepting the condition from a broader class. See
    ``extraction_fidelity._CONTRA_NEGATION_CUES``.
    """
    from medic.validation.extraction_fidelity import screen_contraindications

    by_name = {e.name: e for e in diseases}
    result = screen_contraindications(
        [e.name for e in diseases], contraindication_text, source=source, parts=parts,
        verbatims={e.name: e.verbatim for e in diseases if e.verbatim})
    for d in result.dropped:
        logger.warning(
            "Dropping non-contraindication %r (cue: %r) — source states the condition is "
            "not contraindicated", d["disease"], d["reason"],
        )
    if result.unlocatable:
        logger.info(
            "Polarity not evaluable for %d/%d extracted contraindication(s) — not "
            "locatable in the source text",
            len(result.unlocatable), len(diseases),
        )
    return [by_name[n] for n in result.kept]


def extract_diseases_from_text(
    indication_text: str, *, source: str = "DAILYMED",
    parts: list[str] | None = None,
) -> list[Extracted]:
    """Extract disease names from indication free text via LLM.

    The raw LLM extraction is cached; a deterministic negation screen then drops any
    disease the source states negatively (see :func:`_screen_negated_indications`).

    ``source`` is the ingester calling in (``EMA``, ``PMDA``, ``INDIA``, default
    ``DAILYMED``). It selects how the text is split into typed spans for the screen;
    only SPL sections have recoverable inner structure, so it is a no-op elsewhere.
    """
    if not indication_text:
        return []

    cache = _get_disease_cache()
    key = _text_hash(indication_text)
    cached = cache.get(key)
    if cached is not None:
        return _screen_negated_indications(
            _decode_extractions(cached.get("diseases")), indication_text, source, parts)

    if should_skip_expensive_calls():
        _note_skipped_uncached("indication")
        return []

    from medic.llm import llm_call
    text = llm_call(
        (
            "Extract all diseases mentioned as therapeutic indications from this text.\n"
            "For each disease return TWO parts separated by '::' —\n"
            "  <canonical disease name> :: <verbatim substring copied from the text>\n"
            "The second part MUST be copied character-for-character from the text above, "
            "including any abbreviation the text uses. Do not normalise or expand it.\n"
            "Return ONLY a pipe-separated list like: "
            "name1 :: quote1|name2 :: quote2\n"
            "If no diseases, return: None\n"
            "Do not infer diseases - only list those explicitly mentioned.\n"
            "Do not include contraindicated conditions.\n"
            "Be specific in the canonical name (e.g., 'type 2 diabetes mellitus' not just "
            "'diabetes'), but keep the quote exactly as written.\n\n"
            "Example: if the text says 'reducing the risk of nonfatal MI', return\n"
            "  myocardial infarction :: MI\n\n"
            f"Text: {indication_text[:3000]}"
        ),
        task="extraction",
        max_tokens=500,
        system="You are a biomedical expert. Extract disease names from drug indication text.",
    )
    diseases = _parse_llm_disease_list(text)

    # Cache the RAW extraction (faithful to the LLM); screen on return.
    cache.put(key, {"diseases": _encode_extractions(diseases),
                    "text_prefix": indication_text[:200]})
    _checkpoint(cache)
    return _screen_negated_indications(diseases, indication_text, source, parts)


def extract_contraindicated_diseases_from_text(
    contraindication_text: str, *, source: str = "DAILYMED",
    parts: list[str] | None = None,
) -> list[Extracted]:
    """Extract disease names from contraindication free text via LLM.

    Sister to `extract_diseases_from_text`, but tuned for contraindication
    sections. The indication prompt explicitly instructs the LLM to *exclude*
    contraindicated conditions; passing contra text through it produces empty
    or refusal-shaped output that grounds incorrectly. This function inverts
    that instruction so contra-side callers (DailyMed, EMA §4.3, PMDA) get
    sensible disease lists.

    Used by all contra ingest paths. Cache is namespaced separately
    (`dailymed_contra_diseases.json`) so it cannot collide with the indication
    cache even if the same source text is processed both ways.

    Like its indication sister, the cached raw extraction is screened on return —
    see :func:`_screen_negated_contraindications`. That screen did not exist before
    issue #59, so a "not contraindicated in X" sentence published X as a
    contraindication.
    """
    if not contraindication_text:
        return []

    cache = _get_contra_disease_cache()
    key = _text_hash(contraindication_text)
    cached = cache.get(key)
    if cached is not None:
        return _screen_negated_contraindications(
            _decode_extractions(cached.get("diseases")), contraindication_text,
            source, parts)

    if should_skip_expensive_calls():
        _note_skipped_uncached("contraindication")
        return []

    from medic.llm import llm_call
    text = llm_call(
        (
            "Extract all medical conditions or diseases mentioned as "
            "contraindications in this text. A contraindication is a condition "
            "that makes a drug inappropriate (e.g., 'patients with active "
            "infection', 'severe hepatic impairment', 'pregnancy').\n\n"
            "For each condition return TWO parts separated by '::' —\n"
            "  <canonical condition name> :: <verbatim substring copied from the text>\n"
            "The second part MUST be copied character-for-character from the text above, "
            "including any abbreviation the text uses. Do not normalise or expand it.\n"
            "Return ONLY a pipe-separated list like: "
            "name1 :: quote1|name2 :: quote2\n"
            "If no specific medical conditions are listed (e.g., the section only "
            "lists hypersensitivity to the drug itself), return: None\n"
            "Do not infer — only list conditions explicitly mentioned.\n"
            "Be specific in the canonical name (e.g., 'severe hepatic impairment' not just "
            "'liver disease'), but keep the quote exactly as written.\n"
            "Exclude generic hypersensitivity to the drug or excipients — that's "
            "trivial and not informative.\n"
            "Exclude pure procedural exclusions (e.g., 'concurrent use of MAOIs') "
            "unless they describe a medical condition.\n\n"
            f"Text: {contraindication_text[:3000]}"
        ),
        task="extraction",
        max_tokens=500,
        system=(
            "You are a biomedical expert. Extract the medical conditions named in "
            "drug contraindication text. Output a pipe-separated list of conditions, "
            "or 'None' if no specific conditions are present."
        ),
    )
    diseases = _parse_llm_disease_list(text)

    # Cache the RAW extraction (faithful to the LLM); screen on return.
    cache.put(
        key,
        {"diseases": _encode_extractions(diseases),
         "text_prefix": contraindication_text[:200]},
    )
    _checkpoint(cache)
    return _screen_negated_contraindications(
        diseases, contraindication_text, source, parts)


def is_allergen_or_diagnostic(drug_name: str) -> dict:
    """Check if a drug is primarily an allergen or diagnostic agent."""
    cache = _get_allergen_cache()
    key = drug_name.upper()
    cached = cache.get(key)
    if cached is not None:
        return {
            "is_allergen": cached.get("is_allergen", False),
            "is_diagnostic_agent": cached.get("is_diagnostic_agent", False),
        }

    if should_skip_expensive_calls():
        return {"is_allergen": False, "is_diagnostic_agent": False}

    from medic.llm import llm_call
    text = llm_call(
        (
            f"For the drug '{drug_name}', answer these two questions with TRUE or FALSE only:\n"
            "1. Is this primarily used as an allergen for allergy testing?\n"
            "2. Is this primarily used as a radiolabel or diagnostic agent?\n"
            "Format: allergen:TRUE/FALSE,diagnostic:TRUE/FALSE"
        ),
        task="classification",
        max_tokens=100,
        system="You are a pharmaceutical expert.",
    ).lower()
    result = {
        "is_allergen": "allergen:true" in text,
        "is_diagnostic_agent": "diagnostic:true" in text,
    }

    cache.put(key, result)
    _checkpoint(cache)
    return result


# ---------------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------------


_GROUNDING_SERVICES: dict[str, object] = {}


def _get_grounding_service(grounding_backend: str):
    """Return a process-cached grounding service for ``grounding_backend``.

    Caching the instance (rather than rebuilding per call via the factory) keeps a
    single lexical SSSOM store handle alive across a whole SPL batch, so the
    disease-grounding decision log can be flushed once at the end.
    """
    svc = _GROUNDING_SERVICES.get(grounding_backend)
    if svc is None:
        from medic.grounding.factory import get_grounding_service

        svc = get_grounding_service(grounding_backend)
        _GROUNDING_SERVICES[grounding_backend] = svc
    return svc


def _flush_dailymed_disease_grounding(grounding_backend: str) -> None:
    """Persist the disease-grounding SSSOM stores once after an SPL batch."""
    svc = _GROUNDING_SERVICES.get(grounding_backend)
    if svc is not None:
        from medic.ingest.grounding import flush_disease_grounding

        flush_disease_grounding(svc)


def _ground_disease(
    disease_name: str, grounding_backend: str, record: dict | None = None
) -> tuple[str, str]:
    """Ground a disease name to (CURIE, label) and attach structured grounding objects.

    When ``record`` is supplied, the deterministic two-stage resolve writes
    ``disease_grounding`` / ``disease_normalization`` onto it (funneled to the product
    association by the on-label merge) and sets the record's disease id/label keys. The
    (id, label) tuple is still returned so existing callers keep working unchanged.
    """
    from medic.ingest.grounding import resolve_disease_onto_record

    service = _get_grounding_service(grounding_backend)
    target = record if record is not None else {}
    disease_id = resolve_disease_onto_record(target, disease_name, service)
    if not disease_id:
        return "", ""
    return disease_id, target["final_normalized_disease_label"]


def _ground_drug(drug_name: str, grounding_backend: str) -> tuple[str, str]:
    """Ground a drug name to (CURIE, label) via the grounding cascade.

    Reuses the process-cached grounding service (via ``_get_grounding_service``) so the
    lexical index and its in-memory SSSOM store are loaded once for the whole SPL batch
    rather than rebuilt per drug — the per-call rebuild was the dominant DailyMed cost.
    """
    service = _get_grounding_service(grounding_backend)
    # Mint and pass the mention id (I-9). The grounding store is keyed by literal, so
    # dropping it here does not merely leave this row un-anchored — it overwrites the
    # MEDICNE id the drug-list ingest already wrote for the same drug name, which blanked
    # 1,285 rows on every full build.
    result = service.ground_drug_best(
        drug_name, mention_id=mint_mention_id(drug_name, "drugs"))
    if result:
        return result.id, result.label
    return "", ""


# ---------------------------------------------------------------------------
# Pipeline: from raw SPL to structured YAML
# ---------------------------------------------------------------------------


def _process_spl_data(
    spl_df: pd.DataFrame, grounding_backend: str
) -> tuple[list[dict], list[dict]]:
    """Process mined SPL data into indication and contraindication records."""
    indication_records: list[dict] = []
    contraindication_records: list[dict] = []

    for _, row in spl_df.iterrows():
        drug_names: list[str] = row["drug_names"]
        indications_text: str = row.get("indications_text", "")
        contras_text: str = row.get("contraindications_text", "")
        # The SPL's own element boundaries, so the destructive negation screen scopes cues
        # to the element that contains them rather than the whole concatenation (#65).
        ind_parts = row.get("indications_text_parts")
        contra_parts_row = row.get("contraindications_text_parts")
        set_id: str = row.get("set_id", "")

        # Ground each drug
        for drug_name in drug_names:
            drug_id, drug_label = _ground_drug(drug_name, grounding_backend)
            if not drug_id:
                logger.debug("Could not ground drug: %s", drug_name)
                continue

            # Allergen/diagnostic check (degrade gracefully on LLM/network error;
            # num_retries in llm_call already handles transient blips)
            try:
                allergen_info = is_allergen_or_diagnostic(drug_name)
            except Exception as exc:
                logger.warning("Allergen/diagnostic check failed for %s (%s); assuming neither.",
                               drug_name, exc)
                allergen_info = {"is_allergen": False, "is_diagnostic_agent": False}

            # Extract and ground diseases from indications
            if indications_text:
                try:
                    diseases = extract_diseases_from_text(
                        indications_text, source="DAILYMED", parts=ind_parts)
                except Exception as exc:
                    logger.warning("Disease extraction failed for setid %s (%s); skipping.",
                                   set_id, exc)
                    diseases = []
                for _extracted in diseases:
                    disease_name, disease_verbatim = (
                        _extracted.name, _extracted.verbatim)
                    record: dict = {}
                    disease_id, disease_label = _ground_disease(
                        disease_name, grounding_backend, record
                    )
                    if not disease_id:
                        logger.debug("Could not ground disease: %s", disease_name)
                        continue

                    evidence_item = {
                        "source_type": "REGULATORY",
                        "jurisdiction": "USA",
                        "confidence": "HIGH",
                        "approval_status": "APPROVED",
                        "explanation": "FDA-approved indication from DailyMed structured product label",
                        "source_role": "INTERMEDIARY",
                    }
                    # URL policy per SPEC §5.6 / architecture §5.6: reference is
                    # the setid landing page (lookup.cfm), source_document_url is
                    # the deterministic label PDF (downloadpdffile.cfm).
                    if set_id:
                        evidence_item["reference"] = f"https://dailymed.nlm.nih.gov/dailymed/lookup.cfm?setid={set_id}"
                        evidence_item["source_document_url"] = f"https://dailymed.nlm.nih.gov/dailymed/downloadpdffile.cfm?setid={set_id}"
                        evidence_item["setid"] = set_id
                    else:
                        import urllib.parse
                        evidence_item["reference"] = f"https://dailymed.nlm.nih.gov/dailymed/search.cfm?labeltype=all&query={urllib.parse.quote_plus(drug_label)}"
                    if indications_text:
                        evidence_item["snippet"] = indications_text[:500]
                    # Preserve raw source strings for audit
                    evidence_item["original_drug_label"] = drug_name
                    if set_id:
                        evidence_item["original_drug_id"] = set_id
                    evidence_item["original_disease_label"] = disease_name
                    evidence_item["original_disease_verbatim"] = disease_verbatim

                    record.update(
                        {
                            "drug_disease": f"{drug_id}|{disease_id}",
                            "final_normalized_drug_id": drug_id,
                            "final_normalized_drug_label": drug_label,
                            "final_normalized_disease_id": disease_id,
                            "final_normalized_disease_label": disease_label,
                            "fda": True,
                            "ema": False,
                            "pmda": False,
                            "relationship_type": "INDICATION",
                            "indications_text": indications_text,
                            "is_allergen": allergen_info["is_allergen"],
                            "is_diagnostic_agent": allergen_info[
                                "is_diagnostic_agent"
                            ],
                            "set_id": set_id,
                            "evidence": [evidence_item],
                        }
                    )
                    indication_records.append(record)

            # Extract and ground diseases from contraindications
            if contras_text:
                try:
                    contra_diseases = extract_contraindicated_diseases_from_text(
                        contras_text, source="DAILYMED", parts=contra_parts_row)
                except Exception as exc:
                    logger.warning("Contraindication extraction failed for setid %s (%s); skipping.",
                                   set_id, exc)
                    contra_diseases = []
                for _extracted in contra_diseases:
                    disease_name, disease_verbatim = (
                        _extracted.name, _extracted.verbatim)
                    record = {}
                    disease_id, disease_label = _ground_disease(
                        disease_name, grounding_backend, record
                    )
                    if not disease_id:
                        continue

                    contra_evidence = {
                        "source_type": "REGULATORY",
                        "jurisdiction": "USA",
                        "confidence": "HIGH",
                        "approval_status": "APPROVED",
                        "explanation": "FDA contraindication from DailyMed structured product label",
                        "source_role": "INTERMEDIARY",
                    }
                    if set_id:
                        contra_evidence["reference"] = f"https://dailymed.nlm.nih.gov/dailymed/lookup.cfm?setid={set_id}"
                        contra_evidence["source_document_url"] = f"https://dailymed.nlm.nih.gov/dailymed/downloadpdffile.cfm?setid={set_id}"
                        contra_evidence["setid"] = set_id
                    else:
                        import urllib.parse
                        contra_evidence["reference"] = f"https://dailymed.nlm.nih.gov/dailymed/search.cfm?labeltype=all&query={urllib.parse.quote_plus(drug_label)}"
                    if contras_text:
                        contra_evidence["snippet"] = contras_text[:500]
                    contra_evidence["original_drug_label"] = drug_name
                    if set_id:
                        contra_evidence["original_drug_id"] = set_id
                    contra_evidence["original_disease_label"] = disease_name
                    contra_evidence["original_disease_verbatim"] = disease_verbatim

                    record.update(
                        {
                            "drug_disease": f"{drug_id}|{disease_id}",
                            "final_normalized_drug_id": drug_id,
                            "final_normalized_drug_label": drug_label,
                            "final_normalized_disease_id": disease_id,
                            "final_normalized_disease_label": disease_label,
                            "fda": True,
                            "ema": False,
                            "pmda": False,
                            "relationship_type": "CONTRAINDICATION",
                            "indications_text": contras_text,
                            "is_allergen": allergen_info["is_allergen"],
                            "is_diagnostic_agent": allergen_info[
                                "is_diagnostic_agent"
                            ],
                            "set_id": set_id,
                            "evidence": [contra_evidence],
                        }
                    )
                    contraindication_records.append(record)

    _flush_dailymed_disease_grounding(grounding_backend)
    return indication_records, contraindication_records


# ---------------------------------------------------------------------------
# Output writing
# ---------------------------------------------------------------------------


def _write_yaml(records: list[dict], path: Path) -> None:
    """Write records to a YAML file with cleaning."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cleaned = _clean_for_yaml(records)
    content = yaml.dump(cleaned, default_flow_style=False, allow_unicode=True, width=1000)
    content = "".join(c for c in content if c == "\n" or c == "\t" or ord(c) >= 32)
    with open(path, "w") as f:
        f.write(content)
    logger.info("Wrote %d records to %s", len(records), path)


def _write_output(
    indication_records: list[dict], contraindication_records: list[dict]
) -> None:
    """Write per-source YAML and merged product files."""
    # Per-source YAML
    if indication_records:
        _write_yaml(indication_records, KB_INDICATIONS_DIR / "indications.yaml")
    if contraindication_records:
        _write_yaml(
            contraindication_records, KB_INDICATIONS_DIR / "contraindications.yaml"
        )

    if contraindication_records:
        product_path = PRODUCTS_DIR / "contraindication_list.yaml"
        product_path.parent.mkdir(parents=True, exist_ok=True)
        cleaned = _clean_for_yaml({"associations": contraindication_records})
        content = yaml.dump(
            cleaned, default_flow_style=False, allow_unicode=True, width=1000
        )
        content = "".join(
            c for c in content if c == "\n" or c == "\t" or ord(c) >= 32
        )
        with open(product_path, "w") as f:
            f.write(content)
        logger.info(
            "Wrote %d contraindications to %s",
            len(contraindication_records),
            product_path,
        )


# ---------------------------------------------------------------------------
# Main CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="DailyMed indication/contraindication extraction pipeline"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data/raw/dailymed/"),
        help="Path to directory containing DailyMed SPL ZIP files",
    )
    parser.add_argument(
        "--grounding-backend",
        default="lexical",
        help="Grounding backend to use (default: lexical — the deterministic two-stage grounder)",
    )
    parser.add_argument(
        "--max-labels",
        type=int,
        default=0,
        help="Limit number of labels to process (0 = all)",
    )
    parser.add_argument(
        "--acquire",
        action="store_true",
        help=(
            "Before mining, fetch SPL XML for USA-approved drugs from the "
            "DailyMed v2 API into --data-dir (see medic.ingest.dailymed.acquire)."
        ),
    )
    parser.add_argument(
        "--acquire-limit",
        type=int,
        default=0,
        help="With --acquire, cap the number of drugs to fetch (0 = all).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    # Optionally acquire real SPL XML first (the acquisition step of the single
    # SPL-XML path).
    if args.acquire:
        from medic.ingest.dailymed.acquire import acquire as acquire_spl
        acquire_spl(data_dir=args.data_dir, limit=args.acquire_limit)

    # Single path: mine real SPL XML.
    spl_df = mine_spl_labels(args.data_dir, max_labels=args.max_labels)

    if spl_df.empty:
        raise SystemExit(
            f"No SPL XML found in {args.data_dir}. The SPL-XML path is the only "
            "DailyMed acquisition path. Populate it first with:\n"
            "    just ingest-dailymed-acquire\n"
            "or run this command with --acquire."
        )

    logger.info("Processing %d mined SPL labels (SPL-XML path)", len(spl_df))
    indication_records, contraindication_records = _process_spl_data(
        spl_df, args.grounding_backend
    )

    _write_output(indication_records, contraindication_records)

    # Flush caches. `EnrichmentCache.put` only mutates the in-memory dict, so a cache that
    # is never flushed is silently re-queried on every build. `_contra_disease_cache` used
    # to be missing from a hand-written list here: its 2,484 LLM calls ran on every run, and
    # because the extraction is not deterministic the contraindication count moved between
    # otherwise identical builds (2,399 -> 2,442 -> 2,445) while indications stayed pinned
    # at 6,504. `just determinism` could not see it — it re-runs the merge twice, never the
    # extraction. `flush_all` flushes whatever caches exist, so the next cache added cannot
    # repeat it (#57).
    flush_all()

    # Fail loudly if the "cheap" path dropped extractions rather than shipping a
    # quietly under-populated build. Runs after the flushes so whatever work *was*
    # done is still persisted.
    _raise_if_rows_were_silently_dropped()
    try:
        from medic.ingest.dailymed.setid_lookup import (
            flush_cache as flush_setid_cache,
            log_failure_summary,
            lookup_failure_summary,
        )
        flush_setid_cache()
        log_failure_summary()
        # Persist setid resolution outcomes alongside the indications so they
        # ship with the kb (visible in the next run's QA without re-querying).
        report_path = Path("kb/indications/dailymed/setid_lookup_report.yaml")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            yaml.dump(
                lookup_failure_summary(),
                default_flow_style=False,
                allow_unicode=True,
            )
        )
        logger.info("Wrote DailyMed setid lookup report to %s", report_path)
    except Exception as e:
        logger.debug("setid summary write failed: %s", e)

    logger.info(
        "Done: %d indications, %d contraindications",
        len(indication_records),
        len(contraindication_records),
    )


if __name__ == "__main__":
    main()

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
(``products/drug_list.yaml``): for each USA-approved drug we query

    https://dailymed.nlm.nih.gov/dailymed/services/v2/spls.json?drug_name=<NAME>

pick the most recently published SPL setid, and download the clean per-setid
SPL XML

    https://dailymed.nlm.nih.gov/dailymed/services/v2/spls/<SETID>.xml

into ``data/raw/dailymed/<SETID>.xml``. This fetches exactly one representative
label per drug we actually care about (a few thousand small XML files, a few
hundred MB) rather than the entire corpus.

The module is resumable (existing setid files are skipped) and rate-limited
with a small inter-request delay. A manifest mapping drug name -> setid is
cached to ``cache/enrichment/dailymed_acquire.json``.

Source isolation (docs/source-isolation.md): this only fetches FDA SPL labels
and only ever produces USA-jurisdiction evidence downstream.
"""

from __future__ import annotations

import argparse
import logging
import time
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

_cache: EnrichmentCache | None = None


def _get_cache() -> EnrichmentCache:
    global _cache
    if _cache is None:
        _cache = EnrichmentCache(ACQUIRE_CACHE_PATH)
    return _cache


def _usa_drug_names(drug_list_path: Path, limit: int = 0) -> list[str]:
    """Return distinct USA-approved drug query names from the merged drug list.

    **Every** candidate name per drug, not one representative. The previous version took the
    first `source_ingredients` string and `break`ed, on the stated assumption that upstream
    label spelling gives the best DailyMed hits. For a large minority it gives none: the drug
    list holds product-style strings like "Lansoprazole Enteric-Coated Capsules", which
    DailyMed's `drug_name` index does not match, while `curie_label` is "lansoprazole", which
    it does. 1,074 of 2,591 names (41%) returned no SPL at all, and lansoprazole's 20 labels
    were never requested — which is why broadening to 20 setids per name recovered its
    indications on paper and not in fact.

    `curie_label` comes first because the normalized generic name is what DailyMed indexes;
    the source strings follow, since a brand or salt spelling occasionally hits where the
    generic misses. Querying both is cheap — list lookups cost no LLM calls, and duplicate
    setids collapse on the filename.
    """
    if not drug_list_path.exists():
        logger.warning("Merged drug list not found: %s", drug_list_path)
        return []

    data = yaml.safe_load(drug_list_path.read_text()) or {}
    drugs = data.get("drugs", [])
    names: list[str] = []
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
        for name in candidates:
            key = name.strip().lower()
            if key and key not in seen:
                seen.add(key)
                names.append(name.strip())
    if limit > 0:
        names = names[:limit]
    logger.info("Selected %d USA-approved drug names to acquire", len(names))
    return names


def _is_usa_approved(value) -> bool:
    if value is True:
        return True
    return str(value).strip().upper() in ("TRUE", "APPROVED", "1")


#: How many SPLs to keep per drug name, newest first.
#:
#: This used to be 1 — the endpoint was already queried with ``pagesize=20`` and 19 of the 20
#: results were thrown away. That is the dominant cause of the recall gap against v1: a drug's
#: indications are split across manufacturers and formulations, so keeping only the
#: most-recently-published label keeps whichever subset that one happens to state. Lansoprazole
#: had 2 labels on disk and asserted exactly one indication, *Helicobacter pylori infection*;
#: GERD, duodenal ulcer and erosive esophagitis were never in any text we had fetched.
#:
#: Measured before changing it (12 drugs, 221 SPLs): 118 distinct indication sections, i.e.
#: ~9.8 extraction calls per drug rather than 1. Roughly half of a drug's SPLs repeat another's
#: indication text verbatim, and extraction is cached on the section-text hash, so the cost is
#: ~10x rather than the 20x the page size suggests.
DEFAULT_LABELS_PER_DRUG = 20


def _setids_for_drug(
    client: httpx.Client, drug_name: str, keep: int = DEFAULT_LABELS_PER_DRUG
) -> list[str]:
    """SPL setids for *drug_name*, most recently published first, at most *keep*."""
    try:
        resp = client.get(
            SPLS_LIST_URL, params={"drug_name": drug_name, "pagesize": 20}
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:  # noqa: BLE001 — network/parse errors are expected
        logger.debug("SPL list lookup failed for %r: %s", drug_name, e)
        return []

    data = payload.get("data") or []
    if not data:
        return []

    def _date(entry: dict) -> str:
        return str(entry.get("published_date") or entry.get("publishing_date") or "")

    out: list[str] = []
    for entry in sorted(data, key=_date, reverse=True):
        setid = (entry.get("setid") or "").strip()
        if setid and setid not in out:
            out.append(setid)
    return out[:keep] if keep > 0 else out


def _most_recent_setid(client: httpx.Client, drug_name: str) -> str | None:
    """The single most-recent setid. Kept for callers that want exactly one."""
    found = _setids_for_drug(client, drug_name, keep=1)
    return found[0] if found else None


def _download_spl_xml(
    client: httpx.Client, setid: str, out_path: Path
) -> bool:
    """Download a single SPL XML by setid. Returns True on success."""
    try:
        resp = client.get(SPL_XML_URL.format(setid=setid))
        resp.raise_for_status()
        content = resp.content
    except Exception as e:  # noqa: BLE001
        logger.debug("SPL XML download failed for %s: %s", setid, e)
        return False
    if not content or b"<document" not in content[:5000]:
        logger.debug("SPL XML for %s missing <document> root; skipping", setid)
        return False
    out_path.write_bytes(content)
    return True


def acquire(
    drug_list_path: Path = DEFAULT_DRUG_LIST,
    data_dir: Path = DEFAULT_DATA_DIR,
    limit: int = 0,
    delay: float = 0.2,
    timeout: float = 30.0,
    labels_per_drug: int = DEFAULT_LABELS_PER_DRUG,
) -> dict:
    """Acquire SPL XML for USA-approved drugs into *data_dir*.

    Returns a summary dict with counts. Resumable: setids whose XML already
    exists on disk are not re-downloaded.

    ``labels_per_drug`` keeps that many SPLs per drug name, newest first. 1 restores the old
    single-label behaviour; see :data:`DEFAULT_LABELS_PER_DRUG` for why it is no longer 1.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    names = _usa_drug_names(drug_list_path, limit=limit)
    cache = _get_cache()

    counts = {"requested": len(names), "resolved": 0, "downloaded": 0,
              "cached_hit": 0, "no_spl": 0, "errors": 0, "already_on_disk": 0}

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        for i, name in enumerate(names, 1):
            key = name.strip().lower()
            cached = cache.get(key)
            # The cache was written one-setid-per-drug. An entry in that old shape cannot
            # answer "what are this drug's other labels", so when more than one is wanted it
            # is re-resolved and rewritten as a list. Without this, every drug already in the
            # cache would silently keep its single label and the change would do nothing.
            setids: list[str] = []
            if cached is not None and isinstance(cached.get("setids"), list):
                setids = [s for s in cached["setids"] if s]
                counts["cached_hit"] += 1
            elif (cached is not None and cached.get("setid")
                  and labels_per_drug <= 1):
                setids = [cached["setid"]]
                counts["cached_hit"] += 1
            elif cached is not None and cached.get("match") == "no_results":
                counts["no_spl"] += 1
                continue
            else:
                setids = _setids_for_drug(client, name, keep=labels_per_drug)
                if not setids:
                    cache.put(key, {"setids": [], "match": "no_results"})
                    counts["no_spl"] += 1
                    time.sleep(delay)
                    continue
                cache.put(key, {"setids": setids})
                time.sleep(delay)

            counts["resolved"] += len(setids)
            for setid in setids:
                out_path = data_dir / f"{setid}.xml"
                if out_path.exists():
                    counts["already_on_disk"] += 1
                    continue
                ok = _download_spl_xml(client, setid, out_path)
                if ok:
                    counts["downloaded"] += 1
                else:
                    counts["errors"] += 1
                time.sleep(delay)

            if i % 100 == 0:
                logger.info(
                    "Acquire progress: %d/%d (downloaded=%d, cached=%d, no_spl=%d)",
                    i, len(names), counts["downloaded"],
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
        help=("SPLs to keep per drug, newest first (1 = the old single-label behaviour). "
              "Each distinct indication section costs one LLM extraction call downstream."),
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

"""Look up DailyMed SPL setids by drug name via the DailyMed v2 API.

Used to attach deep-linked `regulatory_document_url` values to FDA evidence
items in the legacy fallback path, where the source data carries only drug
name + indication text and no setid.

The DailyMed search API:
    https://dailymed.nlm.nih.gov/dailymed/services/v2/spls.json?drug_name=<NAME>

returns a paginated list of SPL entries. We pick the most recent SPL by
`published_date` and emit
    https://dailymed.nlm.nih.gov/dailymed/lookup.cfm?setid=<SETID>
as a stable, deep-linked document URL.

Results are cached to `cache/enrichment/dailymed_setids.json`.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

import httpx

from medic.enrichment.cache import EnrichmentCache

logger = logging.getLogger(__name__)

CACHE_PATH = Path("cache/enrichment/dailymed_setids.json")
API_URL = "https://dailymed.nlm.nih.gov/dailymed/services/v2/spls.json"
LOOKUP_URL = "https://dailymed.nlm.nih.gov/dailymed/lookup.cfm?setid={setid}"

_cache: EnrichmentCache | None = None


def _get_cache() -> EnrichmentCache:
    global _cache
    if _cache is None:
        _cache = EnrichmentCache(CACHE_PATH)
    return _cache


def _normalize_key(drug_name: str) -> str:
    """Lowercase + collapse whitespace + strip dosage/formulation noise."""
    s = (drug_name or "").lower().strip()
    s = re.sub(r"\s+", " ", s)
    return s


def lookup_setid(drug_name: str, timeout: float = 10.0) -> Optional[str]:
    """Return the most recent SPL setid for a drug name, or None.

    Cached by normalized drug name. Returns the setid string only.
    """
    key = _normalize_key(drug_name)
    if not key:
        return None
    cache = _get_cache()
    cached = cache.get(key)
    if cached is not None:
        return cached.get("setid") or None

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(API_URL, params={"drug_name": drug_name})
            resp.raise_for_status()
            payload = resp.json()
    except Exception as e:
        logger.debug("DailyMed lookup failed for %r: %s", drug_name, e)
        cache.put(key, {"setid": "", "error": str(e)[:200]})
        return None

    data = payload.get("data") or []
    if not data:
        cache.put(key, {"setid": "", "match": "no_results"})
        return None

    # Most recent SPL by parsed publication date. This used to sort the raw string, which
    # the endpoint formats as "Oct 14, 2026" — so it ordered alphabetically by month and
    # "Sep 3, 2026" ranked above "Oct 14, 2026". Found in #98's review of the acquirer,
    # where the same bug was latent because every hit was kept anyway; here it decided
    # which document URL an FDA evidence item got, so it was live.
    from medic.ingest.dailymed.acquire import published_date

    data_sorted = sorted(data, key=published_date, reverse=True)
    setid = (data_sorted[0].get("setid") or "").strip()
    cache.put(key, {
        "setid": setid,
        "title": data_sorted[0].get("title", "")[:200],
        "published_date": str(data_sorted[0].get("published_date")
                              or data_sorted[0].get("publishing_date") or ""),
    })
    # Flush incrementally so progress is durable if the run is killed mid-way
    try:
        cache.flush()
    except Exception:
        pass
    return setid or None


def setid_url(drug_name: str) -> Optional[str]:
    """Convenience wrapper: drug name -> deep-linked DailyMed URL or None."""
    setid = lookup_setid(drug_name)
    if not setid:
        return None
    return LOOKUP_URL.format(setid=setid)


def flush_cache() -> None:
    """Persist the cache to disk."""
    if _cache is not None:
        _cache.flush()


def lookup_failure_summary() -> dict:
    """Summarize the cache: how many drugs got a setid vs which failure mode.

    Returns counts grouped by failure reason ("ok", "no_results", "http_error").
    """
    cache = _get_cache()
    cache._load()
    counts = {"ok": 0, "no_results": 0, "http_error": 0}
    samples: dict[str, list[str]] = {"no_results": [], "http_error": []}
    for key, entry in (cache._data or {}).items():
        if not isinstance(entry, dict):
            continue
        if entry.get("setid"):
            counts["ok"] += 1
        elif "error" in entry:
            counts["http_error"] += 1
            if len(samples["http_error"]) < 5:
                samples["http_error"].append(key)
        elif entry.get("match") == "no_results":
            counts["no_results"] += 1
            if len(samples["no_results"]) < 5:
                samples["no_results"].append(key)
    return {"counts": counts, "samples": samples}


def log_failure_summary(level: int = logging.INFO) -> None:
    """Emit a summary log line of cached setid-lookup outcomes."""
    summary = lookup_failure_summary()
    counts = summary["counts"]
    total = sum(counts.values())
    logger.log(
        level,
        "DailyMed setid lookup outcomes: %d total | %d resolved | %d no SPL match | %d HTTP errors",
        total, counts["ok"], counts["no_results"], counts["http_error"],
    )
    if summary["samples"]["no_results"]:
        logger.log(level, "  no SPL match samples: %s", summary["samples"]["no_results"])
    if summary["samples"]["http_error"]:
        logger.log(level, "  HTTP error samples: %s", summary["samples"]["http_error"])

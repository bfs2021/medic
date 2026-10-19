"""RxNorm substance-level resolver (Phase 4 of formulation grounding).

Bridges the *unresolved* drug-string residue to CHEBI without any structure
mapping: for a product string the deterministic lexical grounder cannot ground,
call the free RxNav REST API (``approximateTerm`` -> best RxCUI -> its ingredient
``IN`` concepts -> each ingredient's clean INN name), then feed those clean INN
names back through MeDIC's own lexical grounder to reach CHEBI/DRON.

Determinism guard
-----------------
RxNav is a *network* call, so it must NOT live inside the offline matcher. This
module runs as a separate, **cached** enrichment (``cache/enrichment/rxnorm_resolve.json``,
resumable) and writes the recovered mappings as *proposed* rows into the grounding
SSSOM store (``mappings/drug_grounding.sssom.tsv``). Those proposals carry a distinct
``subject_preprocessing`` rule (``rxnorm_resolve``) so a curator can review them; offline
grounding runs then read them deterministically from the store.

False-positive guard
--------------------
``approximateTerm`` always returns *some* candidate, even for substances absent from
RxNorm (e.g. ``Sheep Pox Vaccine`` -> ``menthol``). We therefore keep an ingredient
only when its (stemmed) name is a token/substring of the source string. This drops the
spurious matches at the cost of a few conservative misses.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

from medic import product_view as pv
from medic.curie_utils import get_prefix
from medic.enrichment.cache import EnrichmentCache
from medic.enrichment.source_xrefs import add_source_xrefs
from medic.grounding.store import (
    NO_TERM,
    RXNORM_RULE,
    UNSPECIFIED,
    GroundingDecision,
    LiteralMappingStore,
    is_locked,
)
from medic.ingest.common import should_skip_expensive_calls

logger = logging.getLogger(__name__)

RXNAV_BASE = "https://rxnav.nlm.nih.gov/REST/"
CACHE_PATH = Path("cache/enrichment/rxnorm_resolve.json")
GROUNDING_STORE = "mappings/drug_grounding.sssom.tsv"

# These rows are distinguished by their `subject_preprocessing` rule, not by a bespoke
# `mapping_justification`. The justification slot is an SSSOM enum and only accepts `semapv:`
# terms — writing the bare string `RXNORM` there made every one of these rows fail SSSOM
# validation (460 errors). The rule was always present on the same rows, so nothing is lost.
RXNORM_JUSTIFICATION = UNSPECIFIED
RXNORM_PREPROCESS = RXNORM_RULE

_NONWORD = re.compile(r"[^a-z0-9]+")


def _norm(s: str) -> str:
    return _NONWORD.sub(" ", s.lower()).strip()


def ingredient_supported_by_source(source: str, ingredient: str) -> bool:
    """True when the RxNav ingredient name is plausibly present in the source string.

    Guards against ``approximateTerm`` returning an unrelated substance for a term
    that is not actually in RxNorm. Requires the ingredient (or a >=6-char stem of its
    first token) to appear as a substring of the normalized source string.
    """
    src = _norm(source)
    ing = _norm(ingredient)
    if not ing or not src:
        return False
    if ing in src:
        return True
    first = ing.split()[0]
    return len(first) >= 5 and first[:6] in src


def _http_get_json(url: str, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "medic-rxnorm-resolver/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed host)
        return json.loads(resp.read().decode("utf-8"))


def _approximate_best_rxcui(term: str) -> tuple[str, float] | None:
    url = RXNAV_BASE + "approximateTerm.json?" + urllib.parse.urlencode(
        {"term": term, "maxEntries": 1}
    )
    data = _http_get_json(url)
    cands = (data.get("approximateGroup") or {}).get("candidate") or []
    if not cands:
        return None
    best = cands[0]
    rxcui = best.get("rxcui")
    if not rxcui:
        return None
    try:
        score = float(best.get("score", 0.0))
    except (TypeError, ValueError):
        score = 0.0
    return rxcui, score


def _ingredient_concepts(rxcui: str) -> list[tuple[str, str]]:
    """``(name, rxcui)`` of every clean ingredient (IN) concept related to an RxCUI.

    In RxNav order. The ingredient's *own* RxCUI is what gets published as a cross-reference
    of the drug it re-grounds to: the product-string RxCUI from ``approximateTerm`` can be a
    clinical drug or a brand, which is a different entity from the ingredient node.
    """
    url = RXNAV_BASE + f"rxcui/{rxcui}/related.json?tty=IN"
    data = _http_get_json(url)
    groups = (data.get("relatedGroup") or {}).get("conceptGroup") or []
    concepts: list[tuple[str, str]] = []
    for g in groups:
        for c in g.get("conceptProperties", []) or []:
            name = c.get("name")
            if name:
                concepts.append((name, str(c.get("rxcui") or "")))
    return concepts


def _ingredient_names(rxcui: str) -> list[str]:
    """Clean ingredient (IN) names related to an RxCUI, in RxNav order."""
    return [name for name, _ in _ingredient_concepts(rxcui)]


def _is_complete(cached: dict | None) -> bool:
    """Whether a cache entry carries everything the current code records.

    Entries written before ingredient RxCUIs were kept have ``kept`` ingredients but no
    ``ingredient_rxcuis``; those are backfilled with one ``related.json`` call rather than
    invalidated wholesale, so the resumable cache stays resumable.
    """
    if cached is None:
        return False
    return not cached.get("kept") or "ingredient_rxcuis" in cached


def rxnav_resolve(term: str, cache: EnrichmentCache) -> dict:
    """Resolve a product string to guarded RxNav ingredient names (cached).

    Returns a dict ``{"rxcui", "score", "ingredients", "kept"}`` where ``kept`` is the
    subset of ingredient names that pass :func:`ingredient_supported_by_source`.
    Network errors and no-match are cached as empty results so reruns stay resumable.
    """
    cached = cache.get(term)
    if _is_complete(cached):
        return cached
    if cached is not None:
        # Backfill: the ingredient RxCUIs for an entry that predates them.
        try:
            concepts = _ingredient_concepts(cached["rxcui"]) if cached.get("rxcui") else []
            kept = set(cached.get("kept") or [])
            cached["ingredient_rxcuis"] = {n: r for n, r in concepts if n in kept and r}
            cache.put(term, cached)
        except Exception as exc:
            logger.warning("RxNav ingredient backfill failed for %r: %s", term, exc)
        return cached

    result: dict = {"rxcui": None, "score": None, "ingredients": [], "kept": []}
    try:
        best = _approximate_best_rxcui(term)
        if best is not None:
            rxcui, score = best
            concepts = _ingredient_concepts(rxcui)
            ings = [name for name, _ in concepts]
            kept = [i for i in ings if ingredient_supported_by_source(term, i)]
            result = {
                "rxcui": rxcui, "score": score, "ingredients": ings, "kept": kept,
                "ingredient_rxcuis": {n: r for n, r in concepts if n in kept and r},
            }
    except Exception as exc:  # network / parse failure -> cache empty, keep going
        logger.warning("RxNav resolve failed for %r: %s", term, exc)
        result["error"] = str(exc)

    cache.put(term, result)
    return result


def _proposal_rows(
    source_label: str,
    ingredient_grounds: list[tuple[str, str, str | None]],
    score: float | None,
) -> list[GroundingDecision]:
    """Build proposed SSSOM decision rows for one recovered source string."""
    rows: list[GroundingDecision] = []
    conf = None if score is None else round(min(score / 100.0, 0.9), 4)
    for ingredient, object_id, object_label in ingredient_grounds:
        rows.append(
            GroundingDecision(
                subject_label=source_label,
                entity_type="drugs",
                predicate_id="skos:closeMatch",
                object_id=object_id,
                object_label=object_label,
                object_match_field=None,
                mapping_justification=RXNORM_JUSTIFICATION,
                subject_preprocessing=[RXNORM_PREPROCESS],
                match_string=ingredient,
                confidence=conf,
            )
        )
    return rows


def resolve_residue(
    unresolved: list[str],
    ground_drug,
    store: LiteralMappingStore | None = None,
    cache: EnrichmentCache | None = None,
    rate_limit_s: float = 0.15,
    write_store: bool = True,
) -> dict:
    """Resolve unresolved drug strings via RxNav and propose CHEBI mappings.

    Args:
        unresolved: Verbatim source strings the deterministic grounder could not ground.
        ground_drug: Callable ``str -> [GroundingResult]`` (e.g.
            ``LexicalCascadeGrounding.ground_drug``) used to re-ground the clean INN.
        store: Grounding SSSOM store to write proposals into (loaded lazily if None).
        cache: RxNav response cache (defaults to ``cache/enrichment/rxnorm_resolve.json``).
        rate_limit_s: Politeness delay between *uncached* RxNav calls.
        write_store: When True, persist proposal rows to the SSSOM store.

    Returns:
        A report dict with counts and per-string recovery details.
    """
    cache = cache or EnrichmentCache(CACHE_PATH)
    if store is None and write_store:
        store = LiteralMappingStore(GROUNDING_STORE, "drugs")
        store.load()

    recovered: list[dict] = []
    rx_hit = 0
    proposed = 0

    for term in unresolved:
        was_cached = _is_complete(cache.get(term))
        res = rxnav_resolve(term, cache)
        if not was_cached and cache.get(term) is not None:
            time.sleep(rate_limit_s)

        kept = res.get("kept") or []
        if kept:
            rx_hit += 1

        grounds: list[tuple[str, str, str | None]] = []
        all_ground = bool(kept)
        for ingredient in kept:
            results = ground_drug(ingredient)
            if results and results[0].id:
                grounds.append((ingredient, results[0].id, results[0].label))
            else:
                all_ground = False

        # Only propose when every kept ingredient re-grounds — avoids partial,
        # misleading combination proposals.
        if grounds and all_ground:
            proposed += 1
            recovered.append(
                {
                    "source": term,
                    "rxcui": res.get("rxcui"),
                    "ingredients": [g[0] for g in grounds],
                    "object_ids": [g[1] for g in grounds],
                    "ingredient_rxcuis": [
                        (res.get("ingredient_rxcuis") or {}).get(g[0]) for g in grounds
                    ],
                }
            )
            if store is not None:
                # Do not clobber a subject a curator already owns.
                if not store.manual_rows(term):
                    store.record_subject(term, _proposal_rows(term, grounds, res.get("score")))

    if store is not None and write_store:
        cache.flush()
        store.save()
    elif cache is not None:
        cache.flush()

    chebi = sum(
        1 for r in recovered if all(get_prefix(i) == "CHEBI" for i in r["object_ids"])
    )
    return {
        "residue": len(unresolved),
        "rxnav_ingredient_hits": rx_hit,
        "proposed": proposed,
        "proposed_all_chebi": chebi,
        "recovered": recovered,
    }


def enrich_rxnorm_resolve(
    unresolved: list[str],
    ground_drug,
    **kwargs,
) -> dict:
    """Entry point mirroring the other enrichment modules; honours the skip flag."""
    if should_skip_expensive_calls():
        logger.info("MEDIC_SKIP_EXPENSIVE_CALLS set; skipping RxNorm resolve enrichment")
        return {"residue": len(unresolved), "rxnav_ingredient_hits": 0,
                "proposed": 0, "proposed_all_chebi": 0, "recovered": [], "skipped": True}
    return resolve_residue(unresolved, ground_drug, **kwargs)


def rxcui_xrefs_by_object(
    store: LiteralMappingStore, cache: EnrichmentCache
) -> dict[str, set[str]]:
    """``object_id -> {"RXCUI:<id>", ...}`` for every RxNorm-proposed grounding.

    Joins the two things this module writes: the proposal rows in the grounding store
    (subject string -> ingredient ``match_string`` -> ``object_id``) and the cache (subject
    string -> each kept ingredient's own RxCUI). The RxCUI is attached to the *ingredient's*
    object, never the product string's, so a combination's components each get their own.
    Entries without ``ingredient_rxcuis`` (pre-backfill cache) contribute nothing.
    """
    out: dict[str, set[str]] = {}
    for d in store.all_rows():
        if RXNORM_RULE not in d.subject_preprocessing or not d.object_id or not d.match_string:
            continue
        res = cache.get(d.subject_label) or {}
        rxcui = (res.get("ingredient_rxcuis") or {}).get(d.match_string)
        if rxcui:
            out.setdefault(d.object_id, set()).add(f"RXCUI:{rxcui}")
    return out


def enrich_rxcui_xrefs(
    drugs: list[dict],
    store: LiteralMappingStore | None = None,
    cache: EnrichmentCache | None = None,
) -> int:
    """Attach ``RXCUI:`` cross-references to the drugs RxNorm resolution grounded.

    Offline: reads the grounding store and the resolver cache that an earlier
    ``rxnorm_resolve`` pass wrote; makes no network call, so it runs under
    ``MEDIC_SKIP_EXPENSIVE_CALLS`` too. Returns the number of drugs touched.
    """
    if store is None:
        store = LiteralMappingStore(GROUNDING_STORE, "drugs")
        store.load()
    cache = cache or EnrichmentCache(CACHE_PATH)
    by_object = rxcui_xrefs_by_object(store, cache)
    if not by_object:
        return 0
    touched = 0
    for drug in drugs:
        xrefs = by_object.get(pv.drug_id(drug))
        if xrefs:
            add_source_xrefs(drug, xrefs)
            touched += 1
    logger.info("Attached RXCUI cross-references to %d drugs", touched)
    return touched


def collect_residue(store: LiteralMappingStore) -> list[str]:
    """Verbatim subjects the grounder left unresolved (``sssom:NoTermFound``).

    Reads the authoritative residue straight from the grounding SSSOM store, skipping
    subjects a curator or a prior RxNorm proposal already owns.
    """
    residue: list[str] = []
    seen: set[str] = set()
    for rows in store._rows.values():  # noqa: SLF001 (module-owned store)
        if any(is_locked(d) for d in rows):
            continue
        if all(d.predicate_id == NO_TERM or d.object_id == NO_TERM for d in rows):
            subj = rows[0].subject_label
            if subj and subj not in seen:
                seen.add(subj)
                residue.append(subj)
    return residue


def main(argv: list[str] | None = None) -> None:
    """Batch pass: resolve the grounding residue via RxNav and write proposals.

    Run after all source ingests (``just build-drug-list``) and before the next
    grounding/merge run so the proposals are read deterministically. Network-based and
    cached; honours ``MEDIC_SKIP_EXPENSIVE_CALLS``.
    """
    import argparse

    parser = argparse.ArgumentParser(description="RxNorm substance-level grounding resolver")
    parser.add_argument("--limit", type=int, default=None,
                        help="cap the number of residue strings processed (for a quick pass)")
    parser.add_argument("--dry-run", action="store_true",
                        help="measure recovery without writing proposals to the store")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if should_skip_expensive_calls():
        logger.info("MEDIC_SKIP_EXPENSIVE_CALLS set; skipping RxNorm resolve pass")
        return

    from medic.grounding.factory import get_grounding_service

    store = LiteralMappingStore(GROUNDING_STORE, "drugs")
    store.load()
    residue = collect_residue(store)
    if args.limit:
        residue = residue[: args.limit]
    logger.info("Collected %d unresolved drug strings from %s", len(residue), GROUNDING_STORE)

    svc = get_grounding_service("lexical")
    report = resolve_residue(
        residue, svc.ground_drug, store=store, write_store=not args.dry_run
    )
    logger.info(
        "RxNorm resolve: residue=%d rxnav_ingredient_hits=%d proposed=%d (all-CHEBI=%d)%s",
        report["residue"], report["rxnav_ingredient_hits"], report["proposed"],
        report["proposed_all_chebi"], " [dry-run]" if args.dry_run else "",
    )


__all__ = [
    "RXNORM_JUSTIFICATION",
    "collect_residue",
    "enrich_rxcui_xrefs",
    "enrich_rxnorm_resolve",
    "ingredient_supported_by_source",
    "resolve_residue",
    "rxcui_xrefs_by_object",
    "rxnav_resolve",
]


if __name__ == "__main__":
    main()

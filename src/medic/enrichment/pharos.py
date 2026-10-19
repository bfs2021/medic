"""Enrichment: PHAROS cross-references.

PHAROS is queried by *label* (``ligand(ligid: "<drug label>")``), so what comes back is
"the identifiers PHAROS holds for the ligand whose name matches this drug's label" — a
cross-reference copied from a source record, not an identity MeDIC established. That is why
the ids land in ``source_xrefs`` (KGX ``xref``) and not in ``alternate_ids``, which the SSSOM
export publishes as ``skos:exactMatch``.

PHAROS spells its synonym namespaces however it likes (``unii``, ``PubChem``, ``Guide to
Pharmacology`` — with spaces) and mixes in things that are not identifiers at all
(``pt:VORICONAZOLE`` is a *preferred term*, a label). Passed through raw, 1,092 drug nodes
carried lowercase ``unii:`` and name strings in ``xref``, which no consumer can join on
(#94). :func:`normalize_pharos_xref` rewrites each entry onto the Biolink prefix the KGX
graph declares for drugs, and drops everything that is not an identifier.
"""

import logging
from pathlib import Path

import httpx

from medic import product_view as pv
from medic.enrichment.cache import EnrichmentCache
from medic.enrichment.source_xrefs import add_source_xrefs
from medic.ingest.common import should_skip_expensive_calls

logger = logging.getLogger(__name__)

_cache = EnrichmentCache(Path("cache/enrichment/pharos.json"))

PHAROS_GRAPHQL_URL = "https://pharos-api.ncats.io/graphql"

#: PHAROS synonym ``name`` -> the Biolink prefix the KGX graph declares for drugs
#: (``biolink:Drug`` ``id_prefixes``), which is also what the SRI Node Normalizer keys on.
#: Closed on purpose: a name PHAROS starts sending that is not listed here is dropped with
#: a debug log rather than passed through raw — that is how ``pt:`` got into the graph.
PHAROS_PREFIXES = {
    "unii": "UNII",
    "cas": "CAS",
    "PubChem": "PUBCHEM.COMPOUND",
    "ChEMBL": "CHEMBL.COMPOUND",
    "DrugBank": "DRUGBANK",
    "DrugCentral": "DrugCentral",
    "Guide to Pharmacology": "GTOPDB",
}

#: Synonym names that are *not* identifiers and are dropped silently.
#:
#: ``pt`` is PHAROS's preferred term — a label. ``LyCHI`` is the ligand's structure hash,
#: which is the same string as its ``ligid``; that is already carried as ``PHAROS:<ligid>``,
#: a URL that resolves, so the bare hash adds nothing a consumer could join on.
PHAROS_NOT_IDENTIFIERS = frozenset({"pt", "LyCHI", "lychi"})

#: Our own prefix for the ligand page (``medic.curie_utils`` expands it).
PHAROS_PREFIX = "PHAROS"


def normalize_pharos_xref(raw: str) -> str | None:
    """A raw ``name:value`` PHAROS entry as a Biolink-prefixed CURIE, or ``None`` to drop it.

    Accepts both what the API returns and what older caches hold, so a cache written before
    this normalisation existed is read correctly without being regenerated.
    """
    if not raw or ":" not in raw:
        return None
    name, _, value = raw.partition(":")
    name, value = name.strip(), value.strip()
    if not value:
        return None
    if name in PHAROS_NOT_IDENTIFIERS:
        return None
    if name == PHAROS_PREFIX:
        prefix = PHAROS_PREFIX
    elif name in PHAROS_PREFIXES:
        prefix = PHAROS_PREFIXES[name]
    else:
        logger.debug("PHAROS synonym %r has no known prefix; dropped", raw)
        return None
    # A local identifier never contains whitespace — a value that does is a name a source
    # fell back to when it had no accession (``PHAROS:nitric oxide``).
    if any(ch.isspace() for ch in value):
        return None
    return f"{prefix}:{value}"


def normalize_pharos_xrefs(raw_ids) -> list[str]:
    """Every usable identifier in a raw PHAROS id list, deduplicated and sorted."""
    out = {normalize_pharos_xref(r) for r in raw_ids if isinstance(r, str)}
    out.discard(None)
    return sorted(out)


def enrich_pharos(drugs: list[dict]) -> None:
    """Enrich drugs with PHAROS cross-references.

    For each drug, queries the PHAROS GraphQL API and:
    - Extracts cross-reference IDs from synonyms, normalised to Biolink prefixes, into
      ``source_xrefs``
    - Adds the LyCHI ligand id as ``PHAROS:{ligid}``

    The cache keeps the raw ``name:value`` strings PHAROS returned; normalisation is applied
    on read, so no cache is invalidated by a change to the prefix table.

    When should_skip_expensive_calls(): skips all lookups.
    """
    if should_skip_expensive_calls():
        return

    for i, drug in enumerate(drugs):
        curie = pv.drug_id(drug)
        drug_label = pv.drug_label(drug)
        if not drug_label:
            continue

        # Check cache first
        cached = _cache.get(curie) if curie else None
        if cached is not None:
            add_source_xrefs(drug, normalize_pharos_xrefs(cached.get("pharos_ids", [])))
            continue

        try:
            query = """
            {
                ligand(ligid: "%s") {
                    ligid
                    name
                    synonyms {
                        name
                        value
                    }
                }
            }
            """ % drug_label.replace('"', '\\"')

            with httpx.Client(timeout=30.0) as client:
                resp = client.post(
                    PHAROS_GRAPHQL_URL,
                    json={"query": query},
                )
                resp.raise_for_status()
                data = resp.json()

            ligand = (data.get("data") or {}).get("ligand")
            if not ligand:
                if curie:
                    _cache.put(curie, {"pharos_ids": []})
                continue

            new_ids = []

            # Extract synonyms as cross-references (raw; normalised below and on cache read)
            synonyms = ligand.get("synonyms") or []
            for syn in synonyms:
                name = syn.get("name", "")
                value = syn.get("value", "")
                if name and value:
                    new_ids.append(f"{name}:{value}")

            # Add LyCHI hash if available
            ligid = ligand.get("ligid", "")
            if ligid:
                new_ids.append(f"{PHAROS_PREFIX}:{ligid}")

            add_source_xrefs(drug, normalize_pharos_xrefs(new_ids))

            # Store in cache
            if curie:
                _cache.put(curie, {"pharos_ids": sorted(new_ids)})

        except Exception:
            logger.debug("PHAROS lookup failed for %s", drug_label)
        if (i + 1) % 100 == 0:
            _cache.flush()

    _cache.flush()

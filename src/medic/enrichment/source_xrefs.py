"""The ``source_xrefs`` slot: cross-references copied from an enrichment source.

Distinct from ``alternate_ids`` on purpose. ``alternate_ids`` holds ids MeDIC's own resolution
established (the pre-normalization id, what the grounder returned) and the SSSOM export
publishes every one as ``skos:exactMatch`` — the strongest identity claim there is. A PHAROS
record found by *label*, or an RxNorm concept found by *approximate* match, is a
cross-reference a source holds, not an identity MeDIC verified, and the repo's rule (see
``tests/test_everycure_id_not_published_as_exactmatch.py``) is that such ids do not become
exactMatch rows. They do belong in KGX ``xref``, which is where a Node-Normalizer consumer
joins on them (#94).
"""

from __future__ import annotations

from collections.abc import Iterable

SLOT = "source_xrefs"


def add_source_xrefs(drug: dict, xrefs: Iterable[str]) -> None:
    """Union ``xrefs`` into the drug's ``source_xrefs`` slot (sorted, deduplicated).

    Leaves the record untouched when there is nothing to add, so a drug with no
    cross-references has no key rather than an empty list.
    """
    merged = set(drug.get(SLOT) or []) | {x for x in xrefs if isinstance(x, str) and x}
    if merged:
        drug[SLOT] = sorted(merged)

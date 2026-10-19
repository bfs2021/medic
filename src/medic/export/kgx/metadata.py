"""KGX content metadata and the proposed ``infores:medic`` registry entry.

MeDIC stamps ``aggregator_knowledge_source: infores:medic`` on every edge. An infores id
that is not in the Translator registry is a dangling reference, so the export writes a
proposed entry alongside the graph; submitting it upstream is a human step.
"""

from __future__ import annotations

from collections import Counter

from medic.export.kgx import biolink as bl
from medic.versions import medic_release

INFORES_ID = "infores:medic"


def _license_block() -> dict:
    """Licensing carried *inside* the graph metadata, not beside it.

    Issue #37's point: nobody reads a sibling ``LICENSING.md``. The KGX content metadata
    travels with the nodes and edges, so the obligations ride along with the data. The
    notice is read from ``conf/release_assets.yaml`` rather than restated here, so it
    cannot drift from what the release actually ships.
    """
    from medic import release_assets

    try:
        notice = release_assets.load().notice
    except (OSError, ValueError):  # manifest unreadable — never fail an export over it
        notice = ""

    return {
        # MeDIC grants no rights over derived data: the products are built from regulatory
        # sources MeDIC does not own, and the upstream terms pass through unchanged.
        "medic_grant": "none",
        "terms": "https://github.com/monarch-initiative/medic/blob/main/LICENSING.md",
        "attribution_notice": notice,
        "note": (
            "Redistributing this graph takes on the obligations of every contributing "
            "source. EMA and PMDA both require attribution; PMDA additionally requires "
            "that edited content be marked as edited."
        ),
    }


def _grounding_ontologies() -> dict:
    """The pinned ontology releases the drug and disease ids were grounded against.

    A consumer reconciling MONDO or CHEBI ids needs to know which release minted them —
    an id obsoleted or merged after that release is not a defect in the graph. Read from
    the pin table, not from the on-disk index: the index is a gitignored cache that may
    predate the pins, whereas the config is what the next build will use.
    """
    from medic.grounding.lexical.sources import pinned_ontologies

    try:
        pins = pinned_ontologies()
    except (OSError, ValueError, KeyError):  # config unreadable — never fail an export
        return {}
    return {name: {"version": pin.version, "url": pin.url} for name, pin in pins.items()}


def content_metadata(nodes: list[dict], edges: list[dict], build_date: str = "") -> dict:
    """Counts a consumer needs before deciding whether to ingest the graph."""
    by_category: Counter = Counter()
    for node in nodes:
        for category in node.get("category") or []:
            by_category[category] += 1

    return {
        "name": "MeDIC",
        "description": (
            "Drug-disease indications, contraindications, adverse events and research "
            "associations built from government regulatory sources."
        ),
        "medic_version": medic_release(),
        "biolink_version": bl.BIOLINK_VERSION,
        "grounding_ontologies": _grounding_ontologies(),
        "build_date": build_date,
        "aggregator_knowledge_source": bl.AGGREGATOR,
        "license": _license_block(),
        "nodes": {
            "total": len(nodes),
            "by_category": dict(sorted(by_category.items())),
            "stubs": sum(1 for n in nodes if n.get("medic_stub")),
        },
        "edges": {
            "total": len(edges),
            "by_predicate": dict(sorted(Counter(
                e.get("predicate", "") for e in edges).items())),
            "by_primary_knowledge_source": dict(sorted(Counter(
                e.get("primary_knowledge_source", "") for e in edges).items())),
            "by_knowledge_level": dict(sorted(Counter(
                e.get("knowledge_level", "") for e in edges).items())),
            "by_reliability": dict(sorted(Counter(
                e.get("medic_reliability", "") for e in edges).items())),
        },
    }


def infores_entry() -> dict:
    """A proposed Translator information-resource entry for MeDIC."""
    return {
        "information_resources": [{
            "id": INFORES_ID,
            "name": "MeDIC",
            "description": (
                "Medicines, Diseases, Indications and Contraindications — an open "
                "knowledge base of drug-disease associations built from government "
                "regulatory sources worldwide."
            ),
            "url": "https://medic.renci.org",
            "xref": [
                "https://github.com/monarch-initiative/medic",
                "PMID:41385096",
            ],
            "knowledge_level": "knowledge_assertion",
            "agent_type": "automated_agent",
            "status": "proposed",
        }, {
            "id": bl.MEDIC_CURATION,
            "name": "MeDIC research curation",
            "description": (
                "Drug-disease associations asserted by MeDIC's AI-assisted literature "
                "curation pipeline where no external citable source carries the claim."
            ),
            "url": "https://github.com/monarch-initiative/medic",
            "knowledge_level": "observation",
            "agent_type": "text_mining_agent",
            "consumed_by": [INFORES_ID],
            "status": "proposed",
        }]
    }

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


def extraction_agents(*products: list[dict]) -> list[dict]:
    """Who produced the assertions: distinct (agent, tool, method) tuples with counts.

    These are build-level facts — one LLM, one extractor version per run — and used to ride
    on every edge as five ``medic_*`` properties that took one value across 70,000 records.
    They belong here, once. Biolink's ``agent_type`` on each edge still says *what kind* of
    agent produced it; this says which one.
    """
    from medic import product_view as pv

    counts: Counter = Counter()
    for product in products:
        for assoc in product or []:
            for assertion in pv.assoc_assertions(assoc):
                claim = assertion.get("assertion") or {}
                agent = claim.get("agent") or {}
                counts[(
                    agent.get("agent_name") or "",
                    agent.get("agent_version") or "",
                    claim.get("tool") or "",
                    claim.get("tool_version") or "",
                    claim.get("method") or "",
                )] += 1
    return [
        {"agent_name": name, "agent_version": version, "tool": tool,
         "tool_version": tool_version, "method": method, "assertions": n}
        for (name, version, tool, tool_version, method), n in sorted(counts.items())
    ]


def source_explanations(*products: list[dict]) -> list[dict]:
    """The explanation each regulatory source attaches to its assertions, with counts.

    An ingester writes one sentence per source ("FDA-approved indication from DailyMed
    structured product label"), so on the edges it was a ``description`` taking a handful
    of values across 70,000 records — the same shape as :func:`extraction_agents`, and it
    lives here for the same reason. Research edges keep their per-claim explanation.
    """
    from medic import product_view as pv
    from medic.export.kgx import biolink as bl

    counts: Counter = Counter()
    for product in products:
        for assoc in product or []:
            for assertion in pv.assoc_assertions(assoc):
                regulatory = assertion.get("regulatory_status") or {}
                evidence = assertion.get("evidence") or {}
                source = regulatory.get("source") or assertion.get("source") or ""
                primary = bl.primary_knowledge_source(source, regulatory.get("authority") or "")
                counts[(primary, evidence.get("explanation") or "")] += 1
    return [
        {"primary_knowledge_source": primary, "description": text, "assertions": n}
        for (primary, text), n in sorted(counts.items())
    ]


def content_metadata(
    nodes: list[dict],
    edges: list[dict],
    build_date: str = "",
    agents: list[dict] | None = None,
    explanations: list[dict] | None = None,
) -> dict:
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
        "extraction_agents": agents or [],
        "source_explanations": explanations or [],
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
            "by_category": dict(sorted(Counter(
                c for e in edges for c in (e.get("category") or [])).items())),
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

"""KGX export: a complete Biolink knowledge graph of everything MeDIC knows.

Reads every product — drugs, diseases, indications, contraindications, research
associations and adverse events — and writes Biolink-conformant nodes and edges as JSONL,
plus content metadata and a proposed infores entry.

Two things shape the output; both are argued in
``specs/2026-08-13-kgx-export-design.md``:

* **One edge per source assertion.** Each edge is single-sourced, so it carries exactly one
  ``primary_knowledge_source``, one document, one quoted span and one confidence. Pair-level
  aggregates ride along on every edge, so the collapsed drug-disease view is a ``GROUP BY``.
* **Two layers.** A strictly Biolink-valid core that a Translator ingest can consume
  unchanged, plus a ``medic_``-namespaced extension layer carrying what Biolink has no slot
  for and a consumer needs on the record. A strict consumer drops the extension layer with
  one rule and still has a valid graph.
* **Built through the model.** Every node and edge is an instance of a Biolink pydantic
  class (``biolink:Drug``, ``biolink:ChemicalOrDrugOrTreatmentToDiseaseOrPhenotypicFeature
  Association``, ...) dumped to JSON, so the class fixes ``category`` and refuses a property
  or enum value Biolink 4.4.6 does not define.
"""

from __future__ import annotations

import logging
from pathlib import Path

from medic.export.kgx import edges as edge_builders
from medic.export.kgx import metadata as metadata_builders
from medic.export.kgx import nodes as node_builders
from medic.export.kgx import writer
from medic.reliability import StatementReviewStore

logger = logging.getLogger(__name__)

PRODUCTS_DIR = Path("products")
EXPORTS_DIR = Path("exports")

NODES_FILE = "medic_nodes.jsonl"
EDGES_FILE = "medic_edges.jsonl"
METADATA_FILE = "medic_kgx_metadata.yaml"
INFORES_FILE = "infores_medic.yaml"

#: Product file -> the key holding its record list. ``disease_list.yaml`` is read from
#: ``products/`` (written by ``merge/disease_merge.py``) with the ``kb/`` copy as a fallback.
_PRODUCTS = {
    "drugs": ("drug_list.yaml", "drugs"),
    "diseases": ("disease_list.yaml", "diseases"),
    "indications": ("indication_list.yaml", "associations"),
    "contraindications": ("contraindication_list.yaml", "associations"),
    "research": ("research_list.yaml", "associations"),
    "adverse_events": ("adverse_event_list.yaml", "associations"),
}
_DISEASE_FALLBACK = Path("kb/diseases/disease_list.yaml")


def _load_all(products_dir: Path) -> dict[str, list[dict]]:
    loaded = {
        name: writer.load_product(products_dir / filename, key)
        for name, (filename, key) in _PRODUCTS.items()
    }
    if not loaded["diseases"] and _DISEASE_FALLBACK.exists():
        loaded["diseases"] = writer.load_product(_DISEASE_FALLBACK, "diseases")
    return loaded


def export_kgx(
    products_dir: Path = PRODUCTS_DIR,
    exports_dir: Path = EXPORTS_DIR,
    build_date: str = "",
) -> tuple[list[dict], list[dict]]:
    """Build and write the KGX graph. Returns the nodes and edges written."""
    products = _load_all(Path(products_dir))
    exports_dir = Path(exports_dir)

    # One store for the whole export, loaded once. Without it the builders recompute the
    # tier from the automated gates alone and a curator's REJECTED is discarded at the
    # point of publication (#62).
    review = StatementReviewStore().load()

    built_edges, referenced = edge_builders.build_edges(
        products["indications"],
        products["contraindications"],
        products["research"],
        products["adverse_events"],
        review=review,
    )
    built_nodes = node_builders.build_nodes(
        products["drugs"], products["diseases"], referenced, review=review,
        unii_by_drug=node_builders.unii_by_drug(
            products["indications"], products["contraindications"]),
    )

    writer.write_jsonl(built_nodes, exports_dir / NODES_FILE)
    writer.write_jsonl(built_edges, exports_dir / EDGES_FILE)
    writer.write_yaml(
        metadata_builders.content_metadata(
            built_nodes, built_edges, build_date,
            agents=metadata_builders.extraction_agents(
                products["indications"], products["contraindications"]),
            explanations=metadata_builders.source_explanations(
                products["indications"], products["contraindications"]),
        ),
        exports_dir / METADATA_FILE,
    )
    writer.write_yaml(metadata_builders.infores_entry(), exports_dir / INFORES_FILE)

    stubs = sum(1 for n in built_nodes if n.get("medic_stub"))
    logger.info(
        "Exported %d nodes (%d stubs) and %d edges to KGX",
        len(built_nodes), stubs, len(built_edges),
    )
    return built_nodes, built_edges


__all__ = ["export_kgx", "EXPORTS_DIR", "NODES_FILE", "EDGES_FILE", "METADATA_FILE"]

"""Drug nodes carry the UNII the label asserts (#94).

717 drug ids (3,448 indication assertions, 28%) are unknown to the SRI Node Normalizer:
ChEBI and DRON are fetched unpinned from PURLs, so they mint ids newer than its snapshot,
and anything joining on the primary id drops those edges. They are the biologics —
adalimumab 143 assertions, denosumab 109, rituximab 89.

The UNII is sitting in the SPL next to the moiety name we already parse
(`<activeMoiety><code codeSystem="2.16.840.1.113883.4.9">`), so recording it at ingest and
publishing it as an xref lets a consumer join whatever the primary id turns out to be. That
also makes the problem self-healing across ontology refreshes rather than something to
re-fix each time.

What this does *not* do: a drug with no indication rows gets no UNII, because the identifier
travels on the evidence. `basiliximab` is the live example — it is a node with zero edges, so
nothing carries its UNII. The remedy there is whatever restores its indications, not this.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from medic.export.kgx.nodes import drug_node, unii_by_drug
from medic.ingest.dailymed.__main__ import unii_by_moiety

_SPL = """<?xml version="1.0" encoding="UTF-8"?>
<document xmlns="urn:hl7-org:v3">
  <setId root="1"/>
  <manufacturedProduct>
    <asEntityWithGeneric><genericMedicine><name>{generic}</name></genericMedicine></asEntityWithGeneric>
  </manufacturedProduct>
  {moieties}
</document>"""


def _root(generic: str, *pairs: tuple[str, str]) -> ET.Element:
    blocks = "".join(
        f'<activeMoiety><activeMoiety><code code="{code}" '
        f'codeSystem="2.16.840.1.113883.4.9"/><name>{name}</name></activeMoiety>'
        f"</activeMoiety>"
        for name, code in pairs
    )
    return ET.fromstring(_SPL.format(generic=generic, moieties=blocks))


# ---------------------------------------------------------------------------
# Ingest side
# ---------------------------------------------------------------------------
def test_the_unii_is_read_off_the_moiety():
    got = unii_by_moiety(_root("ADALIMUMAB", ("ADALIMUMAB", "FYS6T7F842")))
    assert got["ADALIMUMAB"] == "FYS6T7F842"


def test_a_code_from_another_system_is_ignored():
    """Only the FDA UNII OID counts. An SPL carries other coded identifiers in the same
    shape, and treating one of those as a UNII would publish a wrong xref — worse than
    publishing none, because a consumer cannot tell."""
    root = ET.fromstring(_SPL.format(
        generic="X",
        moieties='<activeMoiety><activeMoiety>'
                 '<code code="12345" codeSystem="2.16.840.1.113883.6.69"/>'
                 '<name>SOMETHING</name></activeMoiety></activeMoiety>',
    ))
    assert unii_by_moiety(root) == {}


def test_a_metabolite_finds_a_unii_under_its_dispensed_name():
    """#95 resolves RAMIPRILAT to RAMIPRIL, so the drug id is grounded from the prodrug name
    while the label codes the metabolite. Both keys must reach the asserted UNII or the
    rescue in #95 silently costs the xref in #94."""
    got = unii_by_moiety(_root("RAMIPRIL", ("RAMIPRILAT", "L35JN3I7SJ")))
    assert got["RAMIPRILAT"] == "L35JN3I7SJ"
    assert got["RAMIPRIL"] == "L35JN3I7SJ"


# ---------------------------------------------------------------------------
# Export side
# ---------------------------------------------------------------------------
def test_the_collector_reads_the_evidence_rows():
    assocs = [{
        "drug_id": "CHEBI:749494",
        # `assertions[].evidence` is a dict, one row per source document — not a list.
        "assertions": [{"evidence": {"original_drug_unii": "FYS6T7F842"}}],
    }]
    assert unii_by_drug(assocs) == {"CHEBI:749494": "FYS6T7F842"}


def test_a_drug_with_no_unii_on_its_rows_is_absent():
    assocs = [{"drug_id": "CHEBI:1", "assertions": [{"evidence": {}}]}]
    assert unii_by_drug(assocs) == {}


def test_the_node_publishes_the_unii_as_an_xref():
    node = drug_node(
        {"identity": {"id": "m1", "resolved_id": "CHEBI:749494", "original_literal": "x"},
         "label": "adalimumab"},
        unii_by_drug={"CHEBI:749494": "FYS6T7F842"},
    )
    assert "UNII:FYS6T7F842" in node["xref"]


def test_a_node_without_a_unii_has_no_unii_xref():
    node = drug_node(
        {"identity": {"id": "m1", "resolved_id": "CHEBI:1", "original_literal": "x"},
         "label": "whatever"},
        unii_by_drug={},
    )
    assert not [x for x in (node.get("xref") or []) if x.startswith("UNII:")]

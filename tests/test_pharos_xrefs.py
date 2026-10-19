"""PHAROS cross-references reach KGX `xref` as Biolink-prefixed CURIEs, or not at all.

PHAROS returns its synonyms as ``name``/``value`` pairs spelled its own way (``unii``,
``PubChem``, ``Guide to Pharmacology``) and mixed with labels (``pt:VORICONAZOLE``). Written
through raw, 1,092 drug nodes carried identifiers no Node-Normalizer consumer could join on
(#94). The enrichment now rewrites each onto the prefix ``biolink:Drug`` declares, drops what
is not an identifier, and writes the result to ``source_xrefs`` — not ``alternate_ids``,
which the SSSOM export publishes as ``skos:exactMatch``.
"""

from __future__ import annotations

import logging

import pytest

from medic.enrichment import pharos
from medic.enrichment.cache import EnrichmentCache
from medic.enrichment.pharos import normalize_pharos_xref, normalize_pharos_xrefs
from medic.export.kgx import biolink as bl


@pytest.mark.parametrize("raw, expected", [
    ("unii:JFU09I87TR", "UNII:JFU09I87TR"),
    ("cas:137234-62-9", "CAS:137234-62-9"),
    ("PubChem:71616", "PUBCHEM.COMPOUND:71616"),
    ("ChEMBL:CHEMBL638", "CHEMBL.COMPOUND:CHEMBL638"),
    ("DrugBank:DB00582", "DRUGBANK:DB00582"),
    ("DrugCentral:2846", "DrugCentral:2846"),
    ("Guide to Pharmacology:6853", "GTOPDB:6853"),
    ("PHAROS:RMWVQW96R8LU", "PHAROS:RMWVQW96R8LU"),
    (" unii : JFU09I87TR ", "UNII:JFU09I87TR"),
])
def test_known_namespaces_are_rewritten_to_biolink_prefixes(raw, expected):
    assert normalize_pharos_xref(raw) == expected


@pytest.mark.parametrize("raw", [
    "pt:VORICONAZOLE",            # a preferred term — a label
    "LyCHI:RMWVQW96R8LU",         # the ligid again; already carried as PHAROS:
    "PHAROS:nitric oxide",        # a name where an accession should be
    "unii:",                      # no local id
    "no colon at all",
    "",
])
def test_non_identifiers_are_dropped(raw):
    assert normalize_pharos_xref(raw) is None


def test_an_unknown_namespace_is_dropped_with_a_debug_log(caplog):
    """Closed table: a new PHAROS synonym type must be added here, not passed through."""
    with caplog.at_level(logging.DEBUG, logger="medic.enrichment.pharos"):
        assert normalize_pharos_xref("SomeNewSource:12345") is None
    assert "SomeNewSource:12345" in caplog.text


def test_every_target_prefix_is_one_the_graph_declares_for_drugs():
    """The point of the rewrite is joinability: the prefixes must be the ones
    `biolink:Drug` lists, which is what the SRI Node Normalizer keys on."""
    declared = bl.id_prefixes("biolink:Drug")
    assert declared, "biolink model unavailable"
    for name, prefix in pharos.PHAROS_PREFIXES.items():
        assert prefix in declared, (name, prefix)


def test_every_target_prefix_expands_to_an_iri():
    """Biolink spells two of them (`GTOPDB`, `DrugCentral`) in a way the bioregistry does
    not recognise; the SSSOM exporter's alias table bridges that, so a future decision to
    publish these as closeMatch rows would find every prefix resolvable."""
    from medic.curie_utils import get_converter
    from medic.export.sssom import _PREFIX_ALIASES

    converter = get_converter()
    for prefix in [*pharos.PHAROS_PREFIXES.values(), pharos.PHAROS_PREFIX]:
        assert converter.expand(f"{_PREFIX_ALIASES.get(prefix, prefix)}:X"), prefix


def test_list_normalisation_deduplicates_and_sorts():
    raw = ["unii:JFU09I87TR", "UNII:JFU09I87TR", "pt:VORICONAZOLE", "ChEMBL:CHEMBL638", 42]
    assert normalize_pharos_xrefs(raw) == ["CHEMBL.COMPOUND:CHEMBL638", "UNII:JFU09I87TR"]


def test_enrichment_reads_a_raw_cache_into_source_xrefs_only(tmp_path, monkeypatch):
    """The cache keeps PHAROS's raw spellings (no regeneration needed); the drug gets the
    normalised set in `source_xrefs`, and `alternate_ids` is left exactly as it was."""
    cache = EnrichmentCache(tmp_path / "pharos.json")
    cache.put("CHEBI:10023", {"pharos_ids": [
        "ChEMBL:CHEMBL638", "Guide to Pharmacology:6853", "LyCHI:RMWVQW96R8LU",
        "PHAROS:RMWVQW96R8LU", "pt:VORICONAZOLE", "unii:JFU09I87TR",
    ]})
    monkeypatch.setattr(pharos, "_cache", cache)
    monkeypatch.setattr(pharos, "should_skip_expensive_calls", lambda: False)
    drug = {
        "identity": {"resolved_id": "CHEBI:10023", "resolved_label": "voriconazole"},
        "alternate_ids": ["DRUGBANK:DB00582"],
    }

    pharos.enrich_pharos([drug])

    assert drug["source_xrefs"] == [
        "CHEMBL.COMPOUND:CHEMBL638", "GTOPDB:6853", "PHAROS:RMWVQW96R8LU", "UNII:JFU09I87TR",
    ]
    assert drug["alternate_ids"] == ["DRUGBANK:DB00582"]


def test_a_fresh_lookup_caches_raw_and_writes_normalised(tmp_path, monkeypatch):
    cache = EnrichmentCache(tmp_path / "pharos.json")
    monkeypatch.setattr(pharos, "_cache", cache)
    monkeypatch.setattr(pharos, "should_skip_expensive_calls", lambda: False)

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": {"ligand": {
                "ligid": "RMWVQW96R8LU", "name": "voriconazole",
                "synonyms": [{"name": "unii", "value": "JFU09I87TR"},
                             {"name": "pt", "value": "VORICONAZOLE"}],
            }}}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(pharos.httpx, "Client", _Client)
    drug = {"identity": {"resolved_id": "CHEBI:10023", "resolved_label": "voriconazole"}}

    pharos.enrich_pharos([drug])

    assert drug["source_xrefs"] == ["PHAROS:RMWVQW96R8LU", "UNII:JFU09I87TR"]
    assert "alternate_ids" not in drug
    assert cache.get("CHEBI:10023") == {
        "pharos_ids": ["PHAROS:RMWVQW96R8LU", "pt:VORICONAZOLE", "unii:JFU09I87TR"]}

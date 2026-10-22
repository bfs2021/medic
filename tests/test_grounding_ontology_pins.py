"""Every ontology the lexical index is built from is pinned to a release.

`just download-grounding-ontologies` fetched the floating PURL
(http://purl.obolibrary.org/obo/<ont>.json), which resolves to whatever is current on the
day it runs, and nothing recorded the version — so two builds could ground against
different term sets with no way to tell (#94). The pin table in
``conf/grounding_sources.yaml`` is now the single source for the download URL, the version
stamped into the index and the version the KGX metadata reports.
"""

from __future__ import annotations

import json
import re
import sqlite3
import textwrap

import pytest

from medic.grounding.lexical import build, sources

CONF = "conf/grounding_sources.yaml"


def test_the_shipped_config_pins_every_obo_source():
    data = sources.load_sources(CONF)
    pins = sources.pinned_ontologies(CONF)
    obo_entries = [e for key in ("diseases", "drugs") for e in data[key]
                   if e.get("loader") == "obo_json"]
    assert obo_entries, "no OBO sources configured?"
    for entry in obo_entries:
        name = sources.ontology_for_source(entry)
        assert name, f"source {entry['prefix']} does not name its ontology"
        assert name in pins, f"{name} has no pin"


def test_every_pin_has_a_version_and_a_versioned_url():
    for name, pin in sources.pinned_ontologies(CONF).items():
        assert pin.version
        assert pin.url.startswith("http")
        # The URL must name the release, or the pin is decorative: a floating PURL with a
        # version written next to it is the unpinned state with a label on it.
        assert pin.version in pin.url, (name, pin.url)
        assert not re.fullmatch(r"http://purl\.obolibrary\.org/obo/\w+\.json", pin.url)


def test_a_pin_without_a_version_is_refused(tmp_path):
    conf = tmp_path / "sources.yaml"
    conf.write_text(textwrap.dedent("""
        ontologies:
          mondo: {url: http://purl.obolibrary.org/obo/mondo.json}
        diseases: []
        drugs: []
    """))
    with pytest.raises(ValueError, match="not pinned"):
        sources.pinned_ontologies(conf)


def _tiny_obo_json(path, prefix):
    path.write_text(json.dumps({"graphs": [{"nodes": [
        {"id": f"http://purl.obolibrary.org/obo/{prefix}_0000001", "lbl": "thing one"},
    ]}]}))


def test_the_index_build_stamps_the_pinned_versions(tmp_path):
    _tiny_obo_json(tmp_path / "chebi.json", "CHEBI")
    _tiny_obo_json(tmp_path / "dron.json", "DRON")
    conf = tmp_path / "sources.yaml"
    conf.write_text(textwrap.dedent(f"""
        ontologies:
          chebi: {{version: "255", url: http://purl.obolibrary.org/obo/chebi/255/chebi.json}}
          dron: {{version: "2026-09-19",
                 url: http://purl.obolibrary.org/obo/dron/releases/2026-09-19/dron.json}}
        drugs:
          - {{prefix: CHEBI, loader: obo_json, path: {tmp_path / 'chebi.json'}, ontology: chebi}}
          - {{prefix: DRON, loader: obo_json, path: {tmp_path / 'dron.json'}, ontology: dron}}
    """))
    db = tmp_path / "drugs.db"

    assert build.build_index("drugs", str(conf), str(db)) == 2
    assert build.index_meta(str(db)) == [
        {"prefix": "CHEBI", "ontology": "chebi", "version": "255",
         "url": "http://purl.obolibrary.org/obo/chebi/255/chebi.json"},
        {"prefix": "DRON", "ontology": "dron", "version": "2026-09-19",
         "url": "http://purl.obolibrary.org/obo/dron/releases/2026-09-19/dron.json"},
    ]


def test_a_source_naming_an_unpinned_ontology_fails_the_build(tmp_path):
    _tiny_obo_json(tmp_path / "chebi.json", "CHEBI")
    conf = tmp_path / "sources.yaml"
    conf.write_text(textwrap.dedent(f"""
        ontologies: {{}}
        drugs:
          - {{prefix: CHEBI, loader: obo_json, path: {tmp_path / 'chebi.json'}, ontology: chebi}}
    """))
    with pytest.raises(ValueError, match="no pin"):
        build.build_index("drugs", str(conf), str(tmp_path / "drugs.db"))


def test_an_index_built_before_the_meta_table_reports_no_versions(tmp_path):
    db = tmp_path / "old.db"
    sqlite3.connect(db).execute("CREATE TABLE lex (object_id TEXT)").connection.close()
    assert build.index_meta(str(db)) == []


def _versioned_obo_json(path, prefix, version_iri):
    path.write_text(json.dumps({"graphs": [{
        "meta": {"version": version_iri},
        "nodes": [{"id": f"http://purl.obolibrary.org/obo/{prefix}_0000001",
                   "lbl": "thing one"}],
    }]}))


@pytest.mark.parametrize("iri,expected", [
    ("http://purl.obolibrary.org/obo/mondo/releases/2026-07-06/mondo.owl", "2026-07-06"),
    ("http://purl.obolibrary.org/obo/hp/releases/2026-06-23/hp.json", "2026-06-23"),
    # ChEBI's archive layout has no `releases/` segment and a bare integer release.
    ("http://purl.obolibrary.org/obo/chebi/253/chebi.owl", "253"),
])
def test_the_release_token_is_read_back_out_of_the_file(tmp_path, iri, expected):
    p = tmp_path / "o.json"
    _versioned_obo_json(p, "MONDO", iri)
    assert sources.release_in_file(p) == expected


def test_a_file_declaring_no_version_is_unverifiable_rather_than_wrong(tmp_path):
    """Neither a match nor a mismatch: a robot-converted OWL carries no version IRI, and
    refusing it would stop the build over something the pin cannot settle either way."""
    _tiny_obo_json(tmp_path / "chebi.json", "CHEBI")
    assert sources.release_in_file(tmp_path / "chebi.json") is None
    sources.check_pin(sources.PinnedOntology("chebi", "253", "http://x/253/chebi.json"),
                      tmp_path / "chebi.json")


def test_a_pin_that_disagrees_with_the_download_fails_the_build(tmp_path):
    """The pin has to be *checked*, not just declared. Bump the version, forget to
    re-download, and without this the index stamps a release it was not built from — a
    false provenance claim, which is worse than recording nothing (#94)."""
    _versioned_obo_json(tmp_path / "chebi.json", "CHEBI",
                        "http://purl.obolibrary.org/obo/chebi/253/chebi.owl")
    conf = tmp_path / "sources.yaml"
    conf.write_text(textwrap.dedent(f"""
        ontologies:
          chebi: {{version: "255", url: http://purl.obolibrary.org/obo/chebi/255/chebi.json}}
        drugs:
          - {{prefix: CHEBI, loader: obo_json, path: {tmp_path / 'chebi.json'}, ontology: chebi}}
    """))
    with pytest.raises(ValueError, match=r"release '253'.*pins '255'"):
        build.build_index("drugs", str(conf), str(tmp_path / "drugs.db"))


def test_the_shipped_pins_match_the_ontologies_on_disk():
    """The config must name the releases this checkout actually grounds against. Skipped
    on a fresh clone, where cache/ontologies/ has not been downloaded yet."""
    data = sources.load_sources(CONF)
    pins = sources.pinned_ontologies(CONF)
    checked = 0
    for entry in (e for key in ("diseases", "drugs") for e in data[key]):
        name = sources.ontology_for_source(entry)
        if name is None or sources.release_in_file(entry["path"]) is None:
            continue
        sources.check_pin(pins[name], entry["path"])
        checked += 1
    if not checked:
        pytest.skip("cache/ontologies/ not populated")


def test_kgx_content_metadata_reports_the_pins():
    from medic.export.kgx.metadata import content_metadata

    reported = content_metadata([], [])["grounding_ontologies"]
    pins = sources.pinned_ontologies(CONF)
    assert reported == {n: {"version": p.version, "url": p.url} for n, p in pins.items()}
    assert {"mondo", "hp", "chebi", "dron"} <= set(reported)

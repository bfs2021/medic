"""Tests for the SSSOM drug-mapping export, focused on its licensing header.

The header used to declare `license: CC0` over the whole file. That overstates MeDIC's
position: CC0 is right for the mapping *assertions* (MeDIC's own contribution — that string
X grounds to CURIE Y), but `subject_label` reproduces verbatim strings from regulatory
sources, which stay under source terms. LICENSING.md flags this as an open item.
"""

from __future__ import annotations

import pytest
import yaml

from medic.export import sssom
from medic.export.sssom import EXPORTABLE_PREFIXES, normalize_object_id


def _header_fields() -> dict[str, str]:
    fields = {}
    for line in sssom.SSSOM_HEADER.splitlines():
        if line.startswith("#") and ":" in line and not line.startswith("#  "):
            key, _, value = line[1:].partition(":")
            fields[key.strip()] = value.strip()
    return fields


def test_header_records_the_verbatim_source_string_carve_out():
    """A consumer reading only the header must learn that CC0 does not cover every column."""
    header = sssom.SSSOM_HEADER
    assert "subject_label" in header
    assert "comment" in _header_fields()


def test_header_carries_the_attribution_notice_from_the_release_manifest():
    from medic import release_assets as ra

    notice = ra.load().notice
    assert notice
    # The notice is long; check the load-bearing names survived into the header.
    for fragment in ("European Medicines Agency", "Data has been edited"):
        assert fragment in sssom.SSSOM_HEADER, fragment


def test_header_lines_are_all_comments_so_the_tsv_still_parses():
    for line in sssom.SSSOM_HEADER.splitlines():
        assert line == "" or line.startswith("#"), line


# ---------------------------------------------------------------------------
# The declared licence
# ---------------------------------------------------------------------------
def test_license_is_cc_by_not_cc0():
    """CC0 told consumers attribution was optional. It is not: the file mixes MeDIC's
    assertions with verbatim EMA and PMDA strings, and both require attribution. The
    `license` field is the one machine-readable signal, so it has to be the true one."""
    assert _header_fields()["license"] == "https://creativecommons.org/licenses/by/4.0/"


def test_header_still_offers_the_mapping_assertions_as_cc0():
    """The file-level CC BY comes from third-party content, not from MeDIC wanting credit."""
    assert "publicdomain/zero" in sssom.SSSOM_HEADER


def test_header_states_the_licence_passthrough():
    assert "remains in force" in sssom.SSSOM_HEADER


# ---------------------------------------------------------------------------
# The rows: is this actually a usable mapping set? (review #36, item C1)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw", [
    "pt:VORICONAZOLE",              # a preferred *term* — a label, not an identifier
    "LyCHI:RMWVQW96R8LU",           # a structure hash; nothing resolves it
    "PHAROS:nitric oxide",          # a source falling back to the drug's name
    "UniProtKB:P12345",             # a chemical is not a protein
    "NCBIGene:1234",
    "NotARegistry:1",
    "CHEBI:",                       # no local id
    "bare-string",
])
def test_unusable_cross_references_are_dropped(raw):
    assert normalize_object_id(raw) is None


@pytest.mark.parametrize("raw,expected", [
    ("ChEMBL:CHEMBL638", "chembl.compound:CHEMBL638"),
    ("PubChem:71616", "pubchem.compound:71616"),
    ("Guide to Pharmacology:6853", "iuphar.ligand:6853"),
    ("GTOPDB:13676", "iuphar.ligand:13676"),
    ("DrugCentral:2846", "drugcentral:2846"),
    ("RXCUI:1152373", "rxnorm:1152373"),
    ("unii:JFU09I87TR", "unii:JFU09I87TR"),
    ("CHEBI:10023", "CHEBI:10023"),
])
def test_prefixes_are_normalised_to_a_resolvable_namespace(raw, expected):
    assert normalize_object_id(raw) == expected


def test_every_exportable_prefix_expands():
    """A prefix that cannot expand would be declared in the curie_map and still be useless."""
    from medic.curie_utils import get_converter

    converter = get_converter()
    unresolvable = [p for p in EXPORTABLE_PREFIXES if not converter.expand(f"{p}:X")]
    assert not unresolvable, unresolvable


def _write_export(tmp_path, monkeypatch, drugs):
    from medic.export import sssom

    (tmp_path / "products").mkdir()
    (tmp_path / "exports").mkdir()
    (tmp_path / "products" / "drug_list.yaml").write_text(yaml.dump({"drugs": drugs}))
    monkeypatch.setattr(sssom, "PRODUCTS_DIR", tmp_path / "products")
    monkeypatch.setattr(sssom, "EXPORTS_DIR", tmp_path / "exports")
    sssom.export_sssom()
    return tmp_path / "exports" / "medic_drug_mappings.sssom.tsv"


DRUGS = [{
    "identity": {"resolved_id": "CHEBI:10023", "resolved_label": "voriconazole"},
    "alternate_ids": ["ChEMBL:CHEMBL638", "pt:VORICONAZOLE", "LyCHI:RMWVQW96R8LU",
                      "Guide to Pharmacology:6853", "unii:JFU09I87TR"],
    "drugbank_id": "DB00582",
}]


def test_the_export_parses_as_sssom(tmp_path, monkeypatch):
    """The check that string assertions cannot make.

    The header is YAML behind `#` markers, and the attribution notice contains
    "Data has been edited: source records were parsed" — an unquoted colon-space, which made
    YAML read it as a nested mapping and left the whole file unparseable by sssom-py.
    """
    from sssom.parsers import parse_sssom_table

    msdf = parse_sssom_table(str(_write_export(tmp_path, monkeypatch, DRUGS)))
    assert len(msdf.df) == 4          # chembl, iuphar, unii, drugbank; pt and LyCHI dropped
    assert msdf.prefix_map


def test_every_curie_in_the_export_expands(tmp_path, monkeypatch):
    from sssom.parsers import parse_sssom_table

    from medic.curie_utils import get_converter

    msdf = parse_sssom_table(str(_write_export(tmp_path, monkeypatch, DRUGS)))
    converter = get_converter()
    ids = set(msdf.df["object_id"]) | set(msdf.df["subject_id"])
    assert not [c for c in ids if not converter.expand(c)]
    assert not {c.partition(":")[0] for c in ids} - set(msdf.prefix_map)


def test_justification_does_not_claim_a_match_that_never_ran(tmp_path, monkeypatch):
    """These are cross-references copied from a field, not lexical comparisons."""
    text = _write_export(tmp_path, monkeypatch, DRUGS).read_text()
    assert "semapv:UnspecifiedMatching" in text
    assert "semapv:LexicalMatching" not in text


def _mapping_rows(path) -> dict[tuple[str, str], str]:
    """``(subject_id, object_id) -> predicate`` for every mapping row in the file."""
    out = {}
    for line in path.read_text().splitlines():
        if line.startswith("#") or line.startswith("subject_id"):
            continue
        subject_id, _label, predicate, object_id, *_ = line.split("\t")
        out[(subject_id, object_id)] = predicate
    return out


XREF_DRUGS = [{
    "identity": {"resolved_id": "CHEBI:10023", "resolved_label": "voriconazole"},
    "alternate_ids": ["DRUGBANK:DB00582", "UNII:JFU09I87TR"],
    "source_xrefs": ["CHEMBL.COMPOUND:CHEMBL638", "GTOPDB:6853", "RXCUI:121243",
                     "UNII:JFU09I87TR", "PHAROS:RMWVQW96R8LU", "pt:VORICONAZOLE"],
}]


def test_source_xrefs_are_close_matches_and_alternate_ids_stay_exact(tmp_path, monkeypatch):
    """`source_xrefs` are cross-references a source holds (PHAROS, matched by label; an
    RxNorm approximate match), not identities MeDIC established. They stay in the file —
    moving the slot must not silently drop ~9.5k published rows — but at `skos:closeMatch`,
    never promoted to the strongest claim SSSOM has. `alternate_ids` is exactMatch as before."""
    rows = _mapping_rows(_write_export(tmp_path, monkeypatch, XREF_DRUGS))
    assert rows[("CHEBI:10023", "DRUGBANK:DB00582")] == "skos:exactMatch"
    assert rows[("CHEBI:10023", "chembl.compound:CHEMBL638")] == "skos:closeMatch"
    assert rows[("CHEBI:10023", "iuphar.ligand:6853")] == "skos:closeMatch"
    assert rows[("CHEBI:10023", "rxnorm:121243")] == "skos:closeMatch"
    assert rows[("CHEBI:10023", "PHAROS:RMWVQW96R8LU")] == "skos:closeMatch"
    assert not [o for (_s, o) in rows if o.startswith("pt:")]   # still not an identifier


def test_an_object_in_both_slots_is_asserted_once_at_the_stronger_predicate(
    tmp_path, monkeypatch
):
    path = _write_export(tmp_path, monkeypatch, XREF_DRUGS)
    assert _mapping_rows(path)[("CHEBI:10023", "unii:JFU09I87TR")] == "skos:exactMatch"
    assert path.read_text().count("unii:JFU09I87TR") == 1


def test_close_match_rows_parse_as_sssom_with_the_same_columns(tmp_path, monkeypatch):
    from sssom.parsers import parse_sssom_table

    msdf = parse_sssom_table(str(_write_export(tmp_path, monkeypatch, XREF_DRUGS)))
    close = msdf.df[msdf.df["predicate_id"] == "skos:closeMatch"]
    assert len(close) == 4
    assert set(close["mapping_justification"]) == {"semapv:UnspecifiedMatching"}
    assert set(close["subject_label"]) == {"voriconazole"}


def test_the_everycure_precedent_still_holds_with_source_xrefs(tmp_path, monkeypatch):
    """The untrusted EveryCure id is filtered out of `alternate_ids` by the merge; the
    closeMatch channel must not become a back door for it. `source_xrefs` is only ever
    written by the PHAROS and RxNorm enrichments, so a merged record never carries the
    EveryCure id there — and the exporter never reads `everycure_id` itself."""
    from medic.merge.drug_merge import _merge_group

    merged = _merge_group("CHEBI:749610", [{
        "source": "EVERYCURE", "source_name": "Ofatumumab", "original_literal": "Ofatumumab",
        "everycure_id": "CHEBI:28887", "normalized_id": "CHEBI:749610",
        "normalized_label": "ofatumumab",
        "alternate_ids": ["CHEBI:28887", "DRUGBANK:DB06650"],
        "source_xrefs": ["UNII:M95KG522R0"],
    }])
    assert "CHEBI:28887" not in merged["alternate_ids"]
    assert merged["source_xrefs"] == ["UNII:M95KG522R0"]
    rows = _mapping_rows(_write_export(tmp_path, monkeypatch, [merged]))
    assert ("CHEBI:749610", "CHEBI:28887") not in rows
    assert rows[("CHEBI:749610", "DRUGBANK:DB06650")] == "skos:exactMatch"
    assert rows[("CHEBI:749610", "unii:M95KG522R0")] == "skos:closeMatch"

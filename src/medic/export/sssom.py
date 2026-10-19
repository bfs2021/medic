"""SSSOM export: generates drug identifier cross-reference mappings.

Produces exports/medic_drug_mappings.sssom.tsv from the primary identifier to all other
coding schemes, at two strengths:

* ``alternate_ids`` -> ``skos:exactMatch``. Ids MeDIC's own resolution established (the
  pre-normalization id, what the grounder returned).
* ``source_xrefs`` -> ``skos:closeMatch``. Cross-references copied from an enrichment
  source's record — PHAROS synonyms found by *label*, the RxNorm ingredient RxCUI found by
  *approximate* match. A source holds them for the entity it matched to this drug's name;
  MeDIC did not verify the identity, so they are not asserted as exact. They were formerly
  in ``alternate_ids`` and shipped as exactMatch; the weaker predicate keeps them in the
  file rather than dropping ~9.5k rows when the slot moved (#94).
"""

import logging
from pathlib import Path

import yaml

from medic import product_view as pv

logger = logging.getLogger(__name__)

PRODUCTS_DIR = Path("products")
EXPORTS_DIR = Path("exports")

#: Raw `alternate_ids` prefixes that name a real namespace under a spelling the bioregistry
#: does not recognise. Everything else is standardised by the converter itself.
_PREFIX_ALIASES = {
    "DrugCentral": "drugcentral",
    "PubChem": "pubchem.compound",
    "Guide to Pharmacology": "iuphar.ligand",
    "GTOPDB": "iuphar.ligand",
    "RXCUI": "rxnorm",
    # The bioregistry standardises `ChEMBL` to `chembl`, the *database*. The values here are
    # compound accessions (`ChEMBL:CHEMBL638`), so the resolvable namespace is the compound
    # one — otherwise the CURIE expands to a database landing page, not the molecule.
    "ChEMBL": "chembl.compound",
}

#: Values that are not identifiers at all, and must never become the object of a mapping.
#:
#: ``pt`` is a *preferred term* — a label (``pt:VORICONAZOLE``), so a row using it asserts
#: that a ChEBI id exactly matches a string. ``LyCHI`` is a structure hash, not a record in
#: any database, so nothing resolves it. Both were shipping because `alternate_ids` is a
#: passthrough of whatever each enrichment source happened to put there.
_NOT_IDENTIFIERS = {"pt", "LyCHI", "lychi"}

#: A drug mapping set maps drugs. An object in a gene or protein namespace is a category
#: error however well-formed the CURIE is — `CHEBI:x skos:exactMatch UniProtKB:y` claims a
#: chemical *is* a protein.
_WRONG_ENTITY_TYPE = {"ncbigene", "uniprot", "hgnc", "ensembl"}

#: Every prefix the exporter is allowed to emit, and therefore every prefix the header's
#: `curie_map` must declare. Closed on purpose: a new one has to be added here, which is the
#: moment to ask whether it resolves and whether it names a drug.
EXPORTABLE_PREFIXES = frozenset({
    "CHEBI", "DRUGBANK", "rxnorm", "unii", "pubchem.compound", "mesh", "DRON", "atc",
    "chembl.compound", "drugcentral", "iuphar.ligand", "umls", "PHAROS",
})

EXACT_MATCH = "skos:exactMatch"
CLOSE_MATCH = "skos:closeMatch"


def normalize_object_id(raw: str) -> str | None:
    """A raw `alternate_ids` entry as a usable CURIE, or ``None`` if it is not one.

    ``alternate_ids`` is a passthrough of whatever each enrichment source supplied, so it
    mixes real CURIEs with labels (``pt:VORICONAZOLE``), structure hashes (``LyCHI:...``) and
    prefixes spelled however that source spells them (``PubChem``, ``ChEMBL``,
    ``Guide to Pharmacology`` — with a space, so not a CURIE at all). 10,223 of 14,014
    exported rows used a prefix the file's own `curie_map` did not declare, which leaves a
    consumer unable to expand them to IRIs — the one thing a mapping set is for.
    """
    from medic.curie_utils import get_converter

    if not raw or ":" not in raw:
        return None
    prefix, _, local = raw.partition(":")
    local = local.strip()
    if not local:
        return None
    # A local identifier cannot contain whitespace. Catches sources that fall back to the
    # drug's *name* when they have no accession for it — `PHAROS:nitric oxide` is a label,
    # and a mapping to it asserts that a ChEBI id exactly matches a piece of text.
    if any(ch.isspace() for ch in local):
        return None
    if prefix in _NOT_IDENTIFIERS:
        return None
    prefix = _PREFIX_ALIASES.get(prefix, prefix)
    standard = get_converter().standardize_prefix(prefix)
    if standard is None:
        return None
    if standard in _WRONG_ENTITY_TYPE:
        return None
    if standard not in EXPORTABLE_PREFIXES and prefix not in EXPORTABLE_PREFIXES:
        return None
    return f"{standard if standard in EXPORTABLE_PREFIXES else prefix}:{local}"


def _as_list(value) -> list[str]:
    """A product slot as a list: absent -> ``[]``, a ``|``-joined string -> its parts."""
    if not value:
        return []
    if isinstance(value, str):
        return [x.strip() for x in value.split("|") if x.strip()]
    return [str(x) for x in value if x]


def _curie_map(prefixes: set[str]) -> dict[str, str]:
    """`curie_map` entries for exactly the prefixes in play, expanded by the converter.

    Generated rather than hand-written: a literal list is what drifted out of step with the
    data in the first place, and there is no reason for the header to know anything the
    converter cannot tell it.
    """
    from medic.curie_utils import get_converter

    converter = get_converter()
    out = {}
    for prefix in sorted(prefixes, key=str.lower):
        expanded = converter.expand(f"{prefix}:")
        if expanded:
            out[prefix] = expanded
    return out


def _build_header(prefixes: set[str] | None = None) -> str:
    """The SSSOM mapping-set header, including its licensing carve-out.

    The declared ``license`` is **CC BY 4.0, not CC0**. CC0 is right for MeDIC's own
    contribution — the assertion that string X grounds to CURIE Y, at a given confidence, by
    a given method — but the file also carries ``subject_label``, which reproduces verbatim
    strings from regulatory sources. EMA and PMDA both require attribution for those, so a
    file-level CC0 tells consumers attribution is optional when it is not: exactly the
    misreading LICENSING.md warns against. ``license`` is the one field a machine reads, so
    it states the obligation that actually attaches, and the comment records that MeDIC's
    mappings alone are still offered as CC0.

    The licence terms and attribution notice are read from ``conf/release_assets.yaml`` so
    they cannot drift from what the release actually ships.

    The notice comes from the *manifest*, not from ``plan()``. ``plan()`` narrows the notice
    to the assets present on disk, which is right for a release but wrong here: this header
    is built at import time, before any export runs, so on a clean checkout (``products/``
    untracked, ``exports/`` gitignored) it would resolve to no sources at all and silently
    drop the attribution.
    """
    from medic import release_assets

    license_url = "https://creativecommons.org/licenses/by/4.0/"
    assertions_url = "https://creativecommons.org/publicdomain/zero/1.0/"
    try:
        manifest = release_assets.load()
        notice = manifest.notice
        if manifest.license:
            license_url = manifest.license.medic_contribution or license_url
            assertions_url = manifest.license.medic_assertions_offered_as or assertions_url
            # Naming the sources is not enough: a single-notice format must also say the
            # upstream terms carry over. The two always travel together.
            notice = f"{notice} {manifest.license.passthrough}".strip()
    except (OSError, ValueError):  # manifest unreadable — never fail an export over it
        notice = ""

    carve_out = (
        f"MeDIC's own mapping assertions (that a source string resolves to a given CURIE, "
        f"by a given method, at a given confidence) are offered as {assertions_url}. The "
        f"set is declared {license_url} because subject_label reproduces verbatim strings "
        f"from the regulatory sources, which require attribution. See LICENSING.md."
    )
    comment = f"{carve_out} {notice}".strip()

    # An SSSOM header is YAML with every line prefixed by `#`, so it is *dumped*, never
    # concatenated. Hand-built, the `comment` value broke it: the attribution notice contains
    # "Data has been edited: source records were parsed...", and an unquoted colon-space makes
    # YAML read it as a nested mapping key. sssom-py could not parse the file at all —
    # invisible to any check that reads the TSV as text, which is why this ships a real parse
    # in `tests/test_sssom_export.py` rather than more string assertions.
    document = {
        "curie_map": _curie_map(set(prefixes or EXPORTABLE_PREFIXES)),
        "mapping_set_id": "https://w3id.org/monarch-initiative/medic/mappings",
        "mapping_set_title": "MeDIC Drug Identifier Mappings",
        "mapping_set_description": (
            "Cross-reference mappings between drug identifiers from multiple sources"
        ),
        "license": license_url,
        "comment": comment,
    }
    dumped = yaml.safe_dump(
        document, sort_keys=False, default_flow_style=False, allow_unicode=True, width=10**9
    )
    return "".join(f"#{line}\n" for line in dumped.splitlines())


SSSOM_HEADER = _build_header()


def export_sssom() -> None:
    """Export drug identifier mappings as SSSOM TSV."""
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

    drug_list_path = PRODUCTS_DIR / "drug_list.yaml"
    if not drug_list_path.exists():
        logger.warning("No drug list found at %s", drug_list_path)
        return

    with open(drug_list_path) as f:
        data = yaml.safe_load(f)

    drugs = data.get("drugs", []) if isinstance(data, dict) else data

    # (subject_id, subject_label, predicate, object_id)
    rows: set[tuple[str, str, str, str]] = set()
    dropped: dict[str, int] = {}
    for drug in drugs:
        primary_id = pv.drug_id(drug)
        primary_label = pv.drug_label(drug)
        if not primary_id:
            continue

        alt_ids = drug.get("alternate_ids", [])
        if isinstance(alt_ids, str):
            alt_ids = [x.strip() for x in alt_ids.split("|") if x.strip()]

        drugbank_id = drug.get("drugbank_id", "")
        if drugbank_id:
            alt_ids = [*alt_ids, f"DRUGBANK:{drugbank_id}"]

        exact_objects: set[str] = set()
        for predicate, raw_ids in (
            (EXACT_MATCH, alt_ids),
            (CLOSE_MATCH, _as_list(drug.get("source_xrefs"))),
        ):
            for raw_id in raw_ids:
                if raw_id == primary_id:
                    continue
                object_id = normalize_object_id(raw_id)
                if object_id is None:
                    key = raw_id.partition(":")[0] or "<no prefix>"
                    dropped[key] = dropped.get(key, 0) + 1
                    continue
                if object_id == primary_id:
                    continue
                # The same object in both slots is asserted once, at the stronger predicate.
                if predicate == EXACT_MATCH:
                    exact_objects.add(object_id)
                elif object_id in exact_objects:
                    continue
                rows.add((primary_id, primary_label, predicate, object_id))

    # Deduplicated and ordered, so the file is a set rather than a log of the walk and two
    # builds of the same products are byte-identical.
    ordered = sorted(rows)
    prefixes = ({r[3].partition(":")[0] for r in ordered}
                | {r[0].partition(":")[0] for r in ordered})

    output_path = EXPORTS_DIR / "medic_drug_mappings.sssom.tsv"
    with open(output_path, "w") as f:
        f.write(_build_header(prefixes))
        f.write(
            "subject_id\tsubject_label\tpredicate_id\tobject_id\tobject_label\t"
            "mapping_justification\n"
        )
        for subject_id, subject_label, predicate, object_id in ordered:
            # `semapv:UnspecifiedMatching`, not `LexicalMatching`: these are cross-references
            # copied from a source's own field. Nothing lexical was compared, and claiming a
            # method that never ran misrepresents how the mapping was arrived at.
            f.write(
                f"{subject_id}\t{subject_label}\t{predicate}\t"
                f"{object_id}\t\tsemapv:UnspecifiedMatching\n"
            )

    if dropped:
        logger.info(
            "Dropped %d cross-reference(s) that are not usable mappings: %s",
            sum(dropped.values()),
            ", ".join(f"{k}={v}" for k, v in sorted(dropped.items(), key=lambda x: -x[1])),
        )
    logger.info("Exported %d mappings to %s", len(ordered), output_path)


def main():
    logging.basicConfig(level=logging.INFO)
    export_sssom()


if __name__ == "__main__":
    main()

"""Compile all configured sources into one on-disk SQLite lexical index."""

from __future__ import annotations

import sqlite3

from medic.grounding.lexical.index import COLS, LexicalIndex
from medic.grounding.lexical.loaders.obo_json import load_obo_json
from medic.grounding.lexical.loaders.umls import load_umls
from medic.grounding.lexical.sources import (
    check_pin,
    load_sources,
    ontology_for_source,
    pinned_ontologies,
)


def _iter_source(entry: dict):
    loader = entry["loader"]
    if loader == "obo_json":
        yield from load_obo_json(entry["path"], entry["prefix"], entry.get("iri_prefix"))
    elif loader == "umls":
        yield from load_umls(entry["path"], member=entry.get("member", "MRCONSO.RRF"))
    elif loader == "icd10cm":
        from medic.grounding.lexical.loaders.icd10cm import load_icd10cm
        yield from load_icd10cm(entry["path"], entry["prefix"])
    elif loader == "omop_rxnorm":
        from medic.grounding.lexical.loaders.omop_rxnorm import load_omop_rxnorm
        yield from load_omop_rxnorm(
            entry["path"], member=entry.get("member", "CONCEPT.csv"),
            prefix=entry.get("prefix", "RXCUI"))
    else:
        raise ValueError(f"unknown loader {loader!r}")


def build_index(entity_type: str, sources_conf: str, out_db: str) -> int:
    entries = load_sources(sources_conf)[entity_type]
    pins = pinned_ontologies(sources_conf)
    con = sqlite3.connect(out_db)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("DROP TABLE IF EXISTS lex")
    con.execute(f"CREATE TABLE lex ({', '.join(c + ' TEXT' for c in COLS)})")
    # Which release of each ontology this index was compiled from. An index is a derived
    # artefact that outlives the download it came from; without this nobody can say which
    # term set a grounding decision was made against (#94).
    con.execute("DROP TABLE IF EXISTS meta")
    con.execute("CREATE TABLE meta (prefix TEXT, ontology TEXT, version TEXT, url TEXT)")
    for entry in entries:
        name = ontology_for_source(entry)
        if name is None:
            continue
        if name not in pins:
            raise ValueError(
                f"{sources_conf}: source {entry.get('prefix')!r} names ontology {name!r}, "
                f"which has no pin under `ontologies:`"
            )
        pin = pins[name]
        # Stamping a version is only worth anything if it is the version on disk: check
        # before recording, so a bumped pin with a stale download fails the build instead
        # of publishing a false provenance claim.
        check_pin(pin, entry["path"])
        con.execute("INSERT INTO meta VALUES (?, ?, ?, ?)",
                    (entry["prefix"], name, pin.version, pin.url))
    insert = f"INSERT INTO lex VALUES ({', '.join('?' * len(COLS))})"
    n = 0
    for entry in entries:
        batch = []
        for row in _iter_source(entry):
            batch.append(tuple(getattr(row, c) for c in COLS))
            if len(batch) >= 50_000:
                con.executemany(insert, batch)
                n += len(batch)
                batch = []
        if batch:
            con.executemany(insert, batch)
            n += len(batch)
    con.execute("CREATE INDEX ix_raw ON lex (raw_value, match_field)")
    con.execute("CREATE INDEX ix_norm ON lex (norm_value, match_field)")
    con.commit()
    con.close()
    return n


def index_meta(db_path: str) -> list[dict]:
    """The ontology releases an index was compiled from (empty for an index built before
    the ``meta`` table existed — those are exactly the builds with unknown versions)."""
    con = sqlite3.connect(db_path)
    try:
        has_meta = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
        if not has_meta:
            return []
        rows = con.execute(
            "SELECT prefix, ontology, version, url FROM meta ORDER BY prefix").fetchall()
    finally:
        con.close()
    return [dict(zip(("prefix", "ontology", "version", "url"), r)) for r in rows]


def open_index(entity_type: str, db_path: str) -> LexicalIndex:
    return LexicalIndex(entity_type, db_path)

"""Compile all configured sources into one on-disk SQLite lexical index."""

from __future__ import annotations

import sqlite3

import yaml

from medic.grounding.lexical.index import COLS, LexicalIndex
from medic.grounding.lexical.loaders.obo_json import load_obo_json
from medic.grounding.lexical.loaders.umls import load_umls


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
    with open(sources_conf) as fh:
        entries = yaml.safe_load(fh)[entity_type]
    con = sqlite3.connect(out_db)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("DROP TABLE IF EXISTS lex")
    con.execute(f"CREATE TABLE lex ({', '.join(c + ' TEXT' for c in COLS)})")
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


def open_index(entity_type: str, db_path: str) -> LexicalIndex:
    return LexicalIndex(entity_type, db_path)

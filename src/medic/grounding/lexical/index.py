"""Unified lexical index — on-disk SQLite, queried directly (not RAM-materialized).

Table ``lex`` holds one row per (object_id, source string, match_field) with two keyed
forms: ``raw_value`` (whitespace-trimmed, case-sensitive) for tier-1 exact matches, and
``norm_value`` (``base_normalize``) for tier-2/3. B-tree indexes on both. UMLS/UniProt
run to millions of rows, so we never load the whole thing into memory.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

VOCAB_ORDER = {
    "diseases": ["MONDO", "HP", "ICD10CM", "UMLS"],
    # RXCUI last on purpose: it is a clinical-terminology identifier, so CHEBI's chemistry
    # is preferred wherever it matches, and RxNorm fills the regional and older approvals
    # the ontologies do not carry. The matcher is vocabulary-major, so this ordering is what
    # actually decides ties (#94).
    "drugs": ["CHEBI", "DRON", "PR", "UniProt", "RXCUI"],
}

COLS = ("object_id", "object_label", "string_value", "raw_value", "norm_value",
        "match_field", "synonym_scope", "source_prefix")


@dataclass
class LexRow:
    object_id: str
    object_label: str
    string_value: str
    raw_value: str
    norm_value: str
    match_field: str      # label | exactSynonym | broadSynonym | narrowSynonym | relatedSynonym
    synonym_scope: str    # exact | broad | narrow | related
    source_prefix: str


class LexicalIndex:
    """Thin query wrapper over the compiled SQLite ``lex`` table."""

    def __init__(self, entity_type: str, db_path: str):
        self.entity_type = entity_type
        self.vocab_order = VOCAB_ORDER[entity_type]
        self._con = sqlite3.connect(db_path)

    def _query(self, column: str, value: str, match_field: str) -> list[LexRow]:
        cur = self._con.execute(
            f"SELECT {','.join(COLS)} FROM lex "
            f"WHERE {column} = ? AND match_field = ? ORDER BY object_id",
            (value, match_field),
        )
        return [LexRow(*row) for row in cur.fetchall()]

    def lookup_raw(self, raw_value: str, match_field: str) -> list[LexRow]:
        return self._query("raw_value", raw_value, match_field)

    def lookup_norm(self, norm_value: str, match_field: str) -> list[LexRow]:
        return self._query("norm_value", norm_value, match_field)

    def lookup_norm_many(self, norm_values, match_field: str) -> list[LexRow]:
        """Batched norm_value lookup (for fuzzy candidate sets). Chunked to stay under
        SQLite's parameter limit."""
        values = list(norm_values)
        out: list[LexRow] = []
        for i in range(0, len(values), 500):
            chunk = values[i:i + 500]
            placeholders = ",".join("?" * len(chunk))
            cur = self._con.execute(
                f"SELECT {','.join(COLS)} FROM lex "
                f"WHERE match_field = ? AND norm_value IN ({placeholders}) ORDER BY object_id",
                (match_field, *chunk),
            )
            out.extend(LexRow(*row) for row in cur.fetchall())
        return out

    def close(self) -> None:
        self._con.close()

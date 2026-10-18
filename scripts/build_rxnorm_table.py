#!/usr/bin/env python3
"""Regenerate `data/vocab/rxnorm_ingredients.tsv` from an OMOP/ATHENA vocabulary download.

**You do not need to run this to build MeDIC.** The table it produces is committed, because
the grounding index must be reproducible from a checkout — a loader pointing at somebody's
local 1GB dump is not. Run this only to refresh the table against a newer vocabulary release.

    python scripts/build_rxnorm_table.py path/to/vocabulary_download_v5_*.zip

What it extracts, and why only this:

* `vocabulary_id == "RxNorm"` only. The ATHENA bundle also carries SNOMED, MedDRA and other
  vocabularies whose term text MeDIC may not republish (I-14 rule 2). Filtering at extraction
  time keeps restricted content out of the committed artifact entirely, rather than relying on
  a downstream check. RxNorm's own content is UMLS Appendix 1 Category 0 — no additional
  restriction — which is what makes the table redistributable; see LICENSING.md.
* `Ingredient` and `Precise Ingredient` classes only. A clinical drug ("lansoprazole 30 MG
  oral capsule") denotes a dose form, not a substance, and indexing those reintroduces what
  #94 reports about DRON clinical-drug forms: the wrong kind of node for a drug-disease edge.
* Drug domain, not deprecated.

Three columns: the OMOP concept id, the RxNorm identifier (RXCUI), and the label.
"""

from __future__ import annotations

import csv
import io
import sys
import zipfile
from pathlib import Path

OUT = Path("data/vocab/rxnorm_ingredients.tsv")
INGREDIENT_CLASSES = {"Ingredient", "Precise Ingredient"}
HEADER = ("omop_concept_id", "rxcui", "label")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    src = Path(sys.argv[1])
    if not src.exists():
        print(f"not found: {src}", file=sys.stderr)
        return 2

    rows: list[tuple[str, str, str]] = []
    with zipfile.ZipFile(src) as zf, zf.open("CONCEPT.csv") as fh:
        text = io.TextIOWrapper(fh, encoding="utf-8", errors="replace", newline="")
        for r in csv.DictReader(text, delimiter="\t"):
            if r.get("vocabulary_id") != "RxNorm":
                continue
            if r.get("concept_class_id") not in INGREDIENT_CLASSES:
                continue
            if (r.get("invalid_reason") or "").strip():
                continue
            if r.get("domain_id") != "Drug":
                continue
            cid = (r.get("concept_id") or "").strip()
            rxcui = (r.get("concept_code") or "").strip()
            label = (r.get("concept_name") or "").strip()
            if cid and rxcui and label:
                rows.append((cid, rxcui, label))

    rows.sort(key=lambda t: int(t[0]))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n")
        w.writerow(HEADER)
        w.writerows(rows)
    print(f"wrote {OUT} — {len(rows):,} RxNorm ingredient concepts "
          f"({OUT.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

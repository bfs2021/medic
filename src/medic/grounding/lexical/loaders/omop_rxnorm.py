"""Loader for RxNorm ingredient concepts from the committed vocabulary table.

**Why RxNorm.** The drug-coverage gap against v1.0.0 is 1,484 drugs that are genuinely absent
rather than merely re-identified (`just v1-drug-gap`), and 589 of them were identified by
RXCUI in v1. 276 of the 1,484 resolve to an RXCUI on an exact ingredient-name match, and they
are exactly the regional and older approvals the CHEBI cascade misses — Levonordefrin,
Delavirdine, Pemirolast, Prothipendyl, Tertatolol, Bencyclane. RxNorm also indexes the
US-label spellings an SPL actually uses, which is where a chemistry ontology is weakest.

**Reads a committed table, not a vocabulary dump.** `data/vocab/rxnorm_ingredients.tsv` holds
three columns — OMOP concept id, RXCUI, label — for 18,279 ingredient concepts, 0.7MB. The
grounding index has to be reproducible from a checkout, and a loader pointing at a 1GB ATHENA
download on one machine is not: nobody else could build MeDIC. `scripts/build_rxnorm_table.py`
regenerates the table against a newer release and is the only thing that needs the dump.

**What the table deliberately excludes**, decided at extraction time rather than here so the
committed artifact can never carry it:

* Anything but `vocabulary_id == "RxNorm"`. The ATHENA bundle also carries SNOMED, MedDRA and
  other vocabularies whose term text MeDIC may not republish (I-14 rule 2). RxNorm's own
  content is UMLS Appendix 1 Category 0 — no additional restriction — which is what makes
  this table redistributable at all. See LICENSING.md.
* Dose forms and packs. A clinical drug ("lansoprazole 30 MG oral capsule") denotes a form,
  not a substance, and indexing those reintroduces what #94 reports about DRON clinical-drug
  forms: the wrong kind of node for a drug-disease edge. `Precise Ingredient` is kept, since a
  specific salt or ester is still a substance.
* Deprecated concepts. They still resolve for a consumer holding the id, but MeDIC should not
  newly ground anything to one.
"""

from __future__ import annotations

import csv
import logging
from collections.abc import Iterator
from pathlib import Path

from medic.grounding.lexical.index import LexRow
from medic.grounding.lexical.preprocess import base_normalize

logger = logging.getLogger(__name__)

DEFAULT_PATH = "data/vocab/rxnorm_ingredients.tsv"


def load_omop_rxnorm(
    path: str | Path = DEFAULT_PATH, prefix: str = "RXCUI", **_: object,
) -> Iterator[LexRow]:
    """Yield one ``label`` row per RxNorm ingredient in the committed table.

    Labels only. RxNorm synonyms are a separate 215MB file in the dump and a separate
    licensing assessment, so they are deliberately not included.
    """
    path = Path(path)
    if not path.exists():
        logger.warning(
            "RxNorm table not found at %s; RxNorm will be absent from the index. "
            "Regenerate it with scripts/build_rxnorm_table.py.", path,
        )
        return

    kept = 0
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            rxcui = (row.get("rxcui") or "").strip()
            label = (row.get("label") or "").strip()
            if not rxcui or not label:
                continue
            kept += 1
            yield LexRow(
                object_id=f"{prefix}:{rxcui}",
                object_label=label,
                string_value=label,
                raw_value=label,
                norm_value=base_normalize(label),
                match_field="label",
                synonym_scope="exact",
                source_prefix=prefix,
            )
    logger.info("RxNorm: %d ingredient concepts indexed from %s", kept, path)

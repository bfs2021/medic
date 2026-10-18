"""Loader for RxNorm ingredient concepts, streamed from an OMOP/ATHENA vocabulary zip.

**Why RxNorm.** The drug-coverage gap against v1.0.0 is 1,484 drugs that are genuinely
absent rather than merely re-identified (`just v1-drug-gap`), and 589 of them were
identified by RXCUI in v1. 276 of the 1,484 resolve to an RXCUI on an exact ingredient-name
match, and they are exactly the regional and older approvals the CHEBI cascade misses —
Levonordefrin, Delavirdine, Pemirolast, Prothipendyl, Tertatolol, Bencyclane. RxNorm also
indexes the US-label spellings an SPL actually uses, which is where a chemistry ontology is
weakest.

**Scoped to `vocabulary_id == "RxNorm"`, deliberately.** The ATHENA bundle also carries
SNOMED, MedDRA and other vocabularies whose term text MeDIC may not republish (I-14 rule 2).
Filtering here keeps restricted content out of the index entirely rather than relying on a
downstream check to catch it — the same reasoning that turned the UMLS label blocklist into
an allowlist.

**Licensing decision (record in LICENSING.md alongside any change here).** RxNorm is produced
by the US National Library of Medicine. RxNorm's own content — RXCUIs and RxNorm normalized
names — carries no additional use or redistribution restriction (UMLS licence category 0),
so ingredient names may be published as `object_label`. This does *not* extend to the other
vocabularies in the bundle, which is why the filter is on `vocabulary_id` and not on
`domain_id` alone.

**Ingredient level only.** A clinical drug ("lansoprazole 30 MG oral capsule") denotes a dose
form, not a substance, and indexing those would reintroduce exactly the problem #94 reports
for DRON clinical-drug forms: the wrong *kind* of node for a drug-disease edge, which nothing
else in the graph joins to. "Precise Ingredient" is kept because a specific salt or ester is
still a substance.

CONCEPT.csv is ~1GB inside the zip and is streamed, never extracted.
"""

from __future__ import annotations

import csv
import io
import logging
import zipfile
from collections.abc import Iterator
from pathlib import Path

from medic.grounding.lexical.index import LexRow
from medic.grounding.lexical.preprocess import base_normalize

logger = logging.getLogger(__name__)

#: Substance-level RxNorm classes. Everything else in RxNorm is a dose form or a pack.
INGREDIENT_CLASSES = frozenset({"Ingredient", "Precise Ingredient"})

#: The one vocabulary in the bundle this loader will read. See the licensing note above.
VOCABULARY = "RxNorm"


def load_omop_rxnorm(
    path: str | Path, member: str = "CONCEPT.csv", prefix: str = "RXCUI",
) -> Iterator[LexRow]:
    """Yield one ``label`` row per valid RxNorm ingredient concept.

    Only labels: RxNorm synonyms live in CONCEPT_SYNONYM.csv, which is a separate 215MB file
    and a separate licensing assessment, so they are deliberately not pulled in here.
    """
    path = Path(path)
    if not path.exists():
        logger.warning(
            "OMOP vocabulary zip not found at %s; RxNorm will be absent from the index. "
            "Download it from athena.ohdsi.org (RxNorm is sufficient; the bundle's other "
            "vocabularies are not read).", path,
        )
        return

    kept = skipped_vocab = skipped_class = skipped_invalid = skipped_domain = 0
    with zipfile.ZipFile(path) as zf, zf.open(member) as fh:
        text = io.TextIOWrapper(fh, encoding="utf-8", errors="replace", newline="")
        for row in csv.DictReader(text, delimiter="\t"):
            if row.get("vocabulary_id") != VOCABULARY:
                skipped_vocab += 1
                continue
            if row.get("concept_class_id") not in INGREDIENT_CLASSES:
                skipped_class += 1
                continue
            # A deprecated concept still resolves for a consumer who already has the id, but
            # MeDIC should not newly ground anything to it.
            if (row.get("invalid_reason") or "").strip():
                skipped_invalid += 1
                continue
            # RxNorm carries a few ingredient concepts in non-Drug domains (Device, mostly
            # product-ish entries). Those are not drugs and should not be groundable as one.
            if row.get("domain_id") != "Drug":
                skipped_domain += 1
                continue
            code = (row.get("concept_code") or "").strip()
            name = (row.get("concept_name") or "").strip()
            if not code or not name:
                continue
            kept += 1
            yield LexRow(
                object_id=f"{prefix}:{code}",
                object_label=name,
                string_value=name,
                raw_value=name.strip(),
                norm_value=base_normalize(name),
                match_field="label",
                synonym_scope="exact",
                source_prefix=prefix,
            )

    logger.info(
        "RxNorm: %d ingredient concepts indexed (skipped %d non-RxNorm, %d non-ingredient, "
        "%d deprecated, %d non-Drug domain)",
        kept, skipped_vocab, skipped_class, skipped_invalid, skipped_domain,
    )

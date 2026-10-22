#!/usr/bin/env python3
"""Move SPL labels that cannot contribute, or can only contribute wrongly, out of the mined
corpus.

    just prune-dailymed-corpus            # dry run: report only
    just prune-dailymed-corpus apply      # move them to data/raw/dailymed_excluded/

**Why.** The ingester mines *every* XML in ``data/raw/dailymed/``, so the corpus is the union
of every acquisition run ever made — including runs from before `acquire.py` filtered the
search pool by human document type (#98/#103). Two kinds of file in there are not worth
mining:

* **Animal labels.** 528 of them. An indication extracted from a veterinary label is a claim
  about an animal product credited to a human drug: "ivermectin treats heartworm in dogs"
  becomes an ivermectin indication. This is the concrete form of the problem Kevin found
  when the old acquirer picked a veterinary label for ivermectin, and filtering future
  searches does not remove the ones already on disk.
* **Labels with neither an Indications (34067-9) nor a Contraindications (34070-3)
  section.** 764 of them. They cost a parse and can yield nothing.

**Why it moves rather than deletes.** Acquisition is slow and rate-limited, and a label that
turns out to have been wrongly excluded should cost a `mv` to get back, not a refetch. The
destination keeps the reason in the path.

**What it deliberately leaves alone.** BULK INGREDIENT, DIETARY SUPPLEMENT, VACCINE LABEL,
PLASMA DERIVATIVE, MEDICAL FOOD, COSMETIC and the rest are reported but not moved. Some
carry real claims (a dietary supplement has a "uses" section; a vaccine label has
indications) and whether MeDIC wants them is a scope question about what counts as a drug,
not an obvious defect like an animal label. Deciding it silently inside a cleanup script is
how scope creeps.

**Why the classification reads displayName and not a hardcoded LOINC list.** Written from a
guessed list first, and the guess was wrong in both directions: it had ``53406-5`` down as
veterinary (it is LICENSED VACCINE BULK INTERMEDIATE LABEL) and missed ``50575-0``,
``50576-8`` and ``81203-2``, which are animal types. Every SPL carries the human-readable
type on its own document code, so that is what gets read.
"""

from __future__ import annotations

import argparse
import collections
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

DEFAULT_DIR = Path("data/raw/dailymed")
DEFAULT_EXCLUDED = Path("data/raw/dailymed_excluded")

LOINC = "2.16.840.1.113883.6.1"
#: Section codes the ingester mines. A label with neither can contribute nothing.
LOINC_INDICATIONS = b'code="34067-9"'
LOINC_CONTRAINDICATIONS = b'code="34070-3"'

#: Document types MeDIC mines. Everything else is reported, and only animal labels are moved.
HUMAN_DRUG_TYPES = {
    "34391-3",   # HUMAN PRESCRIPTION DRUG LABEL
    "34390-5",   # HUMAN OTC DRUG LABEL
    "45129-4",   # HUMAN PRESCRIPTION DRUG LABELING WITH HIGHLIGHTS
}

REASON_ANIMAL = "animal_label"
REASON_NO_SECTION = "no_indication_or_contraindication_section"


def document_type(path: Path) -> tuple[str, str]:
    """``(code, displayName)`` of an SPL's document-level type, or ``("", "")``.

    HL7 v3 SPL puts the document type on the ``<code>`` that is a direct child of
    ``<document>``, before the first ``<component>``. Parsed incrementally and abandoned at
    the first component, because the header is a few KB of a file that can be megabytes —
    and because matching the *first* LOINC code in the file by position picks up a section
    code (34067-9, 34066-1, 42229-5) on labels whose document code comes later.
    """
    try:
        for _event, elem in ET.iterparse(path, events=("start",)):
            tag = elem.tag.split("}")[-1]
            if tag == "code" and elem.get("codeSystem") == LOINC:
                return (elem.get("code") or "", elem.get("displayName") or "")
            if tag == "component":
                return ("", "")
    except ET.ParseError:
        return ("", "")
    return ("", "")


def is_animal(code: str, display: str) -> bool:
    """Is this an animal label? Read off the type's own display name.

    ``ANIMAL`` appears in every veterinary type DailyMed serves — PRESCRIPTION ANIMAL DRUG
    LABEL, OTC ANIMAL DRUG LABEL, BULK INGREDIENT - ANIMAL DRUG, VFD TYPE A MEDICATED
    ARTICLE ANIMAL DRUG LABEL, INTENTIONAL ANIMAL GENOMIC ALTERATION LABEL — and in none of
    the human ones.
    """
    return "ANIMAL" in display.upper()


def has_mined_section(path: Path) -> bool:
    data = path.read_bytes()
    return LOINC_INDICATIONS in data or LOINC_CONTRAINDICATIONS in data


def classify(data_dir: Path) -> tuple[dict[Path, str], collections.Counter, dict[str, str]]:
    """``{path: reason}`` for the labels to move, plus a tally of every type seen."""
    to_move: dict[Path, str] = {}
    types: collections.Counter = collections.Counter()
    names: dict[str, str] = {}
    for path in sorted(data_dir.glob("*.xml")):
        code, display = document_type(path)
        key = f"{code or '(none)'}  {display}"
        types[key] += 1
        names.setdefault(code, display)
        if is_animal(code, display):
            to_move[path] = REASON_ANIMAL
        elif not has_mined_section(path):
            to_move[path] = REASON_NO_SECTION
    return to_move, types, names


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DIR)
    ap.add_argument("--excluded-dir", type=Path, default=DEFAULT_EXCLUDED)
    ap.add_argument("--apply", action="store_true",
                    help="actually move the files (default is to report only)")
    args = ap.parse_args()

    if not args.data_dir.exists():
        raise SystemExit(f"no corpus at {args.data_dir}")

    to_move, types, _names = classify(args.data_dir)
    total = sum(types.values())

    print(f"{total:,} labels in {args.data_dir}\n")
    print("document types:")
    for key, n in types.most_common():
        mark = "  <- animal" if "ANIMAL" in key.upper() else ""
        keep = "" if key.split()[0] in HUMAN_DRUG_TYPES else "   (not a human drug label)"
        print(f"  {n:7,}  {key}{mark}{keep}")

    by_reason = collections.Counter(to_move.values())
    print(f"\nto move: {len(to_move):,} of {total:,} ({100 * len(to_move) / max(total, 1):.1f}%)")
    for reason, n in by_reason.most_common():
        print(f"  {n:7,}  {reason}")

    if not args.apply:
        print(f"\nDry run. Pass --apply to move them under {args.excluded_dir}/<reason>/.")
        return 0

    for path, reason in to_move.items():
        dest_dir = args.excluded_dir / reason
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest_dir / path.name))
    print(f"\nMoved {len(to_move):,} labels under {args.excluded_dir}/. "
          f"Re-run the ingest to see the effect; `mv` them back to undo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

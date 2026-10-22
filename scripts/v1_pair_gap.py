#!/usr/bin/env python3
"""Compare v1.0.0's grounded drug-disease pairs with the current build, hierarchy-aware.

    just v1-pair-gap            # needs products/ built and cache/ontologies/mondo.json

**Why this exists.** The v1.0.0 release assets carry only drug lists, so comparisons stopped
at the drug level for a long time. Its *grounded* pair lists are in the old pipeline's
reporting output on `main` — 11,071 indication and 3,981 contraindication pairs with a final
normalized drug id and disease id — which this script reads directly from git.

**Why hierarchy-aware.** Matching on id, or on id plus exact label, counts a pair as missing
whenever the two releases named the disease at a different level of specificity:

    v1 "urinary tract infection"      ours "Recurrent urinary tract infections"
    v1 "Acute insomnia"               ours "Insomnia"
    v1 "metastatic colorectal cancer" ours "Colorectal cancer"

That is 20.2% of the apparent gap. Two of those classes are not losses at all:

* **ancestor** — we hold a parent of v1's disease for the same drug. v1 propagated
  indications *down* the Mondo hierarchy, so a drug approved for a category gained a pair for
  every subtype. `docs/architecture.md` §5.7 records that this was removed deliberately:
  annotations travel up a hierarchy, not down.
* **descendant** — we assert something *more* specific than v1 did. Scoring that as a loss
  punishes an improvement.

Reporting the strict number alone overstated the regression, repeatedly and by me. A strict
join is still printed, because it is the right number for "can a consumer look this exact
pair up", but it is no longer the only one.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd
import yaml

V1_DIR = "medi/indications/data/08_reporting/indications_medi_coverage"
V1_FILES = {
    "indication": f"{V1_DIR}/matrix_indication_list.xlsx",
    "contraindication": f"{V1_DIR}/matrix_contraindication_list.xlsx",
}
MONDO = Path("cache/ontologies/mondo.json")
#: How far up the hierarchy to look. Mondo is deep; beyond a handful of steps an "ancestor"
#: is too general to mean the two releases agreed about anything.
MAX_DEPTH = 6


def norm(s: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def _from_git(path: str, ref: str) -> Path:
    """Extract a file from a git ref to a temp path. Keeps v1's data un-duplicated."""
    out = Path(tempfile.gettempdir()) / f"medic_v1_{Path(path).name}"
    with out.open("wb") as fh:
        res = subprocess.run(["git", "show", f"{ref}:{path}"], stdout=fh,
                             stderr=subprocess.PIPE, check=False)
    if res.returncode != 0 or not out.stat().st_size:
        raise SystemExit(f"could not read {path} from {ref}: "
                         f"{res.stderr.decode()[:200]}")
    return out


def mondo_parents() -> dict[str, set[str]]:
    """child CURIE -> direct parent CURIEs, from the Mondo obographs build input."""
    if not MONDO.exists():
        raise SystemExit(f"missing {MONDO} — run `just download-grounding-ontologies`")
    graph = json.loads(MONDO.read_text())["graphs"][0]
    parents: dict[str, set[str]] = collections.defaultdict(set)
    for e in graph.get("edges", ()):
        if e.get("pred") != "is_a":
            continue
        sub, obj = str(e.get("sub", "")), str(e.get("obj", ""))
        if "MONDO_" in sub and "MONDO_" in obj:
            parents[f"MONDO:{sub.rsplit('MONDO_', 1)[1]}"].add(
                f"MONDO:{obj.rsplit('MONDO_', 1)[1]}")
    return parents


def ancestors(curie: str, parents, depth: int = MAX_DEPTH) -> set[str]:
    seen: set[str] = set()
    frontier = {curie}
    for _ in range(depth):
        nxt: set[str] = set()
        for node in frontier:
            for par in parents.get(node, ()):
                if par not in seen:
                    seen.add(par)
                    nxt.add(par)
        frontier = nxt
        if not frontier:
            break
    return seen


def load_v1(kind: str, ref: str) -> list[dict]:
    d = pd.read_excel(_from_git(V1_FILES[kind], ref), dtype=str).fillna("")
    return [{"drug": r["final normalized drug label"],
             "drug_id": r["final normalized drug id"],
             "dis": r["final normalized disease label"],
             "dis_id": r["final normalized disease id"]}
            for _, r in d.iterrows()]


def load_now(path: Path) -> list[dict]:
    data = yaml.safe_load(path.read_text()) if path.exists() else {}
    return [{"drug": a.get("drug_label") or "", "drug_id": a.get("drug_id") or "",
             "dis": a.get("disease_label") or "", "dis_id": a.get("disease_id") or "",
             "tier": a.get("reliability") or ""}
            for a in (data.get("associations") or [])]


def classify(v1: list[dict], now: list[dict], parents) -> tuple[collections.Counter, list]:
    now_ids = {(r["drug_id"], r["dis_id"]) for r in now}
    now_names = {(norm(r["drug"]), norm(r["dis"])) for r in now}
    by_drug_ids: dict[str, set[str]] = collections.defaultdict(set)
    by_drug_names: dict[str, set[str]] = collections.defaultdict(set)
    for r in now:
        by_drug_ids[norm(r["drug"])].add(r["dis_id"])
        by_drug_names[norm(r["drug"])].add(norm(r["dis"]))

    counts: collections.Counter = collections.Counter()
    examples: list[tuple[str, str, str]] = []
    for r in v1:
        dn, zid, zn = norm(r["drug"]), r["dis_id"], norm(r["dis"])
        if (r["drug_id"], zid) in now_ids:
            counts["exact"] += 1
            continue
        if (dn, zn) in now_names:
            counts["requalified"] += 1
            continue
        mine_ids, mine_names = by_drug_ids.get(dn, set()), by_drug_names.get(dn, set())
        if not mine_ids and not mine_names:
            counts["drug_absent"] += 1
            continue
        if ancestors(zid, parents) & mine_ids:
            counts["ancestor"] += 1
        elif any(zid in ancestors(m, parents) for m in list(mine_ids)[:80]):
            counts["descendant"] += 1
        elif any(zn and len(x) > 4 and (zn in x or x in zn) for x in mine_names):
            counts["wording"] += 1
            if len(examples) < 6:
                examples.append(("wording", r["drug"], r["dis"]))
        else:
            counts["absent"] += 1
    return counts, examples


LABELS = {
    "exact": "exact — same drug id and disease id",
    "requalified": "re-qualified — same pair, at least one different id",
    "ancestor": "we hold an ANCESTOR (v1 downfilled a subtype — not a loss, §5.7)",
    "descendant": "we hold a DESCENDANT (we are more specific — not a loss)",
    "wording": "same disease, different wording or granularity",
    "absent": "genuinely not asserted",
    "drug_absent": "nothing asserted for that drug at all",
}
#: Classes that mean the two releases agree about this drug and disease, even though a
#: strict join does not match them.
RELATED = ("ancestor", "descendant", "wording")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ref", default="main", help="git ref holding v1's pair lists")
    ap.add_argument("--products", default="products", type=Path)
    args = ap.parse_args()

    parents = mondo_parents()
    # Which Mondo: the ancestor/descendant classes below are only comparable between runs
    # if the hierarchy is, and the pin (#94) is what makes that checkable.
    from medic.grounding.lexical.sources import pinned_ontologies, release_in_file
    print(f"Mondo subclass edges: {sum(len(v) for v in parents.values()):,} "
          f"(release {release_in_file(MONDO) or 'unknown'}, "
          f"pinned {pinned_ontologies()['mondo'].version})\n")

    for kind, fname in (("indication", "indication_list.yaml"),
                        ("contraindication", "contraindication_list.yaml")):
        v1 = load_v1(kind, args.ref)
        now = load_now(args.products / fname)
        counts, examples = classify(v1, now, parents)
        strict = counts["exact"] + counts["requalified"]
        related = sum(counts[k] for k in RELATED)

        print(f"=== {kind}s: v1 {len(v1):,} | now {len(now):,} ===")
        for key in ("exact", "requalified", *RELATED, "absent", "drug_absent"):
            if counts[key]:
                print(f"  {counts[key]:6,}  ({100*counts[key]/len(v1):4.1f}%)  {LABELS[key]}")
        print(f"  {'-'*58}")
        print(f"  strict retention        {strict:6,}/{len(v1):,}"
              f"  ({100*strict/len(v1):.1f}%)")
        print(f"  hierarchy-aware         {strict+related:6,}/{len(v1):,}"
              f"  ({100*(strict+related)/len(v1):.1f}%)")
        if examples:
            print("  wording examples:")
            for _k, drug, dis in examples[:4]:
                print(f"     {drug[:26]:28s} -> {dis[:34]}")
        print()

    print("A strict join answers 'can a consumer look this exact pair up'. The "
          "hierarchy-aware\nnumber answers 'do the two releases agree about this drug and "
          "disease'. Quoting only\nthe first overstates the regression by ~13 points.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

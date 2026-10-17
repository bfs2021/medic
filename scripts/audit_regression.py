#!/usr/bin/env python3
"""Score the current export against the external label audit (Phase 4, #89/#91/#98).

The audit is 300 random MeDIC indication edges judged blind against independently fetched
FDA/EMA text by two reviewers plus a tie-breaker, with a verdict per edge: ``TARGET`` for
correct, or one of the ``FP*`` classes for a specific error. That makes it the only
ground truth available for *both* directions at once — whether an error was removed, and
whether a correct edge survived. Every other metric in this repo can only see one side.

Usage::

    python scripts/audit_regression.py --audit path/to/fp_audit.json

The audit file is **not** committed: it is produced by and licensed as part of
kevinschaper/drug-disease-compare (``src/data/fp_audit.json``), so it is fetched rather
than vendored. Without it this script exits 2 and says so, instead of pretending to pass.

Matching is by id **and** by normalised label, and both are needed. An id-only join reports
a drug whose primary id moved from ``UNII:…`` to ``CHEBI:…`` as a lost edge even though the
edge is still there — that alone was the difference between an apparent 41% loss of correct
edges and the real 25%. A label-only join would miss a correct re-grounding to a different
id for the same term. An edge counts as kept if either join finds it.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

EDGES = Path("exports/medic_edges.jsonl")
NODES = Path("exports/medic_nodes.jsonl")


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _load_export() -> tuple[set, set]:
    nodes = {n["id"]: (n.get("name") or "")
             for n in (json.loads(line) for line in NODES.open())}
    by_id, by_label = set(), set()
    for line in EDGES.open():
        e = json.loads(line)
        by_id.add((e.get("subject"), e.get("object")))
        by_label.add((_norm(nodes.get(e.get("subject"))),
                      _norm(nodes.get(e.get("object")))))
    return by_id, by_label


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--audit", required=True, type=Path,
                    help="fp_audit.json from kevinschaper/drug-disease-compare")
    ap.add_argument("--release", default="new",
                    help="which audited release to score against (default: new)")
    ap.add_argument("--show-lost", type=int, default=0,
                    help="print this many lost TARGET edges, for diagnosis")
    args = ap.parse_args()

    if not args.audit.exists():
        print(f"audit file not found: {args.audit}\n"
              "Fetch it with:\n"
              "  gh api repos/kevinschaper/drug-disease-compare/contents/src/data/"
              "fp_audit.json --jq .content | base64 -d > fp_audit.json",
              file=sys.stderr)
        return 2
    if not (EDGES.exists() and NODES.exists()):
        print("exports/ not built; run the KGX export first", file=sys.stderr)
        return 2

    audited = [e for e in json.load(args.audit.open())["edges"]
               if e.get("source") == "medic" and e.get("release") == args.release]
    by_id, by_label = _load_export()

    counts: collections.Counter = collections.Counter()
    lost: list[dict] = []
    for a in audited:
        correct = a.get("verdict") == "TARGET"
        kept = ((a["drug"], a["disease"]) in by_id
                or (_norm(a["drug_label"]), _norm(a["disease_label"])) in by_label)
        counts[("TARGET" if correct else "FP", "kept" if kept else "gone")] += 1
        if correct and not kept:
            lost.append(a)

    targets = counts[("TARGET", "kept")] + counts[("TARGET", "gone")]
    fps = counts[("FP", "kept")] + counts[("FP", "gone")]
    survivors = counts[("TARGET", "kept")] + counts[("FP", "kept")]

    print(f"audited '{args.release}' medic edges: {len(audited)}")
    print(f"  correct kept : {counts[('TARGET', 'kept')]}/{targets} "
          f"({100 * counts[('TARGET', 'kept')] / max(1, targets):.1f}%)")
    print(f"  wrong removed: {counts[('FP', 'gone')]}/{fps} "
          f"({100 * counts[('FP', 'gone')] / max(1, fps):.1f}%)")
    if survivors:
        print(f"  error rate among survivors: "
              f"{100 * counts[('FP', 'kept')] / survivors:.1f}% "
              f"(audit measured 25.6% on the build it sampled)")

    # Both directions matter. Removing errors by dropping everything is not an improvement,
    # which is exactly what a precision-only metric would have called a success.
    if args.show_lost and lost:
        print(f"\nlost correct edges (first {args.show_lost}):")
        for a in lost[:args.show_lost]:
            print(f"  {a['drug_label'][:30]:32s} -> {a['disease_label'][:44]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

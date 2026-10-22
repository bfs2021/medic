#!/usr/bin/env python3
"""Score the pipeline's combination-label claims against the curated gold standard.

    just gold-combination-audit          # needs products/ built

**Why this exists.** PRs #69-#87 hand-curated 1,133 claims over 103 DailyMed SPLs and 102
ingredient sets (`curation/combinations/`), recovered here from those branches' history.
`plan.md` said they "should be wired in as tests **before** the prompt changes". They were
not, so #88 (combination attribution), #89 (context) and #97 all shipped without the
regression set that would have scored them. That is the largest process failure of this
stretch, and this script is what closes it: it turns "the mechanism works" into a number.

**What it measures.** For each curated claim — a (setid, relation, disease id) triple — does
the build assert the same relation for that setid, and does it attach it to the right
ingredients? Two numbers, because they fail for different reasons:

* **recall of the claim** — did we extract it at all. A miss is an extraction or acquisition
  problem.
* **attribution** — a curated claim is *product-level* unless it names `attributed_to`, and a
  product-level claim belongs to **every** ingredient of the label, while an attributed one
  belongs only to the named moiety. Crediting a product-level claim to one ingredient and
  crediting an attributed claim to all of them are both wrong, in opposite directions, so
  they are counted separately (`over_attributed` / `under_attributed`).

**What it deliberately does not do.** It does not score the disease *grounding* when the
curators recorded a `binding` other than `EXACT`: 'HIV-1 infection' bound to
MONDO:0005109 'HIV infectious disease' as BROADER is a curator's considered compromise about
a gap in Mondo, not a target for us to reproduce. Those claims are counted under
`inexact_binding` and excluded from the match rate rather than scored against a term the
curator themselves flagged as approximate.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

import yaml

GOLD_DIR = Path("curation/combinations")

#: A DailyMed setid as it appears in evidence references and document URLs.
_SETID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

#: Curated relation -> the `relationship_type` the products use.
RELATION_MAP = {
    "TREATS": "INDICATION",
    "AMELIORATES_CONDITION": "INDICATION",
    "PREVENTATIVE_FOR_CONDITION": "INDICATION",
    "CONTRAINDICATED_IN": "CONTRAINDICATION",
}


def _term_id(obj: dict) -> str:
    return str(((obj or {}).get("term") or {}).get("id") or "").strip()


def _binding(obj: dict) -> str:
    return str((obj or {}).get("binding") or "").strip().upper()


def load_gold(gold_dir: Path = GOLD_DIR) -> list[dict]:
    """One row per curated claim, flattened out of the per-combination files."""
    rows: list[dict] = []
    for path in sorted(gold_dir.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text()) or {}
        ingredients = [str(i.get("id") or "") for i in (doc.get("ingredients") or [])]
        for product in doc.get("products") or []:
            setid = str(((product.get("document") or {}).get("setid")) or "").strip().lower()
            for claim in product.get("claims") or []:
                relation = RELATION_MAP.get(str(claim.get("relation") or ""))
                if not relation or not setid:
                    continue
                # `attributed_to` names the moiety that owns the claim; absent means the
                # sentence's subject was the product, so every ingredient owns it.
                attributed = claim.get("attributed_to")
                if isinstance(attributed, dict):
                    attributed = [attributed]
                owners = [str((a or {}).get("id") or "") for a in (attributed or [])]
                rows.append({
                    "file": path.name,
                    "setid": setid,
                    "relation": relation,
                    "disease_id": _term_id(claim.get("object")),
                    "binding": _binding(claim.get("object")),
                    "ingredients": [i for i in ingredients if i],
                    "owners": [o for o in owners if o],
                })
    return rows


def load_build(products: Path) -> dict[tuple[str, str, str], set[str]]:
    """``(setid, relation, disease_id) -> {drug ids asserting it}`` from the products.

    Read through ``product_view``, so this follows the association shape rather than the
    flat fields the provenance re-model removed.
    """
    from medic import product_view as pv

    out: dict[tuple[str, str, str], set[str]] = collections.defaultdict(set)
    for fname, relation in (("indication_list.yaml", "INDICATION"),
                            ("contraindication_list.yaml", "CONTRAINDICATION")):
        path = products / fname
        if not path.exists():
            continue
        data = yaml.safe_load(path.read_text()) or {}
        for assoc in data.get("associations") or []:
            drug_id = pv.assoc_drug_id(assoc)
            disease_id = pv.assoc_disease_id(assoc)
            if not drug_id or not disease_id:
                continue
            for ev in pv.assoc_evidence(assoc):
                # The setid is a UUID inside the evidence's reference and document URL
                # (".../lookup.cfm?setid=<uuid>", ".../downloadpdffile.cfm?setid=<uuid>").
                # Matched rather than sliced, so a change of URL shape cannot silently
                # start yielding a truncated key that matches nothing.
                blob = f"{ev.get('reference') or ''} {ev.get('source_document_url') or ''}"
                for setid in _SETID_RE.findall(blob):
                    out[(setid.lower(), relation, disease_id)].add(drug_id)
    return out


def score(gold: list[dict], build: dict[tuple[str, str, str], set[str]]) -> dict:
    counts: collections.Counter = collections.Counter()
    examples: dict[str, list] = collections.defaultdict(list)
    for row in gold:
        if not row["disease_id"]:
            counts["no_curated_term"] += 1
            continue
        if row["binding"] and row["binding"] != "EXACT":
            counts["inexact_binding"] += 1
            continue
        counts["scored"] += 1
        key = (row["setid"], row["relation"], row["disease_id"])
        ours = build.get(key, set())
        if not ours:
            counts["missed"] += 1
            if len(examples["missed"]) < 8:
                examples["missed"].append(
                    f"{row['file']}: {row['relation']} {row['disease_id']}")
            continue
        counts["found"] += 1
        expected = set(row["owners"]) if row["owners"] else set(row["ingredients"])
        if not expected:
            counts["attribution_unknown"] += 1
            continue
        if row["owners"]:
            extra = ours - expected
            if extra:
                counts["over_attributed"] += 1
                if len(examples["over_attributed"]) < 8:
                    examples["over_attributed"].append(
                        f"{row['file']}: {row['disease_id']} also credited to "
                        f"{sorted(extra)}")
            else:
                counts["attribution_ok"] += 1
        else:
            missing = expected - ours
            if missing:
                counts["under_attributed"] += 1
                if len(examples["under_attributed"]) < 8:
                    examples["under_attributed"].append(
                        f"{row['file']}: {row['disease_id']} not credited to "
                        f"{sorted(missing)}")
            else:
                counts["attribution_ok"] += 1
    return {"counts": dict(counts), "examples": {k: v for k, v in examples.items()}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--products", default="products", type=Path)
    ap.add_argument("--gold", default=GOLD_DIR, type=Path)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    gold = load_gold(args.gold)
    if not gold:
        raise SystemExit(f"no curated claims under {args.gold}")
    build = load_build(args.products)
    result = score(gold, build)
    c = result["counts"]

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    scored = c.get("scored", 0)
    print(f"curated claims: {len(gold):,}  over {len({r['setid'] for r in gold})} SPLs "
          f"and {len({r['file'] for r in gold})} ingredient sets")
    print(f"  excluded: {c.get('inexact_binding', 0):,} inexact binding, "
          f"{c.get('no_curated_term', 0):,} no curated term")
    print(f"  build triples read: {len(build):,}\n")
    if not scored:
        print("nothing scored")
        return 0
    print(f"scored: {scored:,}")
    print(f"  found       {c.get('found', 0):6,}  ({100 * c.get('found', 0) / scored:4.1f}%)")
    print(f"  missed      {c.get('missed', 0):6,}  ({100 * c.get('missed', 0) / scored:4.1f}%)")
    found = c.get("found", 0)
    if found:
        for key, label in (("attribution_ok", "attributed correctly"),
                           ("over_attributed", "credited to a moiety that does not own it"),
                           ("under_attributed", "product-level claim missing from a moiety"),
                           ("attribution_unknown", "no ingredient list to compare")):
            n = c.get(key, 0)
            if n:
                print(f"    {n:6,}  ({100 * n / found:4.1f}% of found)  {label}")
    for key, rows in result["examples"].items():
        if rows:
            print(f"\n  {key}:")
            for line in rows[:5]:
                print(f"     {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

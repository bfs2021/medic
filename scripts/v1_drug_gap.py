"""How many of a reference release's drugs are genuinely absent, vs merely re-identified?

`just compare-release` reports the drug curie overlap, which against v1.0.0 is 41.2% and
reads like a 59% coverage loss. It is not. v1 identified many drugs by UNII, RXCUI,
PUBCHEM.COMPOUND or DRUGBANK where the current pipeline uses CHEBI, so an id-string join
counts the same drug as a miss. Effective coverage — reachable by id *or* by a name we know
the drug under — is 61.8%.

Both numbers are true and they answer different questions. This script exists because the
id-only number keeps getting quoted as a coverage loss, including by me.

Run after a build:

    just v1-drug-gap            # needs tmp/old_release/, populated by `just compare-release`

It prints the gap broken down by the reference release's own approval flags, because that is
what prioritises the work: of 1,484 genuinely-absent v1 drugs, 436 are India-only, 414
USA-only and 248 Russia, so the India reader and the DailyMed acquisition breadth are worth
more here than any amount of identifier mapping.
"""
import collections
import re

import pandas as pd
from pathlib import Path

OLD = Path("tmp/old_release/drug_list_flexible.csv")
NEW = Path("exports/drug_list_flexible.csv")
for path, hint in ((OLD, "run `just compare-release` first to download it"),
                   (NEW, "run `just export-legacy` first to build it")):
    if not path.exists():
        raise SystemExit(f"missing {path} - {hint}")

old = pd.read_csv(OLD, dtype=str).fillna("")
new = pd.read_csv(NEW, dtype=str).fillna("")


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s).lower())


SPLIT = re.compile(r"[;,|\[\]'\"]+")

# Everything the current export knows a drug by: its label, its source ingredient strings,
# its combination components.
known: set[str] = set()
for _, r in new.iterrows():
    known.add(norm(r.get("curie_label", "")))
    for col in ("source_ingredients", "combination_therapy_ingredients", "synonyms"):
        val = r.get(col, "")
        if val:
            for part in SPLIT.split(str(val)):
                if part.strip():
                    known.add(norm(part))
known.discard("")

new_curies = set(new["curie"])
missing = [r for _, r in old.iterrows() if r["curie"] not in new_curies]

matched = [r for r in missing if norm(r.get("curie_label", "")) in known]
rest = [r for r in missing if norm(r.get("curie_label", "")) not in known]

print(f"v1 curies: {len(set(old['curie']))}   current: {len(new_curies)}")
print(f"overlap by id: {len(set(old['curie']) & new_curies)} "
      f"({100 * len(set(old['curie']) & new_curies) / len(set(old['curie'])):.1f}%)")
print()
print(f"absent by id: {len(missing)}")
print(f"  same drug, different id (name known to us): {len(matched)} "
      f"({100 * len(matched) / max(1, len(missing)):.1f}%)")
print(f"  genuinely not in the current export:        {len(rest)} "
      f"({100 * len(rest) / max(1, len(missing)):.1f}%)")
print()
print("  prefixes of the genuinely-absent:",
      dict(collections.Counter(str(r["curie"]).split(":")[0] for r in rest).most_common(8)))
print()
print("  a sample of genuinely-absent labels:")
for r in rest[:14]:
    print(f"     {str(r['curie']):26s} {str(r.get('curie_label', ''))[:44]}")

# Effective coverage: v1 drugs we can still speak about at all.
eff = len(set(old["curie"]) & new_curies) + len(matched)
print()
print(f"effective v1 drug coverage: {eff}/{len(set(old['curie']))} "
      f"({100 * eff / len(set(old['curie'])):.1f}%)")


# Which sources the absent drugs came from. This is the part that prioritises work.
approval_cols = [c for c in old.columns if c.startswith("approved_")]
if approval_cols:
    buckets: collections.Counter = collections.Counter()
    for r in rest:
        flags = [c.replace("approved_", "") for c in approval_cols
                 if str(r.get(c, "")).strip().lower() in ("true", "1", "1.0")]
        buckets[",".join(sorted(flags)) or "(no approval flag)"] += 1
    print()
    print("  genuinely-absent drugs by the reference release's approval flags:")
    for key, n in buckets.most_common(12):
        print(f"     {n:5d}  {key}")

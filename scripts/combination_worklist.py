"""Build the combination curation worklist from the pipeline's DailyMed rows.

Every DailyMed label whose rows carry more than one drug id is a combination product the
pipeline fanned out ingredient by ingredient. The worklist lists them with what the fan-out
costs today, so curation starts where it removes the most wrong rows. Labels with more than
``MAX_PILOT_INGREDIENTS`` ingredients (parenteral nutrition, multivalent vaccines, homeopathic
mixtures) are flagged out of the pilot: they need a naming decision, not attribution.

    uv run python scripts/combination_worklist.py   # writes curation/combination_worklist.tsv
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

import yaml

KB = Path("kb/indications/dailymed")
OUT = Path("curation/combination_worklist.tsv")
MAX_PILOT_INGREDIENTS = 4


def main() -> None:
    labels: dict[str, dict] = defaultdict(lambda: {
        "drugs": {}, "indication_rows": 0, "contraindication_rows": 0, "text": ""})
    for name, key in (("indications", "indication_rows"),
                      ("contraindications", "contraindication_rows")):
        for rec in yaml.safe_load((KB / f"{name}.yaml").read_text()) or []:
            setid = rec.get("set_id")
            if not setid:
                continue
            entry = labels[setid]
            entry["drugs"][rec.get("final_normalized_drug_id")] = rec.get(
                "final_normalized_drug_label")
            entry[key] += 1
            entry["text"] = entry["text"] or (rec.get("indications_text") or "")[:160]

    rows = []
    for setid, e in labels.items():
        if len(e["drugs"]) < 2:
            continue
        n = len(e["drugs"])
        rows.append({
            "setid": setid,
            "ingredients": " | ".join(sorted(v or k for k, v in e["drugs"].items())),
            "n_ingredients": n,
            "pipeline_rows": e["indication_rows"] + e["contraindication_rows"],
            "indication_rows": e["indication_rows"],
            "contraindication_rows": e["contraindication_rows"],
            "pilot": "yes" if n <= MAX_PILOT_INGREDIENTS else "no",
            "status": "todo",
            "indications_excerpt": " ".join(e["text"].split()),
        })
    rows.sort(key=lambda r: (r["pilot"] != "yes", -r["pipeline_rows"], r["setid"]))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]), delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)
    pilot = sum(r["pilot"] == "yes" for r in rows)
    print(f"wrote {OUT}: {len(rows)} combination labels, {pilot} in the pilot")


if __name__ == "__main__":
    main()

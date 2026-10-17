"""Compare new pipeline output against a reference release.

Downloads the reference release files (if not cached), then produces
a detailed comparison report for every file.

Usage:
    uv run python scripts/compare_release.py
    uv run python scripts/compare_release.py --release v1.0.0 --output docs/v1_comparison_report.md

To add new files to the comparison, add entries to RELEASE_FILES below.
"""

import argparse
import ast
import logging
import subprocess
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

# Each entry: (release_filename, local_export_path, key_column, format)
# To add new files, just append to this list.
RELEASE_FILES = [
    ("drug_list_flexible.csv", "exports/drug_list_flexible.csv", "curie", "csv"),
    ("drug_list_stringent.csv", "exports/drug_list_stringent.csv", "curie", "csv"),
    ("orangebook.xlsx", "exports/orangebook.xlsx", "source_ingredients", "excel"),
    ("purplebook.xlsx", "exports/purplebook.xlsx", "source_ingredients", "excel"),
    ("ema.xlsx", "exports/ema.xlsx", "source_ingredients", "excel"),
    ("pmda.xlsx", "exports/pmda.xlsx", "source_ingredients", "excel"),
    ("russia.csv", "exports/russia.csv", "source_ingredients", "csv"),
    ("india.csv", "exports/india.csv", "source_ingredients", "csv"),
]

REPO = "marcello-deluca/medic"


def download_release(release: str, cache_dir: Path) -> None:
    """Download release assets via gh CLI."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    for filename, _, _, _ in RELEASE_FILES:
        dest = cache_dir / filename
        if dest.exists():
            continue
        logger.info("Downloading %s from %s...", filename, release)
        subprocess.run(
            ["gh", "release", "download", release, "--repo", REPO,
             "--pattern", filename, "--dir", str(cache_dir), "--clobber"],
            check=True, capture_output=True,
        )


def to_bool(v):
    if pd.isna(v) or v == "":
        return False
    s = str(v).strip().lower()
    return s in ("true", "1", "1.0")


def normalize_id_set(v):
    """Normalize alternate_ids/source_ingredients to a comparable set."""
    if pd.isna(v) or not v:
        return set()
    s = str(v).strip()
    if not s or s == "nan":
        return set()
    if s.startswith("["):
        try:
            items = ast.literal_eval(s)
            return {str(x).strip() for x in items if str(x).strip()}
        except (ValueError, SyntaxError):
            pass
    return {x.strip() for x in s.split("|") if x.strip()}


def read_file(path: Path, fmt: str) -> pd.DataFrame:
    if fmt == "excel":
        return pd.read_excel(path, dtype=str)
    return pd.read_csv(path, dtype=str, encoding="latin1")


def compare_file(old_path: Path, new_path: Path, key_col: str, name: str) -> str:
    """Compare two files and return a markdown report section."""
    lines = []
    lines.append(f"## {name}\n")

    if not old_path.exists():
        lines.append(f"Reference file not found: `{old_path}`\n")
        return "\n".join(lines)
    if not new_path.exists():
        lines.append(f"New file not found: `{new_path}`\n")
        return "\n".join(lines)

    old = read_file(old_path, "excel" if old_path.suffix in (".xlsx", ".xls") else "csv")
    new = read_file(new_path, "excel" if new_path.suffix in (".xlsx", ".xls") else "csv")

    lines.append("| Metric | Reference | New |")
    lines.append("|--------|-----------|-----|")
    lines.append(f"| Rows | {len(old)} | {len(new)} |")
    lines.append(f"| Columns | {len(old.columns)} | {len(new.columns)} |")

    old_cols = set(old.columns)
    new_cols = set(new.columns)
    shared_cols = old_cols & new_cols
    missing = old_cols - new_cols
    extra = new_cols - old_cols
    lines.append(f"| Shared columns | {len(shared_cols)} | |")
    if missing:
        lines.append(f"| Missing columns | {', '.join(sorted(missing))} | |")
    if extra:
        lines.append(f"| Extra columns | | {', '.join(sorted(extra))} |")
    lines.append("")

    if key_col not in old.columns or key_col not in new.columns:
        lines.append(f"Key column `{key_col}` not in both files.\n")
        return "\n".join(lines)

    old_keys = set(old[key_col].dropna().astype(str))
    new_keys = set(new[key_col].dropna().astype(str))
    overlap = old_keys & new_keys
    pct = len(overlap) / len(old_keys) * 100 if old_keys else 0

    lines.append(f"### Row overlap (by `{key_col}`)\n")
    lines.append("| | Count | % of reference |")
    lines.append("|--|-------|----------------|")
    lines.append(f"| Reference | {len(old_keys)} | 100% |")
    lines.append(f"| New | {len(new_keys)} | |")
    lines.append(f"| **Overlap** | **{len(overlap)}** | **{pct:.1f}%** |")
    lines.append(f"| Only in reference | {len(old_keys - new_keys)} | |")
    lines.append(f"| Only in new | {len(new_keys - old_keys)} | |")
    lines.append("")

    if not overlap or len(shared_cols) <= 1:
        return "\n".join(lines)

    # Field-by-field comparison.
    #
    # Deduplicate on the key first. `.loc[k, col]` returns a *Series* rather than a scalar
    # when the key occurs more than once, and the field comparison below then raised
    # "The truth value of a Series is ambiguous" and took the whole report with it. A
    # duplicated key also makes "does field X match" ill-posed, so the row count kept is
    # reported rather than silently resolved.
    old_dupes = int(old[key_col].astype(str).duplicated().sum())
    new_dupes = int(new[key_col].astype(str).duplicated().sum())
    if old_dupes or new_dupes:
        lines.append(
            f"> Note: {old_dupes} duplicate `{key_col}` row(s) in the reference and "
            f"{new_dupes} in the new export; the first row of each key is compared.\n"
        )
    old_idx = old.drop_duplicates(subset=[key_col]).set_index(key_col)
    new_idx = new.drop_duplicates(subset=[key_col]).set_index(key_col)

    bool_cols = {c for c in shared_cols if c.startswith(("approved_", "is_"))}
    set_cols = {"alternate_ids", "atc_codes", "source_ingredients",
                "combination_therapy_ingredients", "combination_therapy_ingredients_curies"}

    lines.append("### Field comparison (overlapping rows)\n")
    lines.append("| Field | Match | % | Notes |")
    lines.append("|-------|-------|---|-------|")

    for col in sorted(shared_cols - {key_col}):
        matches = 0
        for k in overlap:
            rv = old_idx.loc[k, col] if k in old_idx.index else ""
            nv = new_idx.loc[k, col] if k in new_idx.index else ""
            if col in bool_cols:
                if to_bool(rv) == to_bool(nv):
                    matches += 1
            elif col in set_cols:
                if normalize_id_set(rv) == normalize_id_set(nv):
                    matches += 1
            else:
                os_n = str(rv).strip().lower() if pd.notna(rv) else ""
                ns_n = str(nv).strip().lower() if pd.notna(nv) else ""
                os_n = os_n.replace("discontinued", "discn")
                ns_n = ns_n.replace("discontinued", "discn")
                if os_n == ns_n:
                    matches += 1

        p = matches / len(overlap) * 100 if overlap else 0
        notes = ""
        if col in bool_cols:
            notes = "boolean"
        if p < 50:
            notes += " **LOW**" if notes else "**LOW**"
        lines.append(f"| {col} | {matches}/{len(overlap)} | {p:.1f}% | {notes} |")

    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Compare pipeline output against a release")
    parser.add_argument("--release", default="v1.0.0", help="GitHub release tag")
    parser.add_argument("--output", default="docs/v1_comparison_report.md", help="Output report path")
    parser.add_argument("--cache-dir", default="tmp/old_release", help="Cache dir for downloaded release files")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    cache_dir = Path(args.cache_dir)
    download_release(args.release, cache_dir)

    sections = []
    sections.append(f"# Pipeline Comparison Report: {args.release} vs current\n")
    sections.append("Generated by `scripts/compare_release.py`.\n")

    for release_name, local_path, key_col, fmt in RELEASE_FILES:
        old = cache_dir / release_name
        new = Path(local_path)
        sections.append(compare_file(old, new, key_col, release_name))

    report = "\n".join(sections)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report)
    print(f"Report written to {out}")


if __name__ == "__main__":
    main()

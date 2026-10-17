"""Common utilities for ingest modules."""

import logging
import os
import re
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

import httpx
import pandas as pd
import yaml

logger = logging.getLogger(__name__)


def _clean_for_yaml(obj):
    """Recursively clean strings in a data structure for YAML safety."""
    if isinstance(obj, str):
        # Remove null bytes and control characters
        return "".join(c for c in obj if c == "\n" or c == "\t" or (ord(c) >= 32))
    elif isinstance(obj, dict):
        return {k: _clean_for_yaml(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_clean_for_yaml(v) for v in obj]
    return obj


def write_drug_source_yaml(
    records: list[dict], output_dir: Path, source_name: str
) -> Path:
    """Write a list of drug source records to YAML.

    Args:
        records: List of drug source record dicts.
        output_dir: Directory to write to (e.g., kb/drugs/orangebook/).
        source_name: Name for the output file.

    Returns:
        Path to the written file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{source_name}.yaml"

    # Clean all string values to remove null bytes and control chars
    cleaned = _clean_for_yaml(records)

    # Serialize to string first, then write (avoids file handle issues)
    content = yaml.dump(cleaned, default_flow_style=False, allow_unicode=True)
    # Final cleanup: remove any remaining null bytes
    content = content.replace("\x00", "")
    with open(output_path, "w") as f:
        f.write(content)

    logger.info("Wrote %d records to %s", len(records), output_path)
    return output_path


def should_skip_expensive_calls() -> bool:
    """Check if expensive API calls should be skipped."""
    return os.environ.get("MEDIC_SKIP_EXPENSIVE_CALLS", "").strip() in ("1", "true", "yes")


def load_source_urls() -> dict:
    """Load source URLs from conf/source_urls.yaml."""
    config_path = Path("conf/source_urls.yaml")
    if not config_path.exists():
        return {}
    with open(config_path) as f:
        return yaml.safe_load(f) or {}


def download_file(url: str, dest_path: Path, force: bool = False, timeout: float = 120.0) -> Path:
    """Download a file from URL with retry logic.

    Skips download if file already exists (unless force=True).
    Retries up to 3 times with exponential backoff.
    """
    if dest_path.exists() and not force:
        logger.info("File already exists: %s (use --force-download to re-fetch)", dest_path)
        return dest_path

    dest_path.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(3):
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                response = client.get(url)
                response.raise_for_status()
                dest_path.write_bytes(response.content)
                logger.info("Downloaded %s -> %s", url, dest_path)
                return dest_path
        except Exception as e:
            if attempt < 2:
                wait = 2 ** attempt
                logger.warning("Download attempt %d failed: %s. Retrying in %ds...", attempt + 1, e, wait)
                import time
                time.sleep(wait)
            else:
                raise RuntimeError(f"Failed to download {url} after 3 attempts: {e}") from e
    return dest_path  # unreachable but satisfies type checker


def download_and_extract_zip(url: str, dest_dir: Path, target_filename: str, force: bool = False) -> Path:
    """Download a ZIP file and extract a specific file from it."""
    target_path = dest_dir / target_filename
    if target_path.exists() and not force:
        logger.info("File already exists: %s", target_path)
        return target_path

    dest_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        download_file(url, tmp_path, force=True)
        with zipfile.ZipFile(tmp_path) as zf:
            # Find the target file in the zip (may be in a subdirectory)
            for name in zf.namelist():
                if name.endswith(target_filename) or Path(name).name == target_filename:
                    with zf.open(name) as src, open(target_path, "wb") as dst:
                        dst.write(src.read())
                    logger.info("Extracted %s from ZIP -> %s", name, target_path)
                    return target_path
            raise FileNotFoundError(f"{target_filename} not found in ZIP from {url}")
    finally:
        tmp_path.unlink(missing_ok=True)


def standardize_columns(df: pd.DataFrame, mapping: dict[str, str]) -> pd.DataFrame:
    """Rename DataFrame columns per a mapping dict. Unmapped columns are kept."""
    return df.rename(columns=mapping)


def deduplicate_with_join(df: pd.DataFrame, key_cols: list[str], join_delimiter: str = " | ") -> pd.DataFrame:
    """Deduplicate DataFrame by key columns, joining non-key columns."""
    non_key = [c for c in df.columns if c not in key_cols]

    def combine(group):
        # group has key_cols excluded (include_groups=False), they're in the index
        row = {}
        for col in non_key:
            vals = group[col].dropna().astype(str).unique()
            row[col] = join_delimiter.join(vals) if len(vals) > 1 else (vals[0] if len(vals) == 1 else "")
        return pd.Series(row)

    result = df.groupby(key_cols, sort=False).apply(combine, include_groups=False).reset_index()
    # Drop the extra numeric index column that may be added
    if "level_1" in result.columns:
        result = result.drop(columns=["level_1"])
    return result


def reformat_date(date_str: str | None) -> str:
    """Normalize a date string to YYYYMMDD format.

    Handles: "March 14, 2025", "14/03/2025", "2025-03-14", "20250314", "Approved Prior to Jan 1, 1982".
    Returns empty string for unparseable or empty input.
    """
    if not date_str or pd.isna(date_str):
        return ""
    s = str(date_str).strip()
    if not s:
        return ""

    # Already YYYYMMDD
    if re.match(r"^\d{8}$", s):
        return s

    # Handle "Approved Prior to Jan 1, 1982"
    if "prior to" in s.lower():
        return "19820101"

    # Try various date formats
    formats = [
        "%B %d, %Y",     # March 14, 2025
        "%b %d, %Y",     # Mar 14, 2025
        "%d/%m/%Y",      # 14/03/2025
        "%Y-%m-%d",      # 2025-03-14
        "%m/%d/%Y",      # 03/14/2025
        "%Y%m%d",        # 20250314 (already caught above, but just in case)
        "%d-%b-%y",      # 24-Jan-25 (Purple Book)
        "%d-%b-%Y",      # 24-Jan-2025
        "%d-%B-%Y",      # 24-January-2025
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(s, fmt)
            # Two-digit year (%y) sometimes parses 30 -> 1930. Pick latest plausible century.
            if "%y" in fmt and dt.year < 1950:
                dt = dt.replace(year=dt.year + 100)
            return dt.strftime("%Y%m%d")
        except ValueError:
            continue

    logger.warning("Could not parse date: '%s'", s)
    return ""


def write_grounding_report(report: dict, output_dir: Path, source_name: str) -> Path:
    """Write a grounding QC report to YAML."""
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "grounding_report.yaml"
    content = yaml.dump(report, default_flow_style=False, allow_unicode=True)
    with open(output_path, "w") as f:
        f.write(content)
    logger.info("Wrote grounding report to %s", output_path)
    return output_path


# --- LLM disease-name output guard ----------------------------------------
# Phrases an LLM starts a refusal / commentary with when the section holds no diseases.
# These slip past the "None" sentinel in _parse_llm_disease_list and reach the grounder.
_REFUSAL_STARTS = (
    "these are", "this is", "the text", "no diseases",
    "i cannot", "i can't", "n/a", "not applicable",
    "note:", "warning:",
)

#: Real disease names are short. Anything longer is LLM prose that leaked.
_MAX_DISEASE_NAME_WORDS_WITH_PERIOD = 6
_MAX_DISEASE_NAME_CHARS = 120

#: A target that names the restriction instead of a condition. The structured
#: contraindication prompt (#97) asks for one record per contraindication, and a clause that
#: restricts only a population or only a co-administration has no condition to put in
#: ``target`` — so the model invents one: "contraindication in pediatric population",
#: "contraindication with concurrent MAOI use". The prompt now says to emit no record for
#: those, but this stays as the deterministic half, because the failure is silent without it:
#: the disease vocabulary holds 471 strings containing this token (SNOMED procedure concepts
#: such as "Procedure contraindicated"), so a fabricated target does not fail grounding
#: loudly — it matches one of those and ships as a plausible-looking edge. No legitimate
#: drug-contraindication target is itself named after contraindication.
_META_RESTRICTION_TOKEN = "contraindicat"


def looks_like_disease_name(name: str) -> bool:
    """Defensive filter for LLM disease-extraction output.

    The extraction prompts are tuned per section and occasionally get a refusal sentence
    back ("These are contraindications, not indications…") that bypasses the ``None``
    sentinel and gets grounded as if it were a disease. This rejects strings that are
    clearly not disease names: too long, sentence-shaped, or refusal-prefixed. It is
    conservative — real names like "primary biliary cholangitis" pass.

    Lifted out of ``ema/__main__.py``, where it guarded only the EU path; PMDA and India
    had no second line of defence at all (issue #59). It now runs inside
    ``_parse_llm_disease_list``, the one parser every source's extraction passes through.

    One rule changed in the move. The original rejected **any** string containing a period,
    on the grounds that real disease names rarely have one. Measured against the shipped
    knowledge base that rule's entire effect was to delete four correct
    ``H. pylori infection`` rows, so it is now "a period *and* prose-length", which still
    catches the sentences it was written for.
    """
    name = (name or "").strip()
    if not name:
        return False
    if len(name) > _MAX_DISEASE_NAME_CHARS:
        return False
    lower = name.lower()
    if any(lower.startswith(p) for p in _REFUSAL_STARTS):
        return False
    if _META_RESTRICTION_TOKEN in lower:
        return False
    if "." in name and len(name.split()) > _MAX_DISEASE_NAME_WORDS_WITH_PERIOD:
        return False
    return True

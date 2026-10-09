"""Fetch and read one DailyMed label for curation.

``uv run python -m medic.curation.label <setid>`` prints what a curator needs to write a
combination file: the label's title and version, its active ingredients resolved to CURIEs,
the computed combination id / name / file name, and the Indications and Contraindications
text split into the same typed spans the pipeline uses (so a Limitations of Use passage is
visibly marked as one). ``--skeleton`` prints a starting YAML file.

The XML is cached under ``data/raw/dailymed/`` (gitignored), the directory the DailyMed
ingest already reads.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml

from medic.curation.identity import (
    IngredientResolver,
    combination_id,
    combination_name,
    combination_slug,
)
from medic.ingest.dailymed.__main__ import (
    LOINC_CONTRAINDICATIONS,
    LOINC_INDICATIONS,
    NS,
    _extract_ingredients_from_root,
    _extract_section_chunks_from_root,
)
from medic.ingest.dailymed.acquire import SPL_XML_URL
from medic.spans import spans_for_source

LABEL_CACHE_DIR = Path("data/raw/dailymed")
COMBINATIONS_DIR = Path("kb/combinations")
DAILYMED_SETID_URL = "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={setid}"

SECTION_LOINC = {"INDICATIONS": LOINC_INDICATIONS, "CONTRAINDICATIONS": LOINC_CONTRAINDICATIONS}


@dataclass
class LabelDocument:
    """The parts of one SPL a combination file is checked against."""

    setid: str
    version: int | None
    title: str
    active_moieties: list[str]
    #: The first manufactured product's name as the SPL gives it, e.g. "YOSPRALA".
    product_name: str = ""
    #: Section name -> the section's <title>/<text> elements, in document order.
    sections: dict[str, list[str]] = field(default_factory=dict)

    def section_text(self, section: str) -> str:
        return " ".join(self.sections.get(section, []))

    def spans(self, section: str) -> list[dict]:
        """The section split into typed spans, element by element (see ``medic.spans``)."""
        return spans_for_source(
            "DAILYMED", self.section_text(section), document=self.setid,
            section_code=SECTION_LOINC[section], parts=self.sections.get(section))


def parse_label(xml_bytes: bytes) -> LabelDocument:
    root = ET.fromstring(xml_bytes)
    set_id_elem = root.find("v3:setId", NS)
    setid = ""
    if set_id_elem is not None:
        setid = set_id_elem.get("root", "") or set_id_elem.get("extension", "")
    version_elem = root.find("v3:versionNumber", NS)
    version = None
    if version_elem is not None and (version_elem.get("value") or "").isdigit():
        version = int(version_elem.get("value"))
    title_elem = root.find("v3:title", NS)
    title = " ".join("".join(title_elem.itertext()).split()) if title_elem is not None else ""
    sections = {
        name: _extract_section_chunks_from_root(root, loinc)
        for name, loinc in SECTION_LOINC.items()
    }
    name_elem = root.find(".//v3:manufacturedProduct/v3:manufacturedProduct/v3:name", NS)
    product_name = " ".join("".join(name_elem.itertext()).split()) if name_elem is not None else ""
    return LabelDocument(
        setid=setid, version=version, title=title,
        active_moieties=_extract_ingredients_from_root(root), sections=sections,
        product_name=product_name)


def label_path(setid: str, cache_dir: Path = LABEL_CACHE_DIR) -> Path:
    return cache_dir / f"{setid}.xml"


def fetch_label(setid: str, *, cache_dir: Path = LABEL_CACHE_DIR, offline: bool = False,
                refresh: bool = False) -> LabelDocument:
    """The label for ``setid``, from the cache or DailyMed.

    DailyMed serves the *current* version of a setid. A curated file pins the version its
    quotes were copied from, and the validator warns when the served version has moved on.
    """
    path = label_path(setid, cache_dir)
    if refresh or not path.exists():
        if offline:
            raise FileNotFoundError(f"label {setid} is not cached at {path} (offline)")
        resp = httpx.get(SPL_XML_URL.format(setid=setid), timeout=60.0, follow_redirects=True)
        resp.raise_for_status()
        if b"<document" not in resp.content[:5000]:
            raise ValueError(f"DailyMed returned no SPL document for setid {setid}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
    return parse_label(path.read_bytes())


def _resolve_ingredients(doc: LabelDocument, resolver: IngredientResolver):
    resolved, problems = [], []
    for name in doc.active_moieties:
        ing, why = resolver.resolve(name)
        if ing:
            resolved.append((name, ing))
        else:
            problems.append(why)
    return resolved, problems


SKILL_PATH = ".claude/skills/medic-combination-curation/SKILL.md"
REPO_BLOB = "https://github.com/monarch-initiative/medic/blob/{rev}/" + SKILL_PATH


def _git_head() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                             check=True)
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "main"


def skeleton(doc: LabelDocument, resolver: IngredientResolver) -> dict:
    """A starting file: identity, ingredients, curator and the pinned document, no claims.

    The model that did the work is recorded in the history record, not here, so one fact
    lives in one place.
    """
    resolved, _ = _resolve_ingredients(doc, resolver)
    ings = sorted({(i.id, i.label) for _, i in resolved}, key=lambda t: t[1].lower())
    return {
        "id": combination_id([i for i, _ in ings]),
        "name": combination_name([lbl for _, lbl in ings]),
        "ingredients": [{"id": i, "label": lbl} for i, lbl in ings],
        "curation_status": "DRAFT",
        "curators": [{
            "curator_type": "AI_AGENT",
            "name": "MeDIC combination-curation skill",
            "curator_id": REPO_BLOB.format(rev=_git_head()),
        }],
        "products": [{
            "source": "DAILYMED",
            "jurisdiction": "USA",
            "document": {
                "setid": doc.setid,
                "version": doc.version,
                "title": doc.title,
                "url": DAILYMED_SETID_URL.format(setid=doc.setid),
            },
            "product_name": doc.product_name,
            "claims": [],
        }],
    }


def _print_summary(doc: LabelDocument, resolver: IngredientResolver) -> None:
    resolved, problems = _resolve_ingredients(doc, resolver)
    print(f"setid:   {doc.setid}")
    print(f"version: {doc.version}")
    print(f"title:   {doc.title}")
    print(f"product: {doc.product_name}")
    print("active ingredients (SPL activeMoiety -> CURIE):")
    for name, ing in resolved:
        print(f"  {name} -> {ing.id} ({ing.label})")
    for why in problems:
        print(f"  UNRESOLVED: {why}")
    labels = sorted({i.label for _, i in resolved}, key=str.lower)
    if len(resolved) >= 2 and not problems:
        print(f"combination id:   {combination_id([i.id for _, i in resolved])}")
        print(f"combination name: {combination_name(labels)}")
        print(f"file:             kb/combinations/{combination_slug(labels)}.yaml")
    elif len(resolved) < 2 and not problems:
        print("NOTE: fewer than two active ingredients — not a combination product.")
    for section in SECTION_LOINC:
        print(f"\n=== {section} ===")
        spans = doc.spans(section)
        if not spans:
            print("  (section absent)")
        for span in spans:
            print(f"[{span['role']}]\n{span['text']}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("setid")
    parser.add_argument("--skeleton", action="store_true",
                        help="print a starting YAML file instead of the summary")
    parser.add_argument("--write", action="store_true",
                        help="write the skeleton to its computed path under kb/combinations/ "
                             "(refuses to overwrite)")
    parser.add_argument("--refresh", action="store_true", help="re-download the label")
    args = parser.parse_args(argv)
    doc = fetch_label(args.setid, refresh=args.refresh)
    resolver = IngredientResolver()
    if args.write:
        record = skeleton(doc, resolver)
        labels = [i["label"] for i in record["ingredients"]]
        if len(labels) < 2:
            sys.exit("error: fewer than two resolved ingredients; not a combination file")
        out = COMBINATIONS_DIR / f"{combination_slug(labels)}.yaml"
        if out.exists():
            sys.exit(f"error: {out} already exists; add a product block to it instead")
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(record, fh, sort_keys=False, allow_unicode=True, width=100)
        print(out)
    elif args.skeleton:
        yaml.safe_dump(skeleton(doc, resolver), sys.stdout, sort_keys=False,
                       allow_unicode=True, width=100)
    else:
        _print_summary(doc, resolver)
    return 0


if __name__ == "__main__":
    sys.exit(main())

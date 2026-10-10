"""Fetch and read one DailyMed label for curation.

``uv run python -m medic.curation.label <setid>`` prints what a curator needs to write a
combination file: the label's title and version, its active ingredients resolved to CURIEs,
the computed combination id / name / file name, and the Indications and Contraindications
text split into the same typed spans the pipeline uses (so a Limitations of Use passage is
visibly marked as one). ``--skeleton`` prints a starting YAML file.

Two stores, by design:

- **Pins** — ``curation/labels/dailymed/<setid>.v<version>.yaml``, committed. The label's
  identity, active ingredients and its Indications and Contraindications text, exactly the
  parts the checks read, plus the SHA-256 of the XML they came from. Written by this helper,
  never by hand (dismech's ``references_cache`` pattern). A curated file is checked against
  the pin for the version it names, so validation needs no network and a later revision
  upstream cannot change a verdict. ~3 KB per label version.
- **Downloads** — ``cache/labels/dailymed/<setid>.v<version>.xml``, gitignored. Not
  ``data/raw/dailymed/``: that is the DailyMed ingest's input, and every label curated there
  would silently join the pipeline's input set.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import re
import subprocess
import sys
from datetime import date
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
from medic.curation.paths import enter_repo_root
from medic.ingest.dailymed.acquire import SPL_XML_URL
from medic.spans import spans_for_source

LABEL_CACHE_DIR = Path("cache/labels/dailymed")
PIN_DIR = Path("curation/labels/dailymed")
COMBINATIONS_DIR = Path("kb/combinations")
DAILYMED_SETID_URL = "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={setid}"

SECTION_LOINC = {"INDICATIONS": LOINC_INDICATIONS, "CONTRAINDICATIONS": LOINC_CONTRAINDICATIONS}
#: OTC Drug Facts "Do not use" (LOINC 50570-1): the OTC label's contraindications. Read only
#: when the label has no prescription Contraindications section.
LOINC_OTC_DO_NOT_USE = "50570-1"
#: A chunk that is nothing but a numbered "Limitations of Use" heading ("1.3 Limitations of Use").
_LIMITATION_HEADING = re.compile(r"^\s*(\d+(\.\d+)*\s+)?Limitations? of Use\s*:?\s*$", re.I)
#: A chunk that opens a new numbered subsection ("1.4 Something", "2 DOSAGE ...").
_NUMBERED_HEADING = re.compile(r"^\s*\d+(\.\d+)*\s+[A-Z]")
#: A chunk that states an indication rather than restricting one ("X is also indicated for").
_INDICATION_STATEMENT = re.compile(r"\b(?:is|are)\s+(?:also\s+)?indicated\b", re.I)
#: Longest chunk without closing punctuation still read as a heading ("Hypertension").
_HEADING_MAX_WORDS = 8


def _ends_limitation(part: str, part_spans: list[dict]) -> bool:
    """Whether ``part`` closes the paragraphs under a "Limitations of Use" heading.

    A numbered subsection, a section header, a short unpunctuated heading ("Hypertension"),
    or a sentence stating an indication all start something that is not the limitation.
    """
    text = part.strip()
    heading_like = (bool(text) and len(text.split()) <= _HEADING_MAX_WORDS
                    and text[-1] not in ".;:)")
    return (bool(_NUMBERED_HEADING.match(part)) or heading_like
            or bool(_INDICATION_STATEMENT.search(part))
            or any(s["role"] == "SECTION_HEADER" for s in part_spans))


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
        """The section split into typed spans, element by element (see ``medic.spans``).

        One correction on top of ``medic.spans``: when "Limitations of Use" is a heading of its
        own (a ``<title>`` like "1.3 Limitations of Use"), the paragraphs under it are the
        limitation. Element-by-element splitting sees only the heading as the marker and types
        those paragraphs as ordinary section text, so a quote from them would pass as a claim.
        """
        parts = self.sections.get(section) or []
        out: list[dict] = []
        in_limitation = False
        for part in parts:
            part_spans = spans_for_source(
                "DAILYMED", part, document=self.setid, section_code=SECTION_LOINC[section],
                parts=[part])
            if _LIMITATION_HEADING.match(part):
                in_limitation = True
            elif in_limitation and _ends_limitation(part, part_spans):
                in_limitation = False
            elif in_limitation:
                part_spans = [{**s, "role": "LIMITATION_STATEMENT"}
                              if s["role"] == "SECTION_TEXT" else s for s in part_spans]
            out.extend(part_spans)
        return out


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
        name: [html.unescape(c) for c in _extract_section_chunks_from_root(root, loinc)]
        for name, loinc in SECTION_LOINC.items()
    }
    if not sections["CONTRAINDICATIONS"]:
        sections["CONTRAINDICATIONS"] = [
            html.unescape(c) for c in _extract_section_chunks_from_root(root, LOINC_OTC_DO_NOT_USE)]
    name_elem = root.find(".//v3:manufacturedProduct/v3:manufacturedProduct/v3:name", NS)
    product_name = " ".join("".join(name_elem.itertext()).split()) if name_elem is not None else ""
    return LabelDocument(
        setid=setid, version=version, title=title,
        active_moieties=_extract_ingredients_from_root(root), sections=sections,
        product_name=product_name)


def label_path(setid: str, version: int, cache_dir: Path = LABEL_CACHE_DIR) -> Path:
    return cache_dir / f"{setid}.v{version}.xml"


def pin_path(setid: str, version: int, pin_dir: Path = PIN_DIR) -> Path:
    return pin_dir / f"{setid}.v{version}.yaml"


def _cached_versions(setid: str, cache_dir: Path) -> list[Path]:
    paths = cache_dir.glob(f"{setid}.v*.xml")
    return sorted(paths, key=lambda p: int(p.stem.rsplit(".v", 1)[1]))


def _pin_record(doc: LabelDocument, xml: bytes, retrieved: str) -> dict:
    return {
        "setid": doc.setid,
        "version": doc.version,
        "source_url": SPL_XML_URL.format(setid=doc.setid),
        "xml_sha256": hashlib.sha256(xml).hexdigest(),
        "retrieved": retrieved,
        "title": doc.title,
        "product_name": doc.product_name,
        "active_moieties": doc.active_moieties,
        "sections": doc.sections,
    }


def _comparable(record: dict) -> dict:
    return {k: v for k, v in record.items() if k != "retrieved"}


class PinMismatch(ValueError):
    """An existing pin disagrees with what the label XML says."""


def write_pin(doc: LabelDocument, xml: bytes, pin_dir: Path = PIN_DIR) -> Path:
    """Commit-ready record of the parts of a label version the checks read.

    Refuses to overwrite a pin whose content differs: a pin is evidence of what the label
    said, so a difference means either the pin was edited or DailyMed changed a version in
    place, and both need a human. An identical pin is left untouched (its date included).
    """
    path = pin_path(doc.setid, doc.version or 0, pin_dir)
    record = _pin_record(doc, xml, date.today().isoformat())
    if path.exists():
        existing = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if _comparable(existing) == _comparable(yaml.safe_load(yaml.safe_dump(record))):
            return path
        raise PinMismatch(f"{path} exists and differs from the label XML; investigate "
                          "rather than overwrite (see `--verify-pin`)")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(record, fh, sort_keys=False, allow_unicode=True, width=100)
    return path


def load_pin(setid: str, version: int, pin_dir: Path = PIN_DIR) -> LabelDocument | None:
    path = pin_path(setid, version, pin_dir)
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return LabelDocument(
        setid=data["setid"], version=data["version"], title=data.get("title", ""),
        active_moieties=list(data.get("active_moieties") or []),
        product_name=data.get("product_name", ""),
        sections={k: list(v or []) for k, v in (data.get("sections") or {}).items()})


def _download(setid: str, cache_dir: Path) -> tuple[LabelDocument, bytes]:
    resp = httpx.get(SPL_XML_URL.format(setid=setid), timeout=60.0, follow_redirects=True)
    resp.raise_for_status()
    if b"<document" not in resp.content[:5000]:
        raise ValueError(f"DailyMed returned no SPL document for setid {setid}")
    doc = parse_label(resp.content)
    path = label_path(setid, doc.version or 0, cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_bytes(resp.content)
    tmp.replace(path)
    return doc, resp.content


def fetch_label(setid: str, *, version: int | None = None, cache_dir: Path = LABEL_CACHE_DIR,
                pin_dir: Path = PIN_DIR, offline: bool = False) -> LabelDocument:
    """A label version for curation work.

    With ``version``: the committed pin, else the downloaded copy, else (online) whatever
    DailyMed serves now. Without: what DailyMed serves now — DailyMed only serves the current
    version, and a new curation must start from it — or, offline, the newest download.
    """
    if version is not None:
        pinned = load_pin(setid, version, pin_dir)
        if pinned is not None:
            return pinned
        cached = label_path(setid, version, cache_dir)
        if cached.exists():
            return parse_label(cached.read_bytes())
    if offline:
        cached_all = _cached_versions(setid, cache_dir)
        if not cached_all:
            raise FileNotFoundError(f"label {setid} is not cached under {cache_dir} (offline)")
        return parse_label(cached_all[-1].read_bytes())
    return _download(setid, cache_dir)[0]


def pinned_label(setid: str, version: int, pin_dir: Path = PIN_DIR) -> LabelDocument:
    """The committed pin a curated file is validated against; raises if it is missing."""
    doc = load_pin(setid, version, pin_dir)
    if doc is None:
        raise FileNotFoundError(
            f"no committed label pin {pin_path(setid, version, pin_dir)}; create it with "
            f"`uv run python -m medic.curation.label {setid} --pin` while DailyMed still "
            f"serves version {version}")
    return doc


def verify_pin(setid: str, version: int, cache_dir: Path = LABEL_CACHE_DIR,
               pin_dir: Path = PIN_DIR) -> str:
    """Recompute a pin from the label XML and compare. Returns a one-line verdict.

    Uses the downloaded copy of that version if present, else what DailyMed serves now (which
    can only verify the current version).
    """
    path = pin_path(setid, version, pin_dir)
    if not path.exists():
        return f"MISSING {path}"
    pin = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cached = label_path(setid, version, cache_dir)
    if cached.exists():
        xml = cached.read_bytes()
    else:
        doc, xml = _download(setid, cache_dir)
        if doc.version != version:
            return (f"UNVERIFIABLE {path}: DailyMed now serves v{doc.version}, and v{version} "
                    "is not downloaded")
    expected = _pin_record(parse_label(xml), xml, pin.get("retrieved", ""))
    if _comparable(yaml.safe_load(yaml.safe_dump(expected))) == _comparable(pin):
        return f"OK {path}"
    return f"MISMATCH {path}: the pin differs from the label XML (sha256 {expected['xml_sha256']})"


def pin_current(setid: str, cache_dir: Path = LABEL_CACHE_DIR,
                pin_dir: Path = PIN_DIR) -> tuple[LabelDocument, Path]:
    """Download the current version and write its pin."""
    doc, xml = _download(setid, cache_dir)
    return doc, write_pin(doc, xml, pin_dir)


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
#: A curator reference: where to read the skill, and the git blob id of its exact text. The
#: blob id identifies the content itself, so the reference survives shallow clones and
#: squash merges, which a bare commit reference did not.
SKILL_REF = REPO_BLOB + "#git-blob={blob}"


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True)


def skill_curator_id() -> str:
    """The skill's commit-pinned URL plus its git blob id, or a marker the validator rejects.

    The skill must be committed and unchanged since HEAD; otherwise no commit holds the text
    that guided the curation.
    """
    head = _git("rev-parse", "HEAD")
    blob = _git("rev-parse", f"HEAD:{SKILL_PATH}")
    clean = _git("diff", "--quiet", "HEAD", "--", SKILL_PATH)
    if head.returncode or blob.returncode or clean.returncode:
        return f"UNCOMMITTED {SKILL_PATH}: commit the skill before curating"
    return SKILL_REF.format(rev=head.stdout.strip(), blob=blob.stdout.strip())


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
            "curator_id": skill_curator_id(),
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
    parser.add_argument("--pin", action="store_true",
                        help="write the committed pin for the version DailyMed serves now "
                             "(--write does this too)")
    parser.add_argument("--offline", action="store_true",
                        help="use the newest downloaded copy instead of fetching")
    parser.add_argument("--verify-pin", type=int, metavar="VERSION",
                        help="recompute the committed pin for VERSION from the label XML")
    args = parser.parse_args(argv)
    enter_repo_root()
    if args.verify_pin is not None:
        verdict = verify_pin(args.setid, args.verify_pin)
        print(verdict)
        return 0 if verdict.startswith("OK") else 1
    resolver = IngredientResolver()
    if args.pin or args.write:
        try:
            doc, pin = pin_current(args.setid)
        except PinMismatch as exc:
            sys.exit(f"error: {exc}")
        print(f"pinned {pin}", file=sys.stderr)
    else:
        doc = fetch_label(args.setid, offline=args.offline)
    if args.pin and not args.write:
        return 0
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

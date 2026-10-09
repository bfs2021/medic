"""Deterministic checks for curated combination files (``kb/combinations/*.yaml``).

``uv run python -m medic.curation.validate [files...]`` (or ``just validate-curation``).
With no files, every file under ``kb/combinations/`` is checked. Exit status 1 when any
ERROR is found; WARNINGs are reported and do not fail.

ERRORs are facts the file must get right and a machine can settle: the schema; the id, name
and file name recomputed from the ingredients; the ingredient set equal to the label's own
structured active ingredients; every quote a verbatim substring of the right label section,
and not from a Limitations of Use passage; every descriptor's ``text`` inside its quote; a
bound term that exists, is current and carries its label, with a ``binding``; an unbound
descriptor that records its ``term_gap``; an object term no broader than the label's own
words name; attributions to real ingredients that the quote names; no curated allergy to the
product's own ingredients; no conflict with the disease grounding store; source and
jurisdiction consistent; at least one valid history record naming the model that did the work.

WARNINGs are heuristics or gaps a human should look at: a negation cue in front of the
condition, a relation the quote's wording does not suggest, an unusual ontology for a slot,
a store row in another ontology, a label revised since the file pinned its version.
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from medic.curation.identity import (
    IngredientResolver,
    combination_id,
    combination_name,
    combination_slug,
)
from medic.curation import history, terms
from medic.curation.label import LabelDocument, fetch_label
from medic.curie_utils import get_prefix
from medic.grounding.store import NO_TERM, LiteralMappingStore
from medic.normalization.store import NormalizationMappingStore
from medic.spans import NON_CLAIM_ROLES
from medic.validation.extraction_fidelity import (
    _CONTRA_NEGATION_CUES,
    _NEGATION_CUES,
    assertion_negated,
)

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema" / "combination.yaml"
COMBINATIONS_DIR = Path("kb/combinations")
DISEASE_GROUNDING_STORE = "mappings/disease_grounding.sssom.tsv"
DISEASE_NORMALIZATION_STORE = "mappings/disease_normalization.sssom.tsv"

SOURCE_JURISDICTION = {"DAILYMED": "USA"}

#: Which section each relation may be quoted from.
RELATION_SECTION = {
    "TREATS": "INDICATIONS",
    "PREVENTS": "INDICATIONS",
    "RELIEVES_SYMPTOMS": "INDICATIONS",
    "CONTRAINDICATED": "CONTRAINDICATIONS",
}

#: The ontologies each descriptor slot usually binds to. Advisory only (a WARNING): the
#: curation model favours accuracy, and fitting a term into Biolink is the export's job.
USUAL_PREFIXES = {
    "object": ("MONDO", "HP"),
    "disease_context": ("MONDO", "HP"),
    "anatomical_context": ("UBERON",),
    "life_stage": ("HsapDv", "UBERON"),
    "sex": ("PATO",),
    "object_specialization": ("CHEBI",),
    "in_combination_with": ("CHEBI",),
}

#: Ontologies whose labels and synonyms anchor the specificity check on the object.
ANCHOR_PREFIXES = ("MONDO", "HP")

_CAUSE_CUES = re.compile(r"because|due to|risk|associated with|cause|since|related to")
_OWN_PRODUCT = re.compile(r"component|excipient|ingredient|this product|the formulation", re.I)

#: Wording that suggests each non-default relation; used only for a WARNING.
_PREVENT_CUES = ("risk", "prevent", "prophyla", "reduc")
_RELIEF_CUES = ("relie", "symptom")


@dataclass
class Finding:
    level: str  # ERROR | WARNING
    file: str
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.level:7} {self.file} {self.where}: {self.message}"


def _norm_ws(text: str) -> str:
    """Compare verbatim text modulo whitespace and Unicode compatibility forms only."""
    text = unicodedata.normalize("NFKC", text or "")
    return " ".join(text.split())


def _contains(haystack: str, needle: str) -> bool:
    return bool(needle) and _norm_ws(needle) in _norm_ws(haystack)


class Checker:
    def __init__(self, *, check_terms: bool = True, offline: bool = False):
        self.check_terms = check_terms
        self.offline = offline
        self.ingredients = IngredientResolver()
        self.disease_grounding = LiteralMappingStore(DISEASE_GROUNDING_STORE, "disease")
        self.disease_grounding.load()
        self.disease_normalization = NormalizationMappingStore(
            DISEASE_NORMALIZATION_STORE, "disease")
        self.disease_normalization.load()
        self.findings: list[Finding] = []

    # -- reporting ---------------------------------------------------------------------
    def _add(self, level: str, file: Path, where: str, message: str) -> None:
        self.findings.append(Finding(level, str(file), where, message))

    def error(self, file: Path, where: str, message: str) -> None:
        self._add("ERROR", file, where, message)

    def warn(self, file: Path, where: str, message: str) -> None:
        self._add("WARNING", file, where, message)

    # -- whole file --------------------------------------------------------------------
    def check_file(self, path: Path) -> None:
        try:
            data = yaml.safe_load(path.read_text())
        except yaml.YAMLError as exc:
            self.error(path, "", f"not valid YAML: {exc}")
            return
        if not isinstance(data, dict):
            self.error(path, "", "top level must be a mapping")
            return
        if not self._check_schema(path, data):
            return
        self._check_identity(path, data)
        self._check_history(path)
        for p_idx, product in enumerate(data.get("products") or []):
            self._check_product(path, data, product, f"products[{p_idx}]")

    def _check_schema(self, path: Path, data: dict) -> bool:
        from linkml.validator import validate

        report = validate(data, _merged_schema(), "CombinationProduct")
        for result in report.results:
            self.error(path, "schema", result.message)
        return not report.results

    def _check_identity(self, path: Path, data: dict) -> None:
        ings = data.get("ingredients") or []
        ids = [i["id"] for i in ings]
        labels = [i["label"] for i in ings]
        if len(set(ids)) != len(ids):
            self.error(path, "ingredients", "an ingredient is listed twice")
        expected_id = combination_id(ids)
        if data.get("id") != expected_id:
            self.error(path, "id", f"must be {expected_id} (computed from the ingredients)")
        expected_name = combination_name(labels)
        if data.get("name") != expected_name:
            self.error(path, "name", f"must be {expected_name!r} (computed from the ingredients)")
        expected_file = f"{combination_slug(labels)}.yaml"
        if path.name != expected_file:
            self.error(path, "file name", f"must be {expected_file}")
        for i, ing in enumerate(ings):
            canonical = self.ingredients.normalize(ing["id"])
            if canonical != ing["id"]:
                self.error(path, f"ingredients[{i}]",
                           f"{ing['id']} normalizes to {canonical}; use the canonical id")

    def _check_history(self, path: Path) -> None:
        """Every curated file carries at least one valid append-only history record.

        The record is where the session, the actors and — for an AI actor — the model that
        did the work are written down (``medic.curation.history``, adopted from dismech).
        """
        try:
            target = path.resolve().relative_to(Path.cwd()).as_posix()
        except ValueError:
            target = path.as_posix()
        records = history.records_for(target)
        if not records:
            self.error(path, "history", "no history record; scaffold one with "
                                        "`just new-history --kind combination --slug "
                                        f"{path.stem} ...`")
        for record in records:
            for problem in history.check_record(record):
                self.error(path, "history", f"{record}: {problem}")

    # -- one product block -------------------------------------------------------------
    def _check_product(self, path: Path, data: dict, product: dict, where: str) -> None:
        source = product.get("source")
        if SOURCE_JURISDICTION.get(source) != product.get("jurisdiction"):
            self.error(path, where, f"source {source} must have jurisdiction "
                                    f"{SOURCE_JURISDICTION.get(source)}")
        setid = product["document"]["setid"]
        try:
            doc = fetch_label(setid, offline=self.offline)
        except Exception as exc:  # noqa: BLE001 — any fetch failure means "cannot verify"
            self.error(path, where, f"cannot load label {setid}: {exc}")
            return
        pinned = product["document"].get("version")
        if doc.version is not None and pinned != doc.version:
            self.warn(path, f"{where}.document.version",
                      f"file pins version {pinned}, DailyMed now serves {doc.version}; "
                      "quotes were checked against the current version")
        names = self._ingredient_names(path, data, doc, where)
        seen: set[tuple] = set()
        for c_idx, claim in enumerate(product.get("claims") or []):
            cwhere = f"{where}.claims[{c_idx}]"
            key = _claim_key(claim)
            if key in seen:
                self.warn(path, cwhere, "duplicate of an earlier claim in this product")
            seen.add(key)
            self._check_claim(path, claim, doc, names, cwhere)

    def _ingredient_names(self, path: Path, data: dict, doc: LabelDocument,
                          where: str) -> dict[str, set[str]]:
        """Check the ingredient set against the label; return each id's names (lowercased).

        The names are what the label calls the ingredient (its activeMoiety names) plus the
        ingredient's own label, so an attribution can be checked against the quote.
        """
        names: dict[str, set[str]] = {i["id"]: {i["label"].lower()}
                                      for i in data.get("ingredients") or []}
        label_ids = set()
        for moiety in doc.active_moieties:
            ing, why = self.ingredients.resolve(moiety)
            if ing is None:
                self.error(path, where, f"label ingredient cannot be resolved: {why}")
                return names
            label_ids.add(ing.id)
            names.setdefault(ing.id, set()).add(moiety.lower())
        if set(names) != label_ids:
            self.error(path, "ingredients",
                       f"must equal the label's active ingredients {sorted(label_ids)}; "
                       f"file has {sorted(set(names))}")
        return names

    # -- one claim ---------------------------------------------------------------------
    def _check_claim(self, path: Path, claim: dict, doc: LabelDocument,
                     names: dict[str, set[str]], where: str) -> None:
        relation, section, quote = claim["relation"], claim["section"], claim["quote"]
        if RELATION_SECTION[relation] != section:
            self.error(path, where, f"{relation} claims must quote the "
                                    f"{RELATION_SECTION[relation]} section, not {section}")
        self._check_quote(path, claim, doc, where)
        for slot, desc in _descriptors(claim):
            self._check_descriptor(path, quote, slot, desc, f"{where}.{slot}")
        if claim.get("age_range") and not _contains(quote, claim["age_range"].get("text", "")):
            self.error(path, f"{where}.age_range.text", "does not occur in the quote")
        self._check_attribution(path, claim, names, where)
        self._check_own_ingredient_allergy(path, claim, names, where)
        if claim["status"] in ("REVIEWED", "REJECTED") and not claim.get("reviewer"):
            self.error(path, f"{where}.status", f"{claim['status']} requires a reviewer")
        self._check_object_specificity(path, claim["object"], f"{where}.object")
        self._check_polarity_and_wording(path, claim, where)

    def _check_quote(self, path: Path, claim: dict, doc: LabelDocument, where: str) -> None:
        section, quote = claim["section"], claim["quote"]
        if not _contains(doc.section_text(section), quote):
            self.error(path, f"{where}.quote",
                       f"not a verbatim substring of the label's {section} section")
            return
        if section != "INDICATIONS":
            return
        spans = doc.spans(section)
        readable = [s for s in spans if s["role"] not in NON_CLAIM_ROLES]
        if any(_contains(s["text"], quote) for s in readable):
            return
        if any(_contains(s["text"], quote) for s in spans if s["role"] == "LIMITATION_STATEMENT"):
            self.error(path, f"{where}.quote",
                       "comes from a Limitations of Use passage, which restricts a claim "
                       "rather than making one")
        else:
            self.warn(path, f"{where}.quote", "spans more than one label element; "
                                              "quote a single sentence where possible")

    def _check_descriptor(self, path: Path, quote: str, slot: str, desc: dict,
                          where: str) -> None:
        """Text always, verbatim; a term optional; an unbound concept says why."""
        if not _contains(quote, desc.get("text", "")):
            self.error(path, f"{where}.text", f"{desc.get('text')!r} does not occur in the quote")
        term = desc.get("term")
        if not term:
            if not (desc.get("term_gap") or "").strip():
                self.error(path, where, "no term: record what was searched in term_gap")
            return
        if not desc.get("binding"):
            self.error(path, f"{where}.binding", "required when a term is given")
        prefix = get_prefix(term["id"])
        usual = USUAL_PREFIXES.get(slot)
        if usual and prefix not in usual:
            self.warn(path, f"{where}.term", f"{term['id']}: {slot} usually uses "
                                             f"{', '.join(usual)}; confirm this is intended")
        self._check_term(path, term, f"{where}.term")

    def _check_term(self, path: Path, term: dict, where: str) -> None:
        if not self.check_terms:
            return
        prefix = get_prefix(term["id"])
        if terms.source_selector(prefix) == "obo" and prefix not in terms.PREFIX_SOURCES:
            self.warn(path, where, f"cannot check {term['id']}: no term source for {prefix}")
            return
        try:
            info = terms.lookup(term["id"])
        except Exception as exc:  # noqa: BLE001 — unloadable ontology means "cannot check"
            self.error(path, where, f"cannot check {term['id']}: {exc}")
            return
        if info is None:
            self.error(path, where, f"{term['id']} does not exist in {prefix}")
        elif info.obsolete:
            hint = f"; replaced by {info.replaced_by}" if info.replaced_by else ""
            self.error(path, where, f"{term['id']} is obsolete{hint}")
        elif info.label.lower() != term["label"].lower():
            self.error(path, where, f"{term['id']} is labelled {info.label!r}, not {term['label']!r}")

    # -- specificity: the term must be as specific as the label's words ----------------
    def _check_object_specificity(self, path: Path, obj: dict, where: str) -> None:
        """Compare the bound term with every term the object's text names.

        Each word span of ``object.text`` is looked up as an exact label or synonym in the
        ontologies and as a disease grounding store row. The bound term may equal one of
        those anchors or be more specific than it; it may not be *broader* than any of them
        (the label's words support something more specific), and it should not be unrelated
        to all of them.
        """
        text, term = obj.get("text", ""), obj.get("term")
        anchors = self._anchors(text)  # id -> "ontology" | "store"
        if not term:
            if anchors and self.check_terms:
                self.warn(path, where, f"text matches {sorted(anchors)}; bind one of them or "
                                       "explain in term_gap why none applies")
            return
        chosen = term["id"]
        prefix = get_prefix(chosen)
        same = {a: how for a, how in anchors.items() if get_prefix(a) == prefix}
        other = {a: how for a, how in anchors.items() if get_prefix(a) != prefix}
        if not same:
            if any(how == "store" for how in other.values()):
                self.warn(path, where, f"grounding store maps {text!r} to {sorted(other)}; the "
                                       f"curated {chosen} is in another ontology — confirm and "
                                       "consider a semapv:ManualMappingCuration row")
            elif not anchors:
                self.warn(path, where, f"no term or grounding row matches any part of {text!r}; "
                                       "confirm the binding")
            return
        if not self.check_terms:
            if chosen not in same:
                self.warn(path, where, f"text matches {sorted(same)}, not {chosen}; "
                                       "run with term checks to compare them")
            return
        # A longer part of the phrase can name a more specific term than a shorter part:
        # "acute myocardial infarction" contains "myocardial infarction". Check that first,
        # so matching the shorter part never excuses the broader binding.
        narrower_matches = sorted(a for a in same if chosen in terms.ancestors(a))
        if narrower_matches:
            self.error(path, where, f"{chosen} is broader than the label's own words, which "
                                    f"match {narrower_matches}; bind the more specific term")
            return
        if chosen in same or any(a in terms.ancestors(chosen) for a in same):
            return  # what the words name, or more specific and justified by the quote
        store_targets = sorted(a for a, how in same.items() if how == "store")
        if len(store_targets) == 1:
            # The words name one condition, the store grounds it, and the curated term is
            # unrelated to it: a real disagreement.
            self.error(path, where, f"grounding store maps {text!r} to {store_targets}, which is "
                                    f"unrelated to {chosen}; fix the claim or add a "
                                    "semapv:ManualMappingCuration row")
        else:
            # Several unrelated anchors means the text lists conditions (a named syndrome,
            # "asthma, rhinitis, and nasal polyps"); the composite term is not contradicted.
            self.warn(path, where, f"text matches {sorted(same)}, none related to {chosen}; "
                                   "confirm the binding")

    def _anchors(self, text: str) -> dict[str, str]:
        found: dict[str, str] = {}
        for span in _word_spans(text):
            for row in self.disease_grounding.lookup(span):
                if row.object_id and row.object_id != NO_TERM:
                    found.setdefault(self._normalize_disease(row.object_id), "store")
            if self.check_terms:
                for prefix in ANCHOR_PREFIXES:
                    try:
                        ids = terms.exact_matches(prefix, span)
                    except Exception:  # noqa: BLE001 — no source: no anchors from it
                        ids = ()
                    for curie in ids:
                        found[curie] = found.get(curie, "ontology")
        return found

    def _normalize_disease(self, curie: str) -> str:
        norm = self.disease_normalization.lookup(curie)
        return norm.object_id if norm and norm.object_id else curie

    # -- attribution and allergy -------------------------------------------------------
    def _check_attribution(self, path: Path, claim: dict, names: dict[str, set[str]],
                           where: str) -> None:
        """An ingredient is credited only when the label names it in the quote."""
        low = _norm_ws(claim["quote"]).lower()
        for i, attribution in enumerate(claim.get("attributed_to") or []):
            awhere = f"{where}.attributed_to[{i}]"
            ingredient = attribution["ingredient"]
            if ingredient not in names:
                self.error(path, awhere, f"{ingredient} is not one of this combination's "
                                         "ingredients")
                continue
            if not any(n in low for n in names[ingredient]):
                self.error(path, awhere, f"the quote never names {ingredient} "
                                         f"({' / '.join(sorted(names[ingredient]))}); "
                                         "attribution must be stated by the label")
            if attribution["basis"] == "STATED_REASON" and not _CAUSE_CUES.search(low):
                self.warn(path, awhere, "STATED_REASON, but the quote has no causal wording "
                                        "(because / due to / risk / associated with)")
            if attribution["basis"] == "COMPONENT_STATEMENT" and "component" not in low:
                self.warn(path, awhere, "COMPONENT_STATEMENT, but the quote never says "
                                        "'component'; check it is not a stated reason")

    def _check_own_ingredient_allergy(self, path: Path, claim: dict,
                                      names: dict[str, set[str]], where: str) -> None:
        """Allergy to the product's own ingredients is not curated (only cross-sensitivity)."""
        specs = claim.get("object_specialization") or []
        if claim["relation"] != "CONTRAINDICATED" or not specs:
            return

        def own(spec: dict) -> bool:
            term = spec.get("term") or {}
            return term.get("id") in names or bool(_OWN_PRODUCT.search(spec.get("text", "")))

        if all(own(s) for s in specs):
            self.error(path, f"{where}.object_specialization",
                       "allergy to the product's own ingredients is not curated; keep only "
                       "cross-sensitivities to other agents")

    def _check_polarity_and_wording(self, path: Path, claim: dict, where: str) -> None:
        relation, quote, text = claim["relation"], claim["quote"], claim["object"]["text"]
        cues = _CONTRA_NEGATION_CUES if relation == "CONTRAINDICATED" else _NEGATION_CUES
        neg, total, cue = assertion_negated(text, quote, head_fallback=False, cues=cues)
        if total and neg == total:
            self.warn(path, f"{where}.quote",
                      f"{text!r} follows the negation cue {cue!r}; check the label states "
                      "this claim positively")
        low = quote.lower()
        if relation == "PREVENTS" and not any(c in low for c in _PREVENT_CUES):
            self.warn(path, f"{where}.relation", "PREVENTS, but the quote has no "
                                                 "risk-reduction or prevention wording")
        if relation == "RELIEVES_SYMPTOMS" and not any(c in low for c in _RELIEF_CUES):
            self.warn(path, f"{where}.relation", "RELIEVES_SYMPTOMS, but the quote has no "
                                                 "relief or symptom wording")
        if relation == "TREATS" and re.search(r"\b(reduc\w* the (combined )?risk|prevent)", low):
            self.warn(path, f"{where}.relation", "TREATS, but the quote reads as "
                                                 "risk reduction; consider PREVENTS")


#: Descriptor slots on a claim, single- or multi-valued.
SINGLE_DESCRIPTORS = ("object", "disease_context", "anatomical_context", "life_stage", "sex",
                      "line_of_therapy")
MULTI_DESCRIPTORS = ("object_specialization", "in_combination_with")


def _descriptors(claim: dict):
    for slot in SINGLE_DESCRIPTORS:
        if claim.get(slot):
            yield slot, claim[slot]
    for slot in MULTI_DESCRIPTORS:
        for desc in claim.get(slot) or []:
            yield slot, desc


def _desc_key(desc: dict | None) -> str:
    if not desc:
        return ""
    term = desc.get("term") or {}
    return term.get("id") or _norm_ws(desc.get("text", "")).lower()


def _claim_key(claim: dict) -> tuple:
    """Two claims are duplicates only when every part of them agrees, context included."""
    return (
        claim.get("relation"),
        _desc_key(claim.get("object")),
        tuple(sorted(_desc_key(d) for d in claim.get("object_specialization") or [])),
        tuple(_desc_key(claim.get(s)) for s in SINGLE_DESCRIPTORS[1:]),
        tuple(sorted(_desc_key(d) for d in claim.get("in_combination_with") or [])),
        tuple(sorted(a.get("ingredient", "") for a in claim.get("attributed_to") or [])),
        str(claim.get("age_range") or ""),
    )


def _word_spans(text: str, max_words: int = 8) -> list[str]:
    """Every contiguous run of words in ``text``, longest first (punctuation trimmed)."""
    words = [w.strip(".,;:()[]\"'") for w in _norm_ws(text).split()]
    words = [w for w in words if w]
    spans = []
    for n in range(min(len(words), max_words), 0, -1):
        for i in range(len(words) - n + 1):
            spans.append(" ".join(words[i:i + n]))
    return spans


@lru_cache(maxsize=1)
def _merged_schema():
    """The schema with its imports merged, resolved next to the schema file.

    Handing linkml the path resolves ``imports`` against the working directory instead.
    """
    from linkml_runtime import SchemaView

    view = SchemaView(str(SCHEMA_PATH))
    view.merge_imports()
    return view.schema


def _files(paths: list[str]) -> list[Path]:
    if paths:
        return [Path(p) for p in paths]
    return sorted(COMBINATIONS_DIR.glob("*.yaml"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", help="files to check (default: kb/combinations/)")
    parser.add_argument("--no-terms", action="store_true",
                        help="skip ontology term checks (fast local iteration only)")
    parser.add_argument("--offline", action="store_true",
                        help="use cached labels only; never download")
    args = parser.parse_args(argv)
    files = _files(args.files)
    if not files:
        print("no combination files to check")
        return 0
    checker = Checker(check_terms=not args.no_terms, offline=args.offline)
    for path in files:
        checker.check_file(path)
    for finding in checker.findings:
        print(finding)
    errors = sum(f.level == "ERROR" for f in checker.findings)
    warnings = len(checker.findings) - errors
    print(f"\n{len(files)} file(s): {errors} error(s), {warnings} warning(s)")
    if args.no_terms:
        print("NOTE: term checks were skipped (--no-terms); CI runs them.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

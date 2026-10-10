"""Deterministic checks for curated combination files (``kb/combinations/*.yaml``).

``uv run python -m medic.curation.validate [files...]`` (or ``just validate-curation``).
With no files, every file under ``kb/combinations/`` is checked. Exit status 1 when any
ERROR is found; WARNINGs are reported and do not fail.

ERRORs are facts the file must get right and a machine can settle:

- the schema; the id, name and file name recomputed from the ingredients, whose labels must
  be the resolver's; one file per combination id; curator references that resolve to a
  committed skill;
- the ingredient set equal to the label's own structured active ingredients;
- every quote a verbatim substring of the right label section, and not from a Limitations of
  Use passage; every descriptor's ``text`` inside its quote;
- a bound term that exists, is current, carries its label, comes from a configured source and
  has a ``binding`` consistent with what the text names; an unbound descriptor that records
  its ``term_gap``; no bound term broader than, or unrelated to, the single term the text
  names (checked for every descriptor, not only the object);
- attributions the label states: "the X component" for a component statement, the
  ingredient within the causal clause for a stated reason — never a mention inside the
  product's own name;
- an allergy contraindication that names its agents, none of them the product's own;
- source and jurisdiction consistent; at least one valid history record.

WARNINGs are heuristics or gaps a human should look at: a negation cue in front of the
condition, a relation the quote's wording does not suggest, an unusual ontology for a slot,
anchors in another ontology, a label revised since the file pinned its version.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from medic.curation import history, terms
from medic.curation.identity import (
    IngredientResolver,
    combination_id,
    combination_name,
    combination_slug,
    require_store,
)
from medic.curation.label import SKILL_PATH, LabelDocument, fetch_label, pinned_label
from medic.curation.paths import enter_repo_root
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
    "PREVENTATIVE_FOR_CONDITION": "INDICATIONS",
    "AMELIORATES_CONDITION": "INDICATIONS",
    "CONTRAINDICATED_IN": "CONTRAINDICATIONS",
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
    "when_coadministered_with": ("CHEBI",),
    "examples": ("MONDO", "HP"),
}

#: Per slot: which ontologies' labels and synonyms, and which grounding store, anchor the
#: specificity check on a descriptor's text.
ANCHOR_SOURCES = {
    "object": (("MONDO", "HP"), "disease"),
    "disease_context": (("MONDO", "HP"), "disease"),
    "anatomical_context": (("UBERON",), None),
    "life_stage": (("HsapDv",), None),
    "sex": (("PATO",), None),
    "object_specialization": (("CHEBI",), "drug"),
    "in_combination_with": (("CHEBI",), "drug"),
    "when_coadministered_with": (("CHEBI",), "drug"),
    "examples": (("MONDO", "HP"), "disease"),
    "line_of_therapy": ((), None),
}

#: Causal wording a stated reason hangs on; the ingredient must follow within the clause.
_CAUSE_CUES = re.compile(r"\b(because|due to|caused by|may cause|since|related to|"
                         r"associated with|attributable to)\b")
_CAUSE_WINDOW_WORDS = 20
_OWN_PRODUCT = re.compile(r"component|excipient|ingredient|this product|the formulation", re.I)
_ALLERGY = re.compile(r"hypersensitiv|allerg|anaphyla", re.I)
#: Allergy subtrees: a term under either is an allergy, whatever its words. Deliberately not
#: the wider hypersensitivity, angioedema or urticaria subtrees: those hold hereditary
#: angioedema (an ACE-inhibitor contraindication that names no agent), asthma and celiac
#: disease, which are not allergies to anything the label names.
ALLERGY_ROOTS = frozenset({
    "MONDO:0005271",  # allergic disease (covers drug allergy)
    "HP:0012393",     # Allergy (covers Drug allergy)
})
#: HP subtrees that qualify a finding rather than name one; "severe" in "severe renal
#: impairment" matches HP "Severe", which says nothing about the condition.
MODIFIER_ROOTS = frozenset({
    "HP:0012823",     # Clinical modifier
    "HP:0000005",     # Mode of inheritance
    "HP:0040279",     # Frequency
    "MONDO:0021125",  # disease characteristic ("acquired", "inherited", ...)
})
#: Roots too general to say anything about what the words name ("disease" in "obstructive
#: disease of the GI tract").
GENERIC_ANCHORS = frozenset({
    "MONDO:0000001",  # disease
    "MONDO:0700096",  # human disease
    "MONDO:0002254",  # syndromic disease
    "HP:0000001",     # All
    "HP:0000118",     # Phenotypic abnormality
})
#: Qualifiers that describe the patient or the event rather than a narrower condition.
_NON_NARROWING = frozenset({
    "a", "an", "the", "of", "with", "in", "and", "or", "patients", "patient",
    "previous", "prior", "history", "recent", "known", "suspected", "documented",
    "confirmed", "nonfatal", "non-fatal", "fatal", "new", "existing", "pre-existing",
})
#: Words after which a label lists examples or members of what it just named; the narrower
#: terms in that tail do not make the head term too broad.
_INCLUSION = re.compile(r"\b(including|such as|e\.g\.?|for example|especially)\b", re.I)
_CURATOR_REF = re.compile(
    r"^https://github\.com/monarch-initiative/medic/blob/[0-9a-f]{7,40}/"
    + re.escape(SKILL_PATH) + r"#git-blob=([0-9a-f]{40})$")

#: FDA DESI efficacy classifications as labels state them ('classified ... as "possibly"
#: effective').
_DESI_WORDING = {
    "DESI_POSSIBLY_EFFECTIVE": re.compile(r"possibly\W{0,3}\s*effective", re.I),
    "DESI_PROBABLY_EFFECTIVE": re.compile(r"probably\W{0,3}\s*effective", re.I),
}

#: Wording that suggests each non-default relation; used only for a WARNING.
_PREVENT_CUES = ("risk", "prevent", "prophyla", "reduc")
_RELIEF_CUES = ("relie", "symptom", "temporar", "reduc", "control")


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


def _word(name: str) -> re.Pattern:
    """``name`` as whole words, case-insensitive (so "aspirin" never matches inside a word)."""
    return re.compile(r"(?<![a-z0-9])" + re.escape(name.lower()) + r"(?![a-z0-9])")


class Checker:
    def __init__(self, *, check_terms: bool = True, offline: bool = False,
                 check_upstream: bool = False):
        self.check_terms = check_terms
        self.offline = offline
        self.check_upstream = check_upstream
        self._unloadable: set[str] = set()
        self.ingredients = IngredientResolver()
        require_store(DISEASE_GROUNDING_STORE)
        require_store(DISEASE_NORMALIZATION_STORE)
        self.disease_grounding = LiteralMappingStore(DISEASE_GROUNDING_STORE, "disease")
        self.disease_grounding.load()
        self.disease_normalization = NormalizationMappingStore(
            DISEASE_NORMALIZATION_STORE, "disease")
        self.disease_normalization.load()
        self.findings: list[Finding] = []
        self._ids: dict[str, Path] = {}

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
        self._check_curators(path, data)
        self._check_history(path)
        for p_idx, product in enumerate(data.get("products") or []):
            self._check_product(path, data, product, f"products[{p_idx}]")
        self._check_consistent_bindings(path, data)

    def _check_consistent_bindings(self, path: Path, data: dict) -> None:
        """The same wording should bind the same term throughout one file."""
        seen: dict[str, set[str]] = {}
        for product in data.get("products") or []:
            for claim in product.get("claims") or []:
                for _slot, desc in _descriptors(claim):
                    term = (desc.get("term") or {}).get("id")
                    if term:
                        key = _norm_ws(desc.get("text", "")).lower()
                        seen.setdefault(key, set()).add(term)
        for text, ids in sorted(seen.items()):
            if len(ids) > 1:
                self.warn(path, "bindings", f"{text!r} is bound to {sorted(ids)} in different "
                                            "claims; bind one term unless the contexts differ")

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
        elif expected_id in self._ids and self._ids[expected_id] != path:
            self.error(path, "id", f"same combination as {self._ids[expected_id]}; one file "
                                   "per ingredient set (add a product block there instead)")
        self._ids.setdefault(expected_id, path)
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

    def _check_curators(self, path: Path, data: dict) -> None:
        """An AI curator's reference names the skill and the git blob id of its exact text.

        The blob id identifies content, not history, so this needs no particular clone depth
        and survives squash merges. A reference to an older skill text is allowed (curations
        outlive edits to the skill) but reported.
        """
        current = subprocess.run(["git", "hash-object", SKILL_PATH], capture_output=True,
                                 text=True)
        current_blob = current.stdout.strip() if current.returncode == 0 else ""
        for i, curator in enumerate(data.get("curators") or []):
            if curator.get("curator_type") != "AI_AGENT":
                continue
            ref = curator.get("curator_id") or ""
            where = f"curators[{i}].curator_id"
            match = _CURATOR_REF.match(ref)
            if not match:
                self.error(path, where, f"{ref!r} is not a reference to this repository's "
                                        f"{SKILL_PATH} with its git blob id; commit the skill "
                                        "and regenerate the curator block with the label "
                                        "helper")
            elif current_blob and match.group(1) != current_blob:
                self.warn(path, where, f"curated with an earlier skill text (git blob "
                                       f"{match.group(1)[:12]}; current {current_blob[:12]})")

    def _check_history(self, path: Path) -> None:
        """Every curated file carries at least one valid append-only history record.

        The record is where the session, the actors and — for an AI actor — the model that
        did the work are written down (``medic.curation.history``, adopted from dismech).
        Whether a *change* came with a new record is checked per pull request in CI.
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
        pinned = product["document"].get("version")
        try:
            doc = pinned_label(setid, pinned)
        except FileNotFoundError as exc:
            self.error(path, f"{where}.document", str(exc))
            return
        if doc.setid != setid or doc.version != pinned:
            self.error(path, f"{where}.document", f"the pin for {setid} v{pinned} holds "
                                                  f"{doc.setid} v{doc.version}; pins are "
                                                  "written by the label helper, never by hand")
        if self.check_upstream:
            try:
                current = fetch_label(setid)
            except Exception as exc:  # noqa: BLE001 — upstream check is advisory
                self.warn(path, f"{where}.document", f"cannot reach DailyMed: {exc}")
            else:
                if current.version != pinned:
                    self.warn(path, f"{where}.document.version",
                              f"file pins version {pinned}; DailyMed now serves "
                              f"{current.version}. Re-check the claims against it.")
        names = self._ingredient_names(path, data, doc, where)
        product_names = [n for n in (doc.product_name, product.get("product_name")) if n]
        seen: set[tuple] = set()
        for c_idx, claim in enumerate(product.get("claims") or []):
            cwhere = f"{where}.claims[{c_idx}]"
            key = _claim_key(claim)
            if key in seen:
                self.warn(path, cwhere, "duplicate of an earlier claim in this product")
            seen.add(key)
            self._check_claim(path, claim, doc, names, product_names, cwhere)

    def _ingredient_names(self, path: Path, data: dict, doc: LabelDocument,
                          where: str) -> dict[str, set[str]]:
        """Check the ingredient set and labels against the label; return each id's names.

        The names (lowercased) are what the label calls the ingredient (its activeMoiety
        names) plus the ingredient's own label, so an attribution can be checked against the
        quote.
        """
        file_labels = {i["id"]: i["label"] for i in data.get("ingredients") or []}
        names: dict[str, set[str]] = {i: {lbl.lower()} for i, lbl in file_labels.items()}
        label_ids = set()
        for moiety in doc.active_moieties:
            ing, why = self.ingredients.resolve(moiety)
            if ing is None:
                self.error(path, where, f"label ingredient cannot be resolved: {why}")
                return names
            label_ids.add(ing.id)
            names.setdefault(ing.id, set()).add(moiety.lower())
            if ing.id in file_labels and file_labels[ing.id] != ing.label:
                self.error(path, "ingredients",
                           f"{ing.id} must be labelled {ing.label!r} (the resolver's label, "
                           f"which the name and file name are computed from), not "
                           f"{file_labels[ing.id]!r}")
        if set(file_labels) != label_ids:
            self.error(path, "ingredients",
                       f"must equal the label's active ingredients {sorted(label_ids)}; "
                       f"file has {sorted(file_labels)}")
        return names

    # -- one claim ---------------------------------------------------------------------
    def _check_claim(self, path: Path, claim: dict, doc: LabelDocument,
                     names: dict[str, set[str]], product_names: list[str], where: str) -> None:
        relation, section, quote = claim["relation"], claim["section"], claim["quote"]
        if RELATION_SECTION[relation] != section:
            self.error(path, where, f"{relation} claims must quote the "
                                    f"{RELATION_SECTION[relation]} section, not {section}")
        self._check_quote(path, claim, doc, where)
        for slot, desc in _descriptors(claim):
            self._check_descriptor(path, quote, slot, desc, f"{where}.{slot}")
        if claim.get("age_range") and not _contains(quote, claim["age_range"].get("text", "")):
            self.error(path, f"{where}.age_range.text", "does not occur in the quote")
        self._check_attribution(path, claim, doc, names, product_names, where)
        self._check_allergy(path, claim, names, where)
        if claim.get("when_coadministered_with") and relation != "CONTRAINDICATED_IN":
            self.error(path, f"{where}.when_coadministered_with",
                       "only a CONTRAINDICATED_IN claim can depend on a co-administered agent")
        self._check_evidence_status(path, claim, doc, where)
        if claim["status"] in ("REVIEWED", "REJECTED") and not claim.get("reviewer"):
            self.error(path, f"{where}.status", f"{claim['status']} requires a reviewer")
        self._check_polarity_and_wording(path, claim, where)

    def _check_evidence_status(self, path: Path, claim: dict, doc: LabelDocument,
                               where: str) -> None:
        """DESI wording on the label and evidence_status on its indications must agree."""
        if claim["section"] != "INDICATIONS":
            if claim.get("evidence_status"):
                self.error(path, f"{where}.evidence_status", "applies to indications only")
            return
        text = _norm_ws(doc.section_text("INDICATIONS"))
        stated = {status for status, pattern in _DESI_WORDING.items() if pattern.search(text)}
        status = claim.get("evidence_status")
        if status and status not in stated:
            self.error(path, f"{where}.evidence_status",
                       f"{status}, but the label's Indications do not use that DESI wording")
        elif stated and not status:
            self.error(path, f"{where}.evidence_status",
                       f"the label classifies its indications as {sorted(stated)}; record it "
                       "in evidence_status")

    def _check_quote(self, path: Path, claim: dict, doc: LabelDocument, where: str) -> None:
        section, quote = claim["section"], claim["quote"]
        if not _contains(doc.section_text(section), quote):
            self.error(path, f"{where}.quote",
                       f"not a verbatim substring of the label's {section} section")
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
            self._check_unbound(path, slot, desc, where)
            return
        if not desc.get("binding"):
            self.error(path, f"{where}.binding", "required when a term is given")
        prefix = get_prefix(term["id"])
        usual = USUAL_PREFIXES.get(slot)
        if usual and prefix not in usual:
            self.warn(path, f"{where}.term", f"{term['id']}: {slot} usually uses "
                                             f"{', '.join(usual)}; confirm this is intended")
        if self._check_term(path, term, f"{where}.term"):
            self._check_specificity(path, slot, desc, where)

    def _check_term(self, path: Path, term: dict, where: str) -> bool:
        """True when the term was found current and correctly labelled."""
        if not self.check_terms:
            return True
        prefix = get_prefix(term["id"])
        if terms.source_selector(prefix) == "obo" and prefix not in terms.PREFIX_SOURCES:
            self.error(path, where, f"{term['id']}: no term source is configured for {prefix}, "
                                    "so the term cannot be checked; add one in "
                                    "conf/curation_terms.yaml or use a configured ontology")
            return False
        try:
            info = terms.lookup(term["id"])
        except Exception as exc:  # noqa: BLE001 — unloadable ontology means "cannot check"
            self.error(path, where, f"cannot check {term['id']}: {exc}")
            return False
        if info is None:
            self.error(path, where, f"{term['id']} does not exist in {prefix}")
        elif info.obsolete:
            hint = f"; replaced by {info.replaced_by}" if info.replaced_by else ""
            self.error(path, where, f"{term['id']} is obsolete{hint}")
        elif info.label.lower() != term["label"].lower():
            self.error(path, where, f"{term['id']} is labelled {info.label!r}, not {term['label']!r}")
        else:
            return True
        return False

    # -- specificity: the term must be as specific as the label's words ----------------
    def _check_unbound(self, path: Path, slot: str, desc: dict, where: str) -> None:
        if not self.check_terms or slot not in ("object", "disease_context"):
            return
        onto, store = self._anchors(slot, desc.get("text", ""))
        if onto or store:
            self.warn(path, where, f"text matches {sorted(set(onto) | set(store))}; bind one "
                                   "of them or explain in term_gap why none applies")

    def _check_specificity(self, path: Path, slot: str, desc: dict, where: str) -> None:
        """Compare the bound term with every term the descriptor's text names.

        Each word span of the text (and its singular) is looked up as an exact label or
        synonym in the slot's ontologies, and as a grounding-store row. The bound term may
        not be broader than any of those anchors, nor unrelated to the single term the words
        name. ``binding`` is checked only against ontology matches on the *whole* text — the
        stores are too coarse ("NSAIDs" -> anti-inflammatory drug), and what a *part* of the
        text names ("infection" in "HIV-1 infection") says nothing about the whole: EXACT for
        the term the whole text names, NARROWER for a descendant of it, never NARROWER for a
        term only part of the text names.
        """
        text, term, binding = desc.get("text", ""), desc["term"], desc.get("binding")
        onto, store = self._anchors(slot, text)  # id -> names the whole text?
        chosen = term["id"]
        prefix = get_prefix(chosen)
        named = sorted(a for a in set(onto) | set(store) if get_prefix(a) == prefix)
        other = sorted(a for a in set(onto) | set(store) if get_prefix(a) != prefix)
        if not named:
            if other:
                self.warn(path, where, f"text matches {other} in another ontology, not {chosen}; "
                                       "confirm the binding"
                          + (" and consider a semapv:ManualMappingCuration row"
                             if any(a in store for a in other) else ""))
            elif slot in ("object", "disease_context") and self.check_terms:
                self.warn(path, where, f"no term or grounding row matches any part of {text!r}; "
                                       "confirm the binding")
            return
        if not self.check_terms:
            if chosen not in named:
                self.warn(path, where, f"text matches {named}, not {chosen}; "
                                       "run with term checks to compare them")
            return
        # A longer part of the phrase can name a more specific term than a shorter part:
        # "acute myocardial infarction" contains "myocardial infarction". Check that first,
        # so matching the shorter part never excuses the broader binding.
        narrower = sorted(a for a in named if chosen in terms.ancestors(a))
        if narrower:
            self.error(path, where, f"{chosen} is broader than the label's own words, which "
                                    f"match {narrower}; bind the more specific term")
            return
        chosen_ancestors = terms.ancestors(chosen)
        if chosen in onto:
            if onto[chosen] and binding != "EXACT":
                self.error(path, f"{where}.binding", f"the whole text names {chosen}; "
                                                     f"binding must be EXACT, not {binding}")
            elif not onto[chosen] and binding == "NARROWER":
                self.error(path, f"{where}.binding", f"{chosen} is what part of the text names, "
                                                     "so it cannot be NARROWER than the text")
            elif not onto[chosen] and binding == "EXACT":
                extra = self._narrowing_words(text, chosen)
                if extra:
                    self.warn(path, f"{where}.binding",
                              f"{chosen} is what only part of {text!r} names; {extra} may "
                              "narrow the condition, making the term BROADER than the text")
            return
        if chosen in named:
            return  # a store row agrees; the store is too coarse to judge the binding
        whole_ancestors = sorted(a for a, whole in onto.items()
                                 if whole and a in chosen_ancestors)
        if whole_ancestors:
            if binding != "NARROWER":
                self.error(path, f"{where}.binding", f"{chosen} is more specific than what the "
                                                     f"whole text names ({whole_ancestors}); "
                                                     f"binding must be NARROWER, not {binding}")
            return
        if any(a in chosen_ancestors for a in named):
            return  # more specific than what part of the words name
        store_targets = sorted(a for a in store if get_prefix(a) == prefix)
        if len(store_targets) == 1 or len(named) == 1:
            # The words name one condition and the curated term is unrelated to it.
            self.error(path, where, f"the label's words name {named}, which is unrelated to "
                                    f"{chosen}; fix the binding"
                       + (" or add a semapv:ManualMappingCuration row" if store_targets else ""))
        else:
            # Several unrelated anchors means the text lists conditions (a named syndrome,
            # "asthma, rhinitis, and nasal polyps"); the composite term is not contradicted.
            self.warn(path, where, f"text matches {named}, none related to {chosen}; "
                                   "confirm the binding")

    def _narrowing_words(self, text: str, chosen: str) -> list[str]:
        """Words of ``text`` outside the term's own name that could narrow the condition.

        Timing, outcome and certainty qualifiers ("previous MI", "nonfatal MI", "suspected
        viral infections") describe the patient or the event, not a narrower disease, so an
        EXACT binding is right despite them.
        """
        info = terms.lookup(chosen)
        named = set()
        for name in (info.label, *info.exact_synonyms) if info else ():
            named |= set(_norm_ws(name).lower().split())
        low = _norm_ws(text).lower()
        # "with or without aura", "arterial or venous", "inherited or acquired": alternatives
        # that together cover the term do not narrow it.
        low = re.sub(r"\bwith or without\s+\S+", " ", low)
        low = re.sub(r"\b[\w-]+\s+(?:or|and/or)\s+[\w-]+\b", " ", low)
        words = [w.strip(".,;:()[]\"'") for w in low.split()]
        return [w for w in words if w and w not in named and _singular(w) not in named
                and w not in _NON_NARROWING]

    def _anchors(self, slot: str, text: str) -> tuple[dict[str, bool], dict[str, bool]]:
        """Terms the text names, from ontologies and from the stores: id -> whole text?"""
        prefixes, store_kind = ANCHOR_SOURCES.get(slot, ((), None))
        # Plurals are folded for diseases, phenotypes and anatomy, whose labels are singular.
        # Not for chemicals: in ChEBI the plural is the class ("benzimidazoles") and the
        # singular the parent compound ("benzimidazole"), a different, narrower term.
        fold = "CHEBI" not in prefixes
        whole = _norm_ws(text).strip(".,;:()[]\"'").lower()
        whole_forms = {whole, _singular(whole)} if fold else {whole}
        onto: dict[str, bool] = {}
        store: dict[str, bool] = {}

        def note(found: dict[str, bool], curie: str, span: str) -> None:
            found[curie] = found.get(curie, False) or span.lower() in whole_forms

        # Members a label lists after "including" / "such as" do not narrow the head term.
        head = _INCLUSION.split(text, maxsplit=1)[0]
        for span in _word_spans(head, fold_plurals=fold):
            if store_kind == "disease":
                for row in self.disease_grounding.lookup(span):
                    if row.object_id and row.object_id != NO_TERM:
                        curie = self._normalize_disease(row.object_id)
                        if curie not in GENERIC_ANCHORS:
                            note(store, curie, span)
            elif store_kind == "drug":
                for curie in self.ingredients.drug_ids_for(span):
                    note(store, curie, span)
            if self.check_terms:
                for prefix in prefixes:
                    if prefix in self._unloadable:
                        continue
                    try:
                        ids = terms.exact_matches(prefix, span)
                    except Exception as exc:  # noqa: BLE001 — reported once below
                        self._unloadable.add(prefix)
                        self._add("WARNING", Path("(terms)"), prefix,
                                  f"cannot load {prefix} for anchoring, so its terms anchor "
                                  f"nothing: {exc}")
                        continue
                    for curie in ids:
                        if curie in GENERIC_ANCHORS or terms.ancestors(curie) & MODIFIER_ROOTS:
                            continue
                        note(onto, curie, span)
        return onto, store

    def _normalize_disease(self, curie: str) -> str:
        norm = self.disease_normalization.lookup(curie)
        return norm.object_id if norm and norm.object_id else curie

    # -- attribution and allergy -------------------------------------------------------
    def _check_attribution(self, path: Path, claim: dict, doc: LabelDocument,
                           names: dict[str, set[str]], product_names: list[str],
                           where: str) -> None:
        """An ingredient is credited only on wording the label uses for *this* claim.

        Judged on the label's own text (the pin), not the quote, so trimming a quote cannot
        change the verdict; and for every place the object occurs in the quote, which must
        all agree. The scope is the object's sentence, extended back to the sentence that
        carries the claim's verb when the object sits in a list under it ("Atorvastatin is
        indicated: ... To reduce the risk of: Myocardial infarction ..."). Product names are
        removed first (a generic name lists every ingredient and attributes nothing).

        - COMPONENT_STATEMENT — the nearest "the X component" before the object, in scope,
          is the attributed ingredient's.
        - INGREDIENT_SUBJECT — the scope opens with the ingredient as the subject of the
          claim's verb ("Clarithromycin is contraindicated in ...").
        - STATED_REASON — a causal cue follows the object in its own sentence, and the
          ingredient is named in the clause after it.
        """
        attributions = claim.get("attributed_to") or []
        if not attributions:
            return
        scopes = _attribution_scopes(doc.sections.get(claim["section"]) or [], claim["quote"],
                                     claim["object"].get("text", ""), product_names)
        if not scopes:
            return  # the quote / object-text checks report this
        for i, attribution in enumerate(attributions):
            awhere = f"{where}.attributed_to[{i}]"
            ingredient, basis = attribution["ingredient"], attribution["basis"]
            if ingredient not in names:
                self.error(path, awhere, f"{ingredient} is not one of this combination's "
                                         "ingredients")
                continue
            ing_names = sorted(names[ingredient])
            for openings, before, after in scopes:
                if basis == "COMPONENT_STATEMENT":
                    nearest = _nearest_component(before, names)
                    if nearest != ingredient:
                        wording = " or ".join(f"'the {n} component'" for n in ing_names)
                        self.error(path, awhere, f"COMPONENT_STATEMENT needs {wording} to govern "
                                                 "this claim: the nearest component phrase "
                                                 f"before {claim['object'].get('text')!r} in its "
                                                 "sentence"
                                                 + (f" is {nearest}'s" if nearest else " is missing"))
                        break
                elif basis == "INGREDIENT_SUBJECT":
                    if not any(_is_sentence_subject(o, ing_names) for o in openings):
                        self.error(path, awhere, "INGREDIENT_SUBJECT needs the object's sentence "
                                                 f"to open with {' / '.join(ing_names)} as its "
                                                 "subject, right before 'is indicated', 'is "
                                                 "contraindicated', 'should not be given', ...")
                        break
                elif basis == "STATED_REASON":
                    if not _named_in_causal_clause(after, ing_names):
                        self.error(path, awhere, "STATED_REASON needs a causal cue ('because', "
                                                 "'due to', 'caused by', ...) after "
                                                 f"{claim['object'].get('text')!r} in its "
                                                 f"sentence, followed by {' / '.join(ing_names)}")
                        break

    def _is_allergy(self, obj: dict) -> bool:
        """Words or hierarchy: "allergy", or a term under a hypersensitivity subtree."""
        term = obj.get("term") or {}
        if _ALLERGY.search(obj.get("text", "")) or _ALLERGY.search(term.get("label", "")):
            return True
        if not term.get("id") or not self.check_terms:
            return False
        try:
            lineage = terms.ancestors(term["id"]) | {term["id"]}
        except Exception:  # noqa: BLE001 — the term check reports an unloadable source
            return False
        return bool(lineage & ALLERGY_ROOTS)

    def _check_allergy(self, path: Path, claim: dict, names: dict[str, set[str]],
                       where: str) -> None:
        """Allergy contraindications name their agents, and not the product's own.

        Allergy to the product's own ingredients is not curated; a cross-sensitivity to other
        agents is, with those agents in ``object_specialization``. Other conditions may name
        the product's own ingredients as their agent ("thrombocytopenia with use of
        trimethoprim"): that is a drug-caused condition, not an allergy, and is curated.
        """
        if claim["relation"] != "CONTRAINDICATED_IN" or not self._is_allergy(claim["object"]):
            return
        specs = claim.get("object_specialization") or []
        if not specs:
            self.error(path, f"{where}.object_specialization",
                       "an allergy contraindication must name the agents it concerns; if they "
                       "are only the product's own ingredients, it is not curated")
            return
        all_names = [n for ns in names.values() for n in ns]

        def own(spec: dict) -> bool:
            term = spec.get("term") or {}
            text = spec.get("text", "").lower()
            return (term.get("id") in names or bool(_OWN_PRODUCT.search(text))
                    or any(_word(n).search(text) for n in all_names))

        own_specs = [s.get("text", "") for s in specs if own(s)]
        if len(own_specs) == len(specs):
            self.error(path, f"{where}.object_specialization",
                       "allergy to the product's own ingredients is not curated; keep only "
                       "cross-sensitivities to other agents")
        elif own_specs:
            self.error(path, f"{where}.object_specialization",
                       f"drop the product's own ingredients ({', '.join(own_specs)}); keep only "
                       "the other agents")

    def _check_polarity_and_wording(self, path: Path, claim: dict, where: str) -> None:
        relation, quote, text = claim["relation"], claim["quote"], claim["object"]["text"]
        cues = _CONTRA_NEGATION_CUES if relation == "CONTRAINDICATED_IN" else _NEGATION_CUES
        neg, total, cue = assertion_negated(text, quote, head_fallback=False, cues=cues)
        if total and neg == total:
            self.warn(path, f"{where}.quote",
                      f"{text!r} follows the negation cue {cue!r}; check the label states "
                      "this claim positively")
        low = quote.lower()
        if relation == "PREVENTATIVE_FOR_CONDITION" and not any(c in low for c in _PREVENT_CUES):
            self.warn(path, f"{where}.relation", "PREVENTATIVE_FOR_CONDITION, but the quote has no "
                                                 "risk-reduction or prevention wording")
        if relation == "AMELIORATES_CONDITION" and not any(c in low for c in _RELIEF_CUES):
            self.warn(path, f"{where}.relation", "AMELIORATES_CONDITION, but the quote has no "
                                                 "relief or symptom wording")
        if relation == "TREATS" and re.search(r"\b(reduc\w* the (combined )?risk|prevent)", low):
            self.warn(path, f"{where}.relation", "TREATS, but the quote reads as "
                                                 "risk reduction; consider PREVENTATIVE_FOR_CONDITION")


#: Descriptor slots on a claim, single- or multi-valued.
SINGLE_DESCRIPTORS = ("object", "disease_context", "anatomical_context", "life_stage", "sex",
                      "line_of_therapy")
MULTI_DESCRIPTORS = ("object_specialization", "in_combination_with",
                     "when_coadministered_with", "examples")


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
        _norm_ws(claim.get("quote", "")).lower(),
    )


def _singular(span: str) -> str:
    """Drop a plural "s" from the last word ("gastric ulcers" -> "gastric ulcer")."""
    head, _, last = span.rpartition(" ")
    if len(last) > 3 and last.endswith("s") and not last.endswith("ss"):
        last = last[:-1]
    return f"{head} {last}".strip()


def _word_spans(text: str, max_words: int = 8, *, fold_plurals: bool = True) -> list[str]:
    """Every contiguous run of words in ``text``, longest first, optionally with singulars."""
    words = [w.strip(".,;:()[]\"'") for w in _norm_ws(text).split()]
    words = [w for w in words if w]
    spans: list[str] = []
    for n in range(min(len(words), max_words), 0, -1):
        for i in range(len(words) - n + 1):
            span = " ".join(words[i:i + n])
            for form in ((span, _singular(span)) if fold_plurals else (span,)):
                if form not in spans:
                    spans.append(form)
    return spans


def _strip_product_names(text: str, product_names: list[str], keep_case: bool = False) -> str:
    """Blank out the product's own names (case-insensitively; ``text`` keeps its case when
    ``keep_case``, otherwise is expected to be lowercased already)."""
    for name in product_names:
        name = _norm_ws(name)
        if len(name) > 3:
            text = re.sub(re.escape(name), " ", text, flags=re.I) if keep_case else \
                text.replace(name.lower(), " ")
    return text


#: A sentence that opens with an ingredient as the subject of the claim's verb. Up to two
#: words may sit between the name and the verb ("clarithromycin tablets are").
_SUBJECT_VERB = (r"(?:\s+(?!and\b|or\b|with\b|plus\b)[\w-]+){0,2}?\s+(?:is|are|should|must)"
                 r"\s+(?:not\s+)?(?:be\s+)?(?:indicated|contraindicated|given|used|administered)\b")


#: An optional heading in front of a sentence's subject, matched on the label's own
#: capitalisation: a numbered heading, a "Heading :" lead-in, or a run of Title-Case words
#: and parentheticals ("Coronary Artery Disease (CAD) Chronic Stable Angina"). A lowercase
#: lead-in ("In patients with CAD, amlodipine is ...") is not a heading.
_HEADING_PREFIX = (r"(?:\d+(?:\.\d+)*\s+)?"
                   r"(?:[^.:;]{1,60}?\s*:\s+"
                   r"|(?:(?:[A-Z][\w’'/-]*|\([^)]{1,40}\)|of|and|or|the|in|with|for|to)\s+){1,12}?)?")


def _is_sentence_subject(opening: str, names: list[str]) -> bool:
    """Does ``opening`` (original case) start, after an optional heading, with one of
    ``names`` as the subject of the claim's verb?"""
    head = opening.lstrip(" ([\"'•")
    return any(re.match(_HEADING_PREFIX + r"(?i:(?:the\s+)?" + re.escape(n) + _SUBJECT_VERB + ")",
                        head)
               for n in names)


#: Tokens whose trailing period is not a sentence end.
_ABBREVIATIONS = frozenset({"u.s", "e.g", "i.e", "approx", "fig", "no", "vs", "dr", "inc",
                            "etc", "al", "mg", "ml", "ph", "st", "ca"})
#: Verbs that carry a claim; a sentence without one is an item in a list under a lead-in.
_CLAIM_VERB = re.compile(r"\b(?:contra)?indicated\b|\b(?:should|must)\s+not\b|\bdo not\b",
                         re.I)


def _sentence_starts(text: str, element_starts: list[int]) -> list[int]:
    """Start offsets of sentences in ``text`` (original case).

    A sentence starts at every label element (a ``<title>`` heading is its own element, so
    "Chronic Stable Angina" does not run into the next sentence), and after a period,
    question or exclamation mark followed by whitespace and a capital, digit, bullet or
    bracket — unless the period ends a known abbreviation. Semicolons do not end sentences:
    lists use them.
    """
    starts = set(element_starts) | {0}
    for m in re.finditer(r"[.!?]\s+(?=[A-Z0-9•(\[])", text):
        token = re.findall(r"[\w.]+$", text[:m.start()])
        if token and token[0].lower().rstrip(".") in _ABBREVIATIONS:
            continue
        starts.add(m.end())
    return sorted(starts)


def _attribution_scopes(chunks: list[str], quote: str, obj: str,
                        product_names: list[str]) -> list[tuple[list[str], str, str]]:
    """For each occurrence of ``obj`` in ``quote``: (candidate openings, text before the
    object within scope, the object's sentence from the object on) — lowercased, product
    names removed. The openings are the scope's start and the start of the semicolon clause
    holding the object ("...agents; Vasospastic Angina: Amlodipine is indicated ...").
    Empty when the quote is not in the section or the object not in the quote."""
    parts = [_norm_ws(c) for c in chunks if _norm_ws(c)]
    text = " ".join(parts)
    element_starts, offset = [], 0
    for part in parts:
        element_starts.append(offset)
        offset += len(part) + 1
    low = text.lower()
    q, o = _norm_ws(quote).lower(), _norm_ws(obj).lower()
    qpos = low.find(q)
    if qpos < 0 or not o:
        return []
    starts = _sentence_starts(text, element_starts)
    scopes = []
    for m in re.finditer(re.escape(o), q):
        opos = qpos + m.start()
        i = max(k for k, st in enumerate(starts) if st <= opos)
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        scope_i = i
        for _ in range(10):  # walk back from a list item to the sentence carrying the verb
            sentence = text[starts[scope_i]:starts[scope_i + 1] if scope_i + 1 < len(starts)
                            else len(text)]
            if _CLAIM_VERB.search(sentence) or scope_i == 0:
                break
            scope_i -= 1
        start = starts[scope_i]
        before = _strip_product_names(low[start:opos], product_names)
        after = _strip_product_names(low[opos:end], product_names)
        # Openings keep the label's capitalisation, which tells a heading from a lead-in.
        opening = _strip_product_names(text[start:end], product_names, keep_case=True)
        clause_start = text.rfind(";", start, opos)
        clause = _strip_product_names(text[clause_start + 1 if clause_start >= 0 else start:end],
                                      product_names, keep_case=True)
        scopes.append(([opening, clause], before, after))
    return scopes


def _nearest_component(before: str, names: dict[str, set[str]]) -> str | None:
    """The ingredient whose "the X component" phrase comes last in ``before``."""
    found = []
    for curie, ing_names in names.items():
        for n in ing_names:
            for m in re.finditer(_word(n).pattern + r"\s+component", before):
                found.append((m.start(), curie))
    return max(found)[1] if found else None


def _named_in_causal_clause(text: str, names: list[str]) -> bool:
    """Does a causal cue in ``text`` have one of ``names`` within its clause after it?"""
    for cue in _CAUSE_CUES.finditer(text):
        clause = re.split(r"[.;]", text[cue.end():], maxsplit=1)[0]
        window = " ".join(clause.split()[:_CAUSE_WINDOW_WORDS])
        if any(_word(n).search(window) for n in names):
            return True
    return False


@lru_cache(maxsize=1)
def _merged_schema():
    """The schema with its imports merged, resolved next to the schema file.

    Handing linkml the path resolves ``imports`` against the working directory instead.
    """
    from linkml_runtime import SchemaView

    view = SchemaView(str(SCHEMA_PATH))
    view.merge_imports()
    return view.schema


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", help="files to check (default: kb/combinations/)")
    parser.add_argument("--no-terms", action="store_true",
                        help="skip ontology term checks (fast local iteration only)")
    parser.add_argument("--offline", action="store_true",
                        help="(kept for compatibility) labels are read from committed pins")
    parser.add_argument("--check-upstream", action="store_true",
                        help="also ask DailyMed whether each label has a newer version")
    args = parser.parse_args(argv)
    files = enter_repo_root(args.files) or sorted(COMBINATIONS_DIR.glob("*.yaml"))
    if not files:
        print("no combination files to check")
        return 0
    try:
        checker = Checker(check_terms=not args.no_terms, offline=args.offline,
                          check_upstream=args.check_upstream)
    except FileNotFoundError as exc:
        print(f"ERROR   {exc}")
        return 1
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

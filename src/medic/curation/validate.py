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
- attributions as evidence (dismech's pattern): a verbatim snippet from the quote's label
  element, in the sentence naming the object (or a list lead governing it), naming the
  ingredient outside the claim's own condition/agent wording and the product's name, with
  the wording its basis stands for, and an explanation; every claim explained; members of
  a coordination that are real expansions of its text; DESI evidence status matching the
  wording before the object;
- an allergy contraindication that names its agents, none of them the product's own;
- source and jurisdiction consistent; at least one valid history record.

WARNINGs are heuristics or gaps a human should look at: a negation cue in front of the
condition, a relation the quote's wording does not suggest, an unusual ontology for a slot,
anchors in another ontology, the same wording bound to different terms, a coordination
without a member or with members its claims leave uncovered, a label revised since the
file pinned its version.
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
        self._check_member_coverage(path, data)

    def _check_consistent_bindings(self, path: Path, data: dict) -> None:
        """The same wording (and member) should bind the same term throughout one file."""
        seen: dict[str, set[str]] = {}
        for product in data.get("products") or []:
            for claim in product.get("claims") or []:
                for _slot, desc in _descriptors(claim):
                    term = (desc.get("term") or {}).get("id")
                    text, member = desc.get("text", ""), desc.get("member")
                    if term and (member or not _coordination_members(text)):
                        key = _norm_ws(text).lower() + (f" [{member}]" if member else "")
                        seen.setdefault(key, set()).add(term)
        for text, ids in sorted(seen.items()):
            if len(ids) > 1:
                self.warn(path, "bindings", f"{text!r} is bound to {sorted(ids)} in different "
                                            "claims; bind one term unless the contexts differ")

    def _check_member_coverage(self, path: Path, data: dict) -> None:
        """Claims that split a coordinated object should cover every member, once each."""
        for p, product in enumerate(data.get("products") or []):
            groups: dict[tuple[str, str], list[str]] = {}
            for claim in product.get("claims") or []:
                obj = claim.get("object") or {}
                if obj.get("member"):
                    key = (_norm_ws(claim.get("quote", "")).lower(),
                           _norm_ws(obj.get("text", "")).lower())
                    groups.setdefault(key, []).append(_norm_ws(obj["member"]).lower())
            for (_quote, text), members in sorted(groups.items()):
                items = (_coordination_items(text) or _coordination_items(text, loose=True))
                covered = {_item_of(m, items) for m in members}
                missing = [" ".join(item) for i, item in enumerate(items) if i not in covered]
                if missing:
                    self.warn(path, f"products[{p}].claims",
                              f"{text!r}: no claim covers {missing} (covered: "
                              f"{sorted(set(members))})")

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
        if not (claim.get("explanation") or "").strip():
            self.error(path, f"{where}.explanation", "explain how the quote states this claim")
        self._check_attribution(path, claim, doc, names, product_names, where)
        self._check_allergy(path, claim, names, where)
        if claim.get("when_coadministered_with") and relation != "CONTRAINDICATED_IN":
            self.error(path, f"{where}.when_coadministered_with",
                       "only a CONTRAINDICATED_IN claim can depend on a co-administered agent")
        for agent in claim.get("when_coadministered_with") or []:
            if (agent.get("term") or {}).get("id") in names:
                self.error(path, f"{where}.when_coadministered_with",
                           f"{agent['term']['id']} is one of this product's own ingredients")
        self._check_evidence_status(path, claim, doc, where)
        if claim["status"] in ("REVIEWED", "REJECTED") and not claim.get("reviewer"):
            self.error(path, f"{where}.status", f"{claim['status']} requires a reviewer")
        self._check_polarity_and_wording(path, claim, where)

    def _check_evidence_status(self, path: Path, claim: dict, doc: LabelDocument,
                               where: str) -> None:
        """DESI wording before a quote and the claim's evidence_status must agree.

        A label can list effective indications and then, after a DESI statement ("... has
        classified the indications as follows: 'Possibly' effective: ..."), the
        less-than-effective ones. So the classification that governs a claim is the nearest
        DESI wording before its object, not any wording anywhere in the section (and not
        before the quote's start: a quote may open with the marker itself).
        """
        if claim["section"] != "INDICATIONS":
            if claim.get("evidence_status"):
                self.error(path, f"{where}.evidence_status", "applies to indications only")
            return
        text = _norm_ws(doc.section_text("INDICATIONS")).lower()
        quote = _norm_ws(claim["quote"]).lower()
        markers = sorted((m.start(), status) for status, pattern in _DESI_WORDING.items()
                         for m in pattern.finditer(text))
        governing = set()
        for at in _object_positions(text, quote, (claim.get("object") or {}).get("text", "")):
            before = [status for start, status in markers if start < at]
            governing.add(before[-1] if before else None)
        if not governing:
            return  # the quote check reports a quote that is not in the section
        status = claim.get("evidence_status")
        if status and status not in governing:
            found = sorted(g for g in governing if g) or ["no DESI classification"]
            self.error(path, f"{where}.evidence_status",
                       f"{status}, but the DESI wording before the quote gives {found}")
        elif not status and None not in governing:
            self.error(path, f"{where}.evidence_status",
                       f"the label classifies this indication as {sorted(governing)} in the "
                       "wording before it; record it in evidence_status")

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
        member = desc.get("member")
        if member is not None:
            members = _coordination_members(desc.get("text", ""), loose=True)
            if _norm_ws(member).lower() not in members:
                self.error(path, f"{where}.member",
                           f"{member!r} is not a member of {desc.get('text')!r}"
                           + (f" (members: {members})" if members else
                              ", which is not a coordination"))
        elif (slot == "object" and _coordination_members(desc.get("text", ""))
              and not _AGENT_ALTERNATIVES.search(desc.get("text", ""))):
            self.warn(path, f"{where}.member", f"{desc.get('text')!r} is a coordination; name "
                                               "the member this claim covers in `member`")
        if slot in ("object", "disease_context") and not member and _is_coordinated_item(
                quote, desc.get("text", "")):
            self.warn(path, f"{where}.text",
                      f"{desc.get('text')!r} is one item of a coordination in the quote; use the "
                      "whole coordinated phrase as text and name this item in `member`")
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
        onto, store = self._anchors(slot, desc.get("member") or desc.get("text", ""))
        if onto or store:
            self.warn(path, where, f"{'member' if desc.get('member') else 'text'} matches {sorted(set(onto) | set(store))}; bind one "
                                   "of them or explain in term_gap why none applies")

    def _check_specificity(self, path: Path, slot: str, desc: dict, where: str) -> None:
        """The bound term against what its text names; against the member, when one is named.

        "renal or hepatic impairment" names two conditions. Each claim quotes the whole
        phrase and says in ``member`` which one it covers ("hepatic impairment"); the term
        and binding are judged against that member. Without a member, the term must fit the
        whole text or one member the validator can judge (one whose words name a term in
        the term's ontology: "renal" alone names nothing, and must not pass any term).
        """
        text, term, binding = desc.get("text", ""), desc["term"], desc.get("binding")
        if desc.get("member"):
            member = desc["member"]
            onto, store = self._anchors(slot, member)
            if not (onto or store) and len(member.split()) == 1:
                self.error(path, f"{where}.member",
                           f"{member!r} names no condition on its own; write the member in full "
                           f"(e.g. with the shared head of {text!r})")
                return
            findings = self._specificity(slot, member, term, binding)
        else:
            findings = self._specificity(slot, text, term, binding)
            prefix = get_prefix(term["id"])
            members = [m for m in _coordination_members(text)
                       if any(get_prefix(a) == prefix
                              for part in self._anchors(slot, m) for a in part)]
            if members and any(level == "ERROR" for level, _, _ in findings):
                per_member = [self._specificity(slot, m, term, binding) for m in members]
                clean = [f for f in per_member
                         if not any(level == "ERROR" for level, _, _ in f)]
                findings = min(clean, key=len) if clean else [(
                    "ERROR", "", f"{term['id']} fits neither {text!r} nor any member of it "
                                 f"({', '.join(repr(m) for m in members)}): "
                                 + "; ".join(msg for f in per_member
                                             for level, _, msg in f if level == "ERROR"))]
        for level, suffix, msg in findings:
            self._add(level, path, f"{where}{suffix}", msg)

    def _specificity(self, slot: str, text: str, term: dict,
                     binding: str | None) -> list[tuple[str, str, str]]:
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
        out: list[tuple[str, str, str]] = []
        onto, store = self._anchors(slot, text)  # id -> names the whole text?
        chosen = term["id"]
        prefix = get_prefix(chosen)
        named = sorted(a for a in set(onto) | set(store) if get_prefix(a) == prefix)
        other = sorted(a for a in set(onto) | set(store) if get_prefix(a) != prefix)
        if not named:
            if other:
                out.append(("WARNING", "", f"text matches {other} in another ontology, not {chosen}; "
                                       "confirm the binding"
                          + (" and consider a semapv:ManualMappingCuration row"
                             if any(a in store for a in other) else "")))
            elif slot in ("object", "disease_context") and self.check_terms:
                out.append(("WARNING", "", f"no term or grounding row matches any part of {text!r}; "
                                       "confirm the binding"))
            return out
        if not self.check_terms:
            if chosen not in named:
                out.append(("WARNING", "", f"text matches {named}, not {chosen}; "
                                       "run with term checks to compare them"))
            return out
        # A longer part of the phrase can name a more specific term than a shorter part:
        # "acute myocardial infarction" contains "myocardial infarction". Check that first,
        # so matching the shorter part never excuses the broader binding.
        narrower = sorted(a for a in named if chosen in terms.ancestors(a))
        if narrower:
            out.append(("ERROR", "", f"{chosen} is broader than the label's own words, which "
                                    f"match {narrower}; bind the more specific term"))
            return out
        chosen_ancestors = terms.ancestors(chosen)
        if chosen in onto:
            if onto[chosen] and binding != "EXACT":
                out.append(("ERROR", ".binding", f"the whole text names {chosen}; "
                                                     f"binding must be EXACT, not {binding}"))
            elif not onto[chosen] and binding == "NARROWER":
                out.append(("ERROR", ".binding", f"{chosen} is what part of the text names, "
                                                     "so it cannot be NARROWER than the text"))
            elif not onto[chosen] and binding == "EXACT":
                extra = self._narrowing_words(text, chosen)
                if extra:
                    out.append(("WARNING", ".binding",
                                f"{chosen} is what only part of {text!r} names; {extra} may "
                                "narrow the condition, making the term BROADER than the text"))
            return out
        if chosen in named:
            return out  # a store row agrees; the store is too coarse to judge the binding
        whole_ancestors = sorted(a for a, whole in onto.items()
                                 if whole and a in chosen_ancestors)
        if whole_ancestors:
            if binding != "NARROWER":
                out.append(("ERROR", ".binding", f"{chosen} is more specific than what the "
                                                     f"whole text names ({whole_ancestors}); "
                                                     f"binding must be NARROWER, not {binding}"))
            return out
        if any(a in chosen_ancestors for a in named):
            return out  # more specific than what part of the words name
        store_targets = sorted(a for a in store if get_prefix(a) == prefix)
        if len(store_targets) == 1 or len(named) == 1:
            # The words name one condition and the curated term is unrelated to it.
            out.append(("ERROR", "", f"the label's words name {named}, which is unrelated to "
                                    f"{chosen}; fix the binding"
                       + (" or add a semapv:ManualMappingCuration row" if store_targets else "")))
        else:
            # Several unrelated anchors means the text lists conditions (a named syndrome,
            # "asthma, rhinitis, and nasal polyps"); the composite term is not contradicted.
            out.append(("WARNING", "", f"text matches {named}, none related to {chosen}; "
                                   "confirm the binding"))
        return out

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
        text = _drop_exhaustive(text)
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
        """An attribution is evidence: the label's own words, checked mechanically.

        dismech's pattern. The snippet must be verbatim from the label element holding the
        claim's quote, in the sentence that names the claim's object (or be a list lead
        ending in ":" that governs it), name the ingredient somewhere other than the claim's
        own condition and agent wording and the product's name, and show the wording its
        basis stands for. An explanation must argue the case. Whether the words really
        attribute the claim is the reviewer's call: four rounds of review showed that
        free-text inference both accepted wrong attributions and rejected right ones.
        """
        chunks = [_norm_ws(c) for c in doc.sections.get(claim["section"]) or []]
        quote = _norm_ws(claim["quote"]).lower()
        element = next((c for c in chunks if quote in c.lower()), None)
        if element is None:  # a quote across elements: the section is the element
            element = _norm_ws(doc.section_text(claim["section"]))
        el = element.lower()
        objects = _object_positions(el, quote, (claim.get("object") or {}).get("text", ""))
        # A condition or agent wording that mentions an ingredient ("aspirin associated
        # gastric ulcers") does not attribute anything. A bare ingredient name is left alone:
        # it is also the subject of "Clarithromycin is contraindicated", and a bare-name
        # snippet fails the basis wording anyway.
        bare = {n for ns in names.values() for n in ns}
        own_words = sorted({t for _s, d in _descriptors(claim)
                            if (t := _norm_ws(d.get("text", "")).lower()) and t not in bare},
                           key=len, reverse=True)
        for i, attribution in enumerate(claim.get("attributed_to") or []):
            awhere = f"{where}.attributed_to[{i}]"
            ingredient, basis = attribution["ingredient"], attribution["basis"]
            snippet = _norm_ws(attribution.get("snippet", ""))
            low = snippet.lower()
            if ingredient not in names:
                self.error(path, awhere, f"{ingredient} is not one of this combination's "
                                         "ingredients")
                continue
            if not (attribution.get("explanation") or "").strip():
                self.error(path, f"{awhere}.explanation", "explain how the snippet ties the "
                                                          "claim to the ingredient")
            if not low:
                self.error(path, f"{awhere}.snippet", "required: the label's words that "
                                                      "attribute the claim")
                continue
            starts = [m.start() for m in re.finditer(re.escape(low), el)]
            if not starts:
                self.error(path, f"{awhere}.snippet", "must be verbatim from the label element "
                                                      "that holds the claim's quote")
                continue
            if not any(_governs(el, at, len(low), obj, element)
                       for at in starts for obj in objects):
                self.error(path, f"{awhere}.snippet",
                           "is not in the sentence that names the claim's object (nor a list "
                           "lead ending in ':' that governs it)")
            stripped = low
            for words in own_words:
                stripped = stripped.replace(words, " ")
            stripped = _strip_product_names(stripped, product_names)
            ingredient_names = sorted(names[ingredient])
            if not any(_word(n).search(stripped) for n in ingredient_names):
                self.error(path, f"{awhere}.snippet",
                           f"never names {' / '.join(ingredient_names)} outside the claim's own "
                           "condition and agent wording and the product's name")
                continue
            before = [el[max(0, at - 30):at] for at in starts]
            self._check_basis(path, claim, basis, stripped, ingredient_names, awhere, before)

    def _check_basis(self, path: Path, claim: dict, basis: str, low: str,
                     ingredient_names: list[str], where: str, before: list[str]) -> None:
        """The snippet shows the kind of statement its basis names, with the ingredient where
        that statement puts it.

        ``low`` is the snippet with the claim's own condition and agent wording and the
        product's name blanked out; ``before`` is the label text just before each place the
        snippet occurs. The ingredient must be the component ("the X component"), the
        subject opening the snippet at a sentence or clause start ("X is contraindicated",
        not "... of X and colchicine is contraindicated"), named after the causal cue (or
        before "therefore"), or a party to the co-administration.
        """
        alternatives = "|".join(re.escape(n) for n in ingredient_names)
        name = rf"\b(?:{alternatives})\b"
        if basis == "COMPONENT_STATEMENT":
            ok = bool(re.search(rf"{name}\s+component\b", low))
            need = '"the <ingredient> component"'
        elif basis == "INGREDIENT_SUBJECT":
            opens = bool(re.match(rf"\s*(?:the\s+)?{name}[^.;:]{{0,40}}?"
                                  r"\b(?:is|are|should|must|may)\b", low))
            # A subject follows a sentence or clause start, a heading or a lead-in comma;
            # never a preposition or conjunction that makes it part of a longer phrase.
            joined = all(re.search(r"\b(?:of|and|or|with|to|by|than|plus|for|in)\s*$", b, re.I)
                         for b in before)
            ok = opens and not joined
            need = ("the snippet to open with the ingredient as the subject of the claim's "
                    "verb, at a sentence or clause start")
        elif basis == "STATED_REASON":
            cue = _CAUSE_CUES.search(low)
            ok = bool((cue and re.search(name, low[cue.end():]))
                      or re.search(rf"{name}.*\btherefore\b", low))
            need = ("the ingredient named after a causal cue (because, due to, ...) or before "
                    '"therefore"')
        elif basis == "INTERACTING_INGREDIENT":
            if not claim.get("when_coadministered_with"):
                self.error(path, f"{where}.basis", "INTERACTING_INGREDIENT needs the other agent "
                                                   "in when_coadministered_with")
            ok = bool(re.search(r"concomitant|co-?administ|coadminist|together with|"
                                r"combination with|use with", low))
            need = "co-administration wording (concomitant, co-administration, ...)"
        else:
            return
        if not ok:
            self.error(path, f"{where}.basis", f"{basis} needs {need}")

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
    key = term.get("id") or _norm_ws(desc.get("text", "")).lower()
    return key + (f" [{_norm_ws(desc['member']).lower()}]" if desc.get("member") else "")


def _claim_key(claim: dict) -> tuple:
    """Two claims are duplicates only when every part of them agrees, context included."""
    return (
        claim.get("relation"),
        _desc_key(claim.get("object")),
        tuple(sorted(_desc_key(d) for d in claim.get("object_specialization") or [])),
        tuple(_desc_key(claim.get(s)) for s in SINGLE_DESCRIPTORS[1:]),
        tuple(sorted(_desc_key(d) for d in claim.get("in_combination_with") or [])),
        tuple(sorted(_desc_key(d) for d in claim.get("when_coadministered_with") or [])),
        tuple(sorted(_desc_key(d) for d in claim.get("examples") or [])),
        tuple(sorted(a.get("ingredient", "") for a in claim.get("attributed_to") or [])),
        str(claim.get("age_range") or ""),
        _norm_ws(claim.get("quote", "")).lower(),
    )


#: Coordinators the validator reads on its own: "or", "and/or", "/", and a comma list that
#: ends in ", or". "and", "&" and bare commas also join names of one syndrome ("asthma,
#: rhinitis, and nasal polyps"), so they count only when a curator names a ``member``.
_COORDINATOR = re.compile(r"\s*,\s*(?:or|and/or)\s+|\s+(?:or|and/or)\s+|(?<=\w)/(?=\w)")
_LOOSE_COORDINATOR = re.compile(
    r"\s*,\s*(?:or|and/or|and)\s+|\s+(?:or|and/or|and|&)\s+|(?<=\w)/(?=\w)|\s*,\s+")
#: Alternatives that together name the whole class: "liver tumors, benign or malignant",
#: "inherited or acquired hypercoagulopathies". Not a coordination: one claim, head term.
_EXHAUSTIVE_PAIRS = {frozenset(p) for p in (
    ("benign", "malignant"), ("inherited", "acquired"), ("hereditary", "acquired"),
    ("congenital", "acquired"), ("arterial", "venous"), ("acute", "chronic"),
    ("fatal", "nonfatal"), ("fatal", "non-fatal"), ("primary", "secondary"))}


def _coordination_items(text: str, *, loose: bool = False) -> list[list[str]]:
    """The coordinated items of ``text`` as word lists, or [] when it is not a coordination."""
    low = _norm_ws(text).lower().strip(".,;:()[]\"'")
    if re.search(r"\bwith or without\b", low):
        return []
    # "liver tumors, benign or malignant, or liver disease": the pair is one class.
    low = re.sub(r"\s*,\s*(?=,)", "", _drop_exhaustive(low)).strip(" ,")
    if not loose and "," in low and not re.search(r",\s*(?:or|and/or)\s", low):
        # "liver tumors, benign or malignant": the comma sets off a modifier, not an item.
        pattern = re.compile(r"\s+(?:or|and/or)\s+|(?<=\w)/(?=\w)")
    elif loose or "," in low:  # "renal, hepatic, or cardiac impairment"
        pattern = _LOOSE_COORDINATOR if loose else re.compile(_COORDINATOR.pattern + r"|\s*,\s+")
    else:
        pattern = _COORDINATOR
    items = [[w.strip(".,;:()[]\"'") for w in part.split()] for part in pattern.split(low)]
    items = [[w for w in item if w] for item in items]
    items = [item for item in items if item]
    # A suspended hyphen borrows the next item's suffix: "estrogen- or progestin-sensitive".
    for item, following in zip(items, items[1:]):
        if item[-1].endswith("-") and "-" in following[0]:
            item[-1] += following[0].split("-", 1)[1]
    if len(items) < 2:
        return []
    if len(items) == 2 and frozenset((items[0][-1], items[1][0])) in _EXHAUSTIVE_PAIRS:
        return []
    return items


#: Alternatives among the agents of a syndrome, not among conditions ("reactivity to aspirin
#: or other NSAIDs").
_AGENT_ALTERNATIVES = re.compile(r"\bto\s+[\w-]+\s+(?:or|and/or)\s+other\b", re.I)


def _is_coordinated_item(quote: str, text: str) -> bool:
    """Whether ``text`` sits in the quote as one item of an "or" coordination: directly
    followed by "or"/"and/or"/"/" and another item, or directly preceded by one."""
    q, t = _norm_ws(quote).lower(), _norm_ws(text).lower()
    if not t:
        return False
    for m in re.finditer(re.escape(t), q):
        after, before = q[m.end():m.end() + 12], q[max(0, m.start() - 10):m.start()]
        if re.match(r"\s*,?\s*(?:or|and/or)\s+\w|/\w", after) or \
                re.search(r"\w\s*,?\s+(?:or|and/or)\s+$|\w/$", before):
            if re.search(r"with or without\s*$", q[max(0, m.start() - 14):m.start()]):
                continue
            # "fatal or nonfatal MI": an exhaustive pair is one class, not two items.
            prev = re.search(r"([\w-]+)\s*,?\s+(?:or|and/or)\s+$", q[:m.start()])
            nxt = re.match(r"\s*,?\s*(?:or|and/or)\s+([\w-]+)", q[m.end():])
            first, last = t.split()[0], t.split()[-1]
            if (prev and frozenset((prev[1], first)) in _EXHAUSTIVE_PAIRS) or \
                    (nxt and frozenset((last, nxt[1])) in _EXHAUSTIVE_PAIRS):
                continue
            return True
    return False


_PAIR = re.compile(r"\b([\w-]+)\s*,?\s+(?:or|and/or)\s+([\w-]+)\b", re.I)


def _drop_exhaustive(text: str) -> str:
    """``text`` without an exhaustive pair: "arterial or venous thrombosis" -> "thrombosis".

    The pair names the whole class, so neither alternative may anchor a narrower term.
    """
    return _norm_ws(_PAIR.sub(
        lambda m: " " if frozenset((m[1].lower(), m[2].lower())) in _EXHAUSTIVE_PAIRS
        else m[0], text)).strip(" ,")


def _item_of(member: str, items: list[list[str]]) -> int | None:
    """Which coordinated item a member expands: the item whose words it contains, longest
    first ("renal impairment" -> "renal"; "severe hepatic impairment" -> "hepatic
    impairment")."""
    words = member.split()

    def contains(item: list[str]) -> bool:
        return any(words[i:i + len(item)] == item for i in range(len(words) - len(item) + 1))
    ranked = sorted(range(len(items)), key=lambda i: -len(items[i]))
    # The first item lends its modifier to later ones; prefer a later item when both fit.
    fits = [i for i in ranked if contains(items[i])]
    later = [i for i in fits if i > 0]
    return (later or fits or [None])[0]


def _coordination_members(text: str, *, loose: bool = False) -> list[str]:
    """The conditions a coordinated phrase names, or [] when it is not one.

    "renal or hepatic impairment" -> renal impairment, hepatic impairment (shared head);
    "hepatic impairment or disease" -> hepatic disease (shared modifier); "severe renal or
    hepatic impairment" -> severe renal impairment, severe hepatic impairment (both);
    "common cold/flu" -> common cold, flu. All readings are returned, the literal items
    among them; a check passes when the bound term fits one of them. Exhaustive pairs
    ("benign or malignant") are not coordinations.
    """
    items = _coordination_items(text, loose=loose)
    if not items:
        return []
    members = [" ".join(item) for item in items]
    first, last = items[0], items[-1]
    for item in items[:-1]:  # shared head: "renal" + "impairment"
        members += [" ".join([*item, *last[k:]]) for k in range(1, len(last))]
    for item in items[1:]:  # shared modifier: "severe" / "hepatic" + "disease"
        members += [" ".join([*first[:j], *item]) for j in range(1, len(first))]
    return list(dict.fromkeys(members))


def _singular(span: str) -> str:
    """Drop a plural "s" from the last word ("gastric ulcers" -> "gastric ulcer")."""
    head, _, last = span.rpartition(" ")
    if len(last) > 4 and last.endswith("ies"):
        last = last[:-3] + "y"  # "hypercoagulopathies", "allergies"
    elif len(last) > 3 and last.endswith("s") and not last.endswith("ss"):
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


#: Candidate sentence ends inside one label element: a period, then an uppercase letter, a
#: digit or "(". A ";" never ends a sentence (label lists use it between items).
_SENTENCE_END = re.compile(r"(?<=\.)\s+(?=[A-Z0-9(])")
#: Abbreviations whose period is not a sentence end ("H. pylori", "e.g. rilpivirine").
_ABBREVIATION = re.compile(r"(?:\b[A-Za-z]|\be\.g|\bi\.e|\bq\.d|\bb\.i\.d|\bt\.i\.d|\bq\.i\.d|"
                           r"\bvs|\bDr|\bNo|\bapprox|\betc)\.$", re.I)
#: Wording that ends a list lead's reach: a DESI classification, a warning.
_LEAD_STOP = re.compile(r"possibly\W{0,3}\s*effective|probably\W{0,3}\s*effective|"
                        r"\bshould not\b|\bdo not use\b", re.I)
#: A sentence that makes a claim of its own; it ends the reach of an earlier list lead.
_CLAIM_SENTENCE = re.compile(r"\b(?:is|are)\s+(?:also\s+)?(?:indicated|contraindicated)\b")


def _object_positions(element: str, quote: str, obj: str) -> list[int]:
    """Where the claim's object text sits in ``element``, inside each copy of the quote.

    All lowercased and whitespace-normalised. Falls back to the quote's start when the
    object text is not inside the quote (the descriptor check reports that).
    """
    obj = _norm_ws(obj).lower()
    out = []
    for q in (m.start() for m in re.finditer(re.escape(quote), element)):
        at = element.find(obj, q, q + len(quote)) if obj else -1
        out.append(at if at >= 0 else q)
    return out


def _governs(element: str, at: int, length: int, obj: int, cased: str | None = None) -> bool:
    """Whether a snippet at ``at`` speaks for the object at ``obj``.

    The same sentence does; so does a list lead ending in ":" ("Atorvastatin is
    indicated:") before the object, up to the next sentence that makes a claim of its own.
    The text is lowercased, so sentence ends are found on the original-case ``cased``
    copy when given; abbreviations ("H. pylori") and ";" never end a sentence.
    """
    source = cased if cased is not None and len(cased) == len(element) else element
    bounds = [0, *(m.start() for m in _SENTENCE_END.finditer(source)
                   if not _ABBREVIATION.search(source[max(0, m.start() - 8):m.start()])),
              len(element) + 1]

    def sentence(pos: int) -> int:
        return max(i for i, b in enumerate(bounds) if b <= pos)

    end = at + length
    if sentence(at) == sentence(obj) or sentence(max(at, end - 1)) == sentence(obj):
        return True
    snippet = element[at:end].rstrip()
    return (snippet.endswith(":") and end <= obj
            and not _CLAIM_SENTENCE.search(element, end, obj)
            and not _LEAD_STOP.search(element, end, obj))


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
    files = [f for path in map(Path, files)
             for f in (sorted(path.glob("*.yaml")) if path.is_dir() else [path])]
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

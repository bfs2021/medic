"""Match cascade: tiered, rung-major / vocab-minor, ambiguity -> unresolved.

Tier order (per atomic literal), first single-id hit wins:
  0 [drugs] salt -> parent   -> closeMatch      (only when the literal ends in a salt word
                                                 and the stripped parent resolves; see
                                                 ``_salt_parent``)
  1 exact (raw)              -> exactMatch
  2 base-normalized          -> exactMatch
  3 surgery variants         -> per-rule predicate
  4 [drugs] INN spelling     -> closeMatch      (family spelling_inn)
  4 [drugs] salt/ester strip -> closeMatch
  4 [diseases] qualifier     -> broadMatch
  5 translation dictionary   -> closeMatch      (family translation)
  6 fuzzy edit-distance-1    -> closeMatch      (family fuzzy; unique-hit guard)
Drugs additionally match ``relatedSynonym`` in every tier (closeMatch). Predicate and
confidence for rule-based matches come from ``RULE_PREDICATE`` / ``RULE_CERTAINTY`` (which
mirror the schema ``PreprocessingRuleEnum`` annotations). Every transform is recorded in
``subject_preprocessing``.
"""

from __future__ import annotations

from medic.grounding.lexical.index import LexicalIndex, LexRow
from medic.grounding.lexical.preprocess import (
    RULE_CERTAINTY,
    RULE_PREDICATE,
    base_normalize,
    cyrillic_transliterate,
    edits1,
    formulation_variants,
    generate_variants,
    has_cyrillic,
    inn_variants,
    qualifier_variants,
    salt_variants,
    split_combination,
    translation_variants,
)
from medic.grounding.store import (
    LEXICAL,
    NO_TERM,
    RXNORM_RULE,
    GroundingDecision,
    LiteralMappingStore,
)

_FIELD_MATCH = {
    "label": "rdfs:label", "exactSynonym": "oio:hasExactSynonym",
    "broadSynonym": "oio:hasBroadSynonym", "narrowSynonym": "oio:hasNarrowSynonym",
    "relatedSynonym": "oio:hasRelatedSynonym",
}
_FIELD_PRED = {"label": "skos:exactMatch", "exactSynonym": "skos:exactMatch",
               "relatedSynonym": "skos:closeMatch"}
_FIELD_CONF = {"label": 1.0, "exactSynonym": 0.95, "relatedSynonym": 0.85}

# Bare inorganic cations / elements. For 'sodium chloride', 'magnesium sulfate', 'ferrous
# sulfate' the salt IS the drug: stripping the anion would ground saline to the sodium
# element, which is the ion problem #95 part 1 is about. `_salt_parent` never resolves one
# of these as a parent.
_INORGANIC_CATIONS = frozenset({
    "sodium", "disodium", "potassium", "calcium", "magnesium", "zinc", "lithium", "iron",
    "ferrous", "ferric", "ammonium", "aluminum", "aluminium", "silver", "bismuth", "copper",
    "cupric", "cuprous", "manganese", "selenium", "chromium", "strontium", "barium", "gold",
    "gallium", "lanthanum", "cerium", "zirconium", "titanium", "cobalt", "nickel",
    "platinum", "mercury", "arsenic", "radium", "thallium", "caesium", "cesium", "rubidium",
})


def _is_bare_cation(stripped: str) -> bool:
    """Is every token of the stripped string an inorganic cation / element word?

    Usually a single token ('sodium'). ``salt_variants`` yields every strip stage, so for
    'calcium acetate hydrate' the bare 'calcium' is skipped and the matcher moves on to
    'calcium acetate', which is the drug. A '<cation> <anion>' residue with the anion
    outside the salt list ('sodium bicarbonate dihydrate' -> 'sodium bicarbonate') is a
    real drug and a fine parent. The all-tokens form also covers double cations such as
    'ferric ammonium' (from 'ferric ammonium citrate').
    """
    toks = stripped.split()
    return bool(toks) and all(t in _INORGANIC_CATIONS for t in toks)


def quality_of(d: GroundingDecision) -> str:
    if d.predicate_id == NO_TERM or d.object_id == NO_TERM:
        return "unresolved"
    # Keyed on the preprocessing rule, not the justification: the justification has to be a
    # legal `semapv:` term (SSSOM conformance) and can no longer carry a MeDIC marker.
    if RXNORM_RULE in (d.subject_preprocessing or ()):
        return "rxnorm_proposed"
    if d.mapping_justification != LEXICAL:
        return "curated"
    if d.subject_preprocessing:
        return "lexical_exact_surgery"
    trimmed = " ".join(d.subject_label.split())
    return "lexical_exact" if d.match_string == trimmed else "lexical_exact_normalized"


class Matcher:
    def __init__(self, index: LexicalIndex, store: LiteralMappingStore,
                 translation: dict[str, str] | None = None):
        self.index = index
        self.store = store
        self.translation = translation or {}
        self.is_drug = index.entity_type == "drugs"
        self.fields = ("label", "exactSynonym") + (("relatedSynonym",) if self.is_drug else ())

    def _vocab_pick(self, rows: list[LexRow]) -> list[LexRow] | None:
        for vocab in self.index.vocab_order:
            v = [r for r in rows if r.source_prefix == vocab]
            if v:
                return v
        return None

    def _of_vocab(self, rows: list[LexRow], vocab: str) -> list[LexRow]:
        return [r for r in rows if r.source_prefix == vocab]

    def _decide(self, subject, rows, confidence, applied, predicate, match_string,
                vocab=None):
        picked = self._of_vocab(rows, vocab) if vocab else self._vocab_pick(rows)
        if not picked:
            return None
        ids = {r.object_id for r in picked}
        if len(ids) != 1:
            return None  # ambiguous within winning vocab
        # Same reasoning as `_fuzzy`: pick by value, not by whatever order the rows arrived in,
        # so `object_label` and `object_match_field` cannot depend on SQL row order.
        r = min(picked, key=lambda x: (x.match_field, x.norm_value))
        return GroundingDecision(
            subject_label=subject, entity_type=self.index.entity_type,
            predicate_id=predicate, object_id=r.object_id, object_label=r.object_label,
            object_match_field=_FIELD_MATCH[r.match_field], mapping_justification=LEXICAL,
            subject_preprocessing=applied, match_string=match_string,
            confidence=round(confidence, 4),
        )

    def _try_base(self, subject, value, column, factor):
        # Vocabulary-major, then field. `VOCAB_ORDER` already prefers CHEBI to DRON, but
        # running field-major consulted that preference only *within* a field — so a DRON
        # clinical-drug form matching on its label was reached before a CHEBI substance
        # matching on an exact synonym, and "Albendazole Tablets" grounded to a dose form
        # (#94). A dose form is the wrong kind of node for a drug-disease edge, and 69 of
        # the 717 ids unknown to the Node Normalizer are DRON.
        lookup = self.index.lookup_raw if column == "raw" else self.index.lookup_norm
        for vocab in self.index.vocab_order:
            for field in self.fields:
                d = self._decide(subject, lookup(value, field),
                                 _FIELD_CONF[field] * factor, [], _FIELD_PRED[field],
                                 value, vocab=vocab)
                if d:
                    return d
        return None

    def _try_rule(self, subject, value, rule_id):
        pred, conf = RULE_PREDICATE[rule_id], RULE_CERTAINTY[rule_id]
        for vocab in self.index.vocab_order:
            for field in self.fields:
                d = self._decide(subject, self.index.lookup_norm(value, field), conf,
                                 [rule_id], pred, value, vocab=vocab)
                if d:
                    return d
        return None

    def _fuzzy(self, subject, norm):
        # `sorted`, not the raw set: `edits1` returns a set, `lookup_norm_many` chunks it into
        # SQL `IN` batches, and `str` hashing is randomised per process (PYTHONHASHSEED). So
        # chunk membership — and therefore the order of `rows` — varied run to run, and
        # `picked[0]` recorded a different `match_string` for the same `object_id`. The id was
        # always stable; the provenance field churned in a git-tracked artifact, against I-2's
        # byte-identical reruns.
        cands = sorted(edits1(norm))
        rows: list[LexRow] = []
        for field in self.fields:
            rows.extend(self.index.lookup_norm_many(cands, field))
        # Same vocabulary-major preference as the exact paths: the substance wins over the
        # dose form even when only the dose form matched on a label (#94).
        picked = self._vocab_pick(rows)
        if not picked:
            return None
        ids = {r.object_id for r in picked}
        if len(ids) != 1:
            return None  # ambiguous edit-1 neighborhood -> skip
        # Explicit tie-break rather than list position: several rows can carry the same
        # object_id via different synonyms, and `match_string` (below) is whichever one wins.
        r = min(picked, key=lambda x: (x.match_field, x.norm_value))
        return GroundingDecision(
            subject_label=subject, entity_type=self.index.entity_type,
            predicate_id=RULE_PREDICATE["fuzzy_edit1_unique"], object_id=r.object_id,
            object_label=r.object_label, object_match_field=_FIELD_MATCH[r.match_field],
            mapping_justification=LEXICAL, subject_preprocessing=["fuzzy_edit1_unique"],
            match_string=r.norm_value, confidence=RULE_CERTAINTY["fuzzy_edit1_unique"],
        )

    def _ladder(self, subject, s):
        """Tiers 2-6 over one normalized working string. Returns a decision or None.

        Reused for the base-normalized string and (when the source is Cyrillic) for its
        transliteration, so transliteration composes with surgery/INN/salt/fuzzy."""
        d = self._try_base(subject, s, "norm", 0.90)             # tier 2: normalized
        if d:
            return d
        for v in generate_variants(s):                           # tier 3: surgery
            d = self._try_rule(subject, v.string, v.applied[0])
            if d:
                return d
        if self.is_drug:                                         # tier 4: drug spelling/salt
            for v in inn_variants(s):
                d = self._try_rule(subject, v.string, v.applied[0])
                if d:
                    return d
            d = self._salt_parent(subject, s)
            if d:
                return d
        else:                                                    # tier 4: disease qualifier
            for v in qualifier_variants(s):
                d = self._try_rule(subject, v.string, "qualifier_strip")
                if d:
                    return d
        for v in translation_variants(s, self.translation):     # tier 5: translation dict
            d = self._try_rule(subject, v.string, "translation_dictionary")
            if d:
                return d
        return self._fuzzy(subject, s)                           # tier 6: fuzzy edit-1

    def _salt_parent(self, subject, norm):
        """[drugs] Resolve a salt form to its parent moiety *before* the exact tiers.

        ChEBI carries its own entry for most salts ('fentanyl citrate' CHEBI:31602 beside
        'fentanyl' CHEBI:119915), so tier 1/2 matched the salt verbatim and the tier-4
        ``salt_ester_strip`` rule never got a turn: 863 salt-form drug nodes sat next to
        their parents in the KGX export, 617 of them duplicating a parent that was also a
        node (#95). The active moiety is what MeDIC means by a drug (DailyMed already grounds
        on ``activeMoiety``), so when the literal ends in a salt word and the stripped
        parent resolves (exact or normalized, via ``lookup_norm``), the parent wins and the
        decision carries the rule: closeMatch, certainty 0.90, surgery-grade quality. When
        only the salt exists in the index ('risedronate sodium', 'carphenazine maleate')
        this returns None and the exact salt match below stands, unchanged.

        Inorganic salts are the exception: for 'sodium chloride', 'potassium chloride',
        'magnesium sulfate', 'calcium acetate', 'ferrous sulfate' the salt is the moiety and
        the stripped residue is a bare cation that ChEBI also carries, so a strip would send
        saline to the sodium element. ``_is_bare_cation`` keeps those on the exact tiers.

        Also the tier-4 salt rung of ``_ladder`` (for the normalized, formulation-stripped
        and transliterated strings), so the cation guard applies there too: an inorganic
        salt the index lacks verbatim must not fall through to its element either.
        """
        for v in salt_variants(norm):
            if _is_bare_cation(v.string):
                continue
            d = self._try_rule(subject, v.string, "salt_ester_strip")
            if d:
                return d
        return None

    def _single(self, subject: str) -> GroundingDecision:
        trimmed = " ".join(subject.split())
        norm = base_normalize(subject)
        if self.is_drug:                                         # tier 0: salt -> parent
            d = self._salt_parent(subject, norm)
            if d:
                return d
        d = self._try_base(subject, trimmed, "raw", 1.0)          # tier 1: exact
        if d:
            return d
        d = self._ladder(subject, norm)
        if d:
            return d
        if self.is_drug:                                         # strip formulation, then re-ladder
            for v in formulation_variants(subject):
                d = self._ladder(subject, base_normalize(v.string))
                if d:
                    d.subject_preprocessing = ["formulation_strip", *d.subject_preprocessing]
                    d.confidence = round(
                        min(d.confidence or 0.0, RULE_CERTAINTY["formulation_strip"]), 4)
                    if d.predicate_id == "skos:exactMatch":  # strip is not an exact match
                        d.predicate_id = "skos:closeMatch"
                    return d
        if has_cyrillic(norm):                                   # transliterate, then re-ladder
            tl = cyrillic_transliterate(norm)
            if tl != norm:
                d = self._ladder(subject, tl)
                if d:
                    d.subject_preprocessing = ["cyrillic_transliteration", *d.subject_preprocessing]
                    d.confidence = round(
                        min(d.confidence or 0.0, RULE_CERTAINTY["cyrillic_transliteration"]), 4)
                    if d.predicate_id == "skos:exactMatch":  # translit is not an exact match
                        d.predicate_id = "skos:closeMatch"
                    return d
        return self._unresolved(subject, norm)

    def _unresolved(self, subject, norm):
        return GroundingDecision(
            subject_label=subject, entity_type=self.index.entity_type, predicate_id=NO_TERM,
            object_id=None, object_label=None, object_match_field=None,
            mapping_justification=LEXICAL, subject_preprocessing=[], match_string=norm,
            confidence=0.0,
        )

    def ground(self, raw: str) -> list[GroundingDecision]:
        """Return one or more decisions for a literal (>1 only for resolved combinations)."""
        locked = self.store.locked_rows(raw)
        if locked:
            return locked
        whole = self._single(raw)
        if whole.object_id is not None:
            return [whole]
        combo = self._combination(raw, split_combination(raw))
        if combo:
            return combo
        # formulation residue may itself be a combination: strip dose/form, then split
        # ('A 500mg / B 30mg Oral Tablet' -> 'a / b' -> [a, b]).
        if self.is_drug:
            for v in formulation_variants(raw):
                combo = self._combination(raw, split_combination(base_normalize(v.string)),
                                          extra=["formulation_strip"])
                if combo:
                    return combo
        return [whole]

    def _combination(self, raw, parts, extra=()):
        """Ground each component of a split combination; all must resolve, else None.

        Components resolving to the *same* id are collapsed (a formulation string often
        repeats one ingredient, e.g. 'X bulk & X tablets' -> ['x','x'] -> one CHEBI id)."""
        if not parts:
            return None
        comps = [self._single(p) for p in parts]
        if not all(c.object_id is not None for c in comps):
            return None
        out, seen = [], set()
        for c in comps:
            if c.object_id in seen:
                continue
            seen.add(c.object_id)
            c.subject_label = raw
            c.subject_preprocessing = [*extra, *c.subject_preprocessing, "combination_split"]
            out.append(c)
        return out

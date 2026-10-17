"""Deterministic string preprocessing for lexical grounding.

Two layers:

- ``base_normalize`` — non-semantic normalization (diacritics, case, whitespace,
  punctuation). Applied to both index strings and query strings. A match that only
  succeeds after this counts as ``lexical_exact_normalized``.
- ``generate_variants`` — minor *semantic* surgery. The rules here are copied and
  adapted from monarch-initiative/mondo PR #10268 (``src/scripts/lexical_variants.py``).
  They are bidirectional and applied query-side, single-pass. A match that only
  succeeds after this counts as ``lexical_exact_surgery``.

We deliberately drop PR #10268's case-preservation machinery (lowercase-skip filter,
mis-cased-noun guard): those exist for human-facing synonym *generation* and are moot
once both sides are uniformly casefolded by ``base_normalize``. We keep the *semantic*
guards that prevent false conversions (roman<->arabic needs an indicator prefix; the
trailing standalone ``X`` is excluded because it is dominated by chromosome usage).
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field

_BRACKETS = re.compile(r"\[[^\]]*\]")
_WS = re.compile(r"\s+")
_DASHES = {"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-"}
_QUOTES = {"’": "'", "‘": "'", "“": '"', "”": '"'}


def base_normalize(s: str) -> str:
    """Non-semantic normalization: diacritics, case, punctuation, whitespace."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))  # strip accents
    for a, b in {**_DASHES, **_QUOTES}.items():
        s = s.replace(a, b)
    s = _BRACKETS.sub("", s)
    s = s.casefold()
    return _WS.sub(" ", s).strip()


@dataclass
class Variant:
    """A generated query variant plus provenance."""

    string: str
    applied: list[str] = field(default_factory=list)
    scope: str = "exact"  # exact | broad | narrow -> SSSOM predicate


# --- roman numeral tables (cap at XII, per PR #10268) ---------------------------------
_ARABIC_TO_ROMAN = {1: "i", 2: "ii", 3: "iii", 4: "iv", 5: "v", 6: "vi",
                    7: "vii", 8: "viii", 9: "ix", 10: "x", 11: "xi", 12: "xii"}
_ROMAN_TO_ARABIC = {v: k for k, v in _ARABIC_TO_ROMAN.items()}
_INDICATORS = "type|stage|grade|class|group|factor"

# British -> American closed list (word-stem, applied on casefolded text)
_BRIT_AM = {
    "tumour": "tumor", "oesophag": "esophag", "haemat": "hemat", "haemo": "hemo",
    "anaemia": "anemia", "leukaemia": "leukemia", "coeliac": "celiac",
    "oedema": "edema", "paediatric": "pediatric", "colour": "color",
    "diarrhoea": "diarrhea", "gynaecolog": "gynecolog",
}
# closed list of cell-type tokens for hyphen<->space (R10)
_CELL_TOKENS = ("t", "b", "nk")


def _r_disease_disorder(s: str) -> list[tuple[str, str]]:
    out = []
    if "disease" in s:
        out.append((s.replace("disease", "disorder"), "disease_to_disorder"))
    if "disorder" in s:
        out.append((s.replace("disorder", "disease"), "disorder_to_disease"))
    return out


def _r_arabic_roman(s: str) -> list[tuple[str, str]]:
    """`<indicator> N` <-> `<indicator> ROMAN`, suffix-anchored, indicator required."""
    out = []
    # arabic -> roman
    m = re.search(rf"\b({_INDICATORS})\s+(\d{{1,2}})\b", s)
    if m and int(m.group(2)) in _ARABIC_TO_ROMAN:
        roman = _ARABIC_TO_ROMAN[int(m.group(2))]
        out.append((s[:m.start(2)] + roman + s[m.end(2):], "arabic_to_roman"))
    # roman -> arabic
    m = re.search(rf"\b({_INDICATORS})\s+([ivx]{{1,4}})\b", s)
    if m and m.group(2) in _ROMAN_TO_ARABIC:
        arabic = str(_ROMAN_TO_ARABIC[m.group(2)])
        out.append((s[:m.start(2)] + arabic + s[m.end(2):], "roman_to_arabic"))
    return out


def _r_comma_drop_type(s: str) -> list[tuple[str, str]]:
    """`X, type N` -> `X type N` (comma-drop only; reverse is unsafe)."""
    m = re.search(r",\s+(type\s+\w+)$", s)
    if m:
        return [(s[:m.start()] + " " + m.group(1), "comma_drop_type")]
    return []


def _r_hyphen_type(s: str) -> list[tuple[str, str]]:
    """`type-N` -> `type N` (unidirectional, per R6)."""
    new = re.sub(r"\btype-(\w+)", r"type \1", s)
    return [(new, "hyphen_type")] if new != s else []


def _r_brit_am(s: str) -> list[tuple[str, str]]:
    out = []
    for brit, am in _BRIT_AM.items():
        if brit in s:
            out.append((s.replace(brit, am), "brit_to_am"))
        elif am in s:
            out.append((s.replace(am, brit), "am_to_brit"))
    return out


def _r_cell_hyphen(s: str) -> list[tuple[str, str]]:
    """`T-cell` <-> `T cell` for the closed token list (R10)."""
    out = []
    for tok in _CELL_TOKENS:
        if re.search(rf"\b{tok}-cell\b", s):
            out.append((re.sub(rf"\b{tok}-cell\b", f"{tok} cell", s), "cell_hyphen_to_space"))
        elif re.search(rf"\b{tok} cell\b", s):
            out.append((re.sub(rf"\b{tok} cell\b", f"{tok}-cell", s), "cell_space_to_hyphen"))
    return out


def _r_strip_other(s: str) -> list[tuple[str, str]]:
    """`other X` -> `X` (broad match)."""
    if s.startswith("other "):
        return [(s[len("other "):], "strip_leading_other")]
    return []


# rule_id -> scope (predicate). Rules not listed default to exact.
_BROAD_RULES = {"strip_leading_other"}

#: Greek letter names as FDA SRS spells them between periods. Closed list: the rule must not
#: fire on an ordinary abbreviation that happens to sit between periods.
_GREEK_LETTERS = (
    "alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta", "iota", "kappa",
    "lambda", "mu", "nu", "xi", "omicron", "pi", "rho", "sigma", "tau", "upsilon", "phi",
    "chi", "psi", "omega",
)
_SRS_GREEK_RE = re.compile(r"\.(" + "|".join(_GREEK_LETTERS) + r")\.", re.I)


def _r_srs_greek(s: str) -> list[tuple[str, str]]:
    """`.alpha.-tocopherol` -> `alpha-tocopherol` (FDA SRS Greek-letter encoding).

    SRS renders Greek letters in substance names as an ASCII-delimited word, so an SPL's
    active moiety is `.ALPHA.-TOCOPHEROL` where ChEBI's label is `alpha-tocopherol`. The
    periods are delimiters carrying no meaning, so this is an encoding fix rather than
    surgery — 89 moiety-name occurrences across 11 distinct names in the current SPL set,
    of which this recovers 51, four of them onto an exact ontology label.
    """
    out = _SRS_GREEK_RE.sub(lambda m: m.group(1).lower(), s)
    return [(out, "srs_greek_encoding")] if out != s else []


_RULES = (
    _r_disease_disorder,
    _r_arabic_roman,
    _r_comma_drop_type,
    _r_hyphen_type,
    _r_brit_am,
    _r_cell_hyphen,
    _r_srs_greek,
    _r_strip_other,
)


def generate_variants(normalized: str) -> list[Variant]:
    """Apply every surgery rule once to ``normalized`` (already base-normalized).

    Single-pass: each rule is applied to the input, results unioned. No recursive
    chaining (bounded, deterministic). Distinct variants only, excluding the input.
    """
    seen: set[str] = set()
    out: list[Variant] = []
    for rule in _RULES:
        for produced, rule_id in rule(normalized):
            produced = _WS.sub(" ", produced).strip()
            if produced and produced != normalized and produced not in seen:
                seen.add(produced)
                scope = "broad" if rule_id in _BROAD_RULES else "exact"
                out.append(Variant(string=produced, applied=[rule_id], scope=scope))
    return out


# --- drug salt/ester stripping (rule id: salt_ester_strip, predicate closeMatch) -----
_SALTS = (
    "sodium", "disodium", "potassium", "calcium", "magnesium", "zinc", "lithium",
    "hydrochloride", "hcl", "dihydrochloride", "hydrobromide", "bromide", "chloride",
    "sulfate", "sulphate", "bisulfate", "acetate", "diacetate", "mesylate", "besylate",
    "maleate", "fumarate", "hemifumarate", "citrate", "dicitrate", "phosphate",
    "diphosphate", "tartrate", "bitartrate", "succinate", "malate", "lactate",
    "gluconate", "stearate", "palmitate", "pamoate", "tosylate", "nitrate", "oxalate",
    "monohydrate", "dihydrate", "trihydrate", "hydrate", "anhydrous",
)
_SALT_RE = re.compile(r"\s+(" + "|".join(_SALTS) + r")$", re.I)


def salt_variants(normalized: str) -> list[Variant]:
    """Strip trailing salt/ester/hydrate words to reach the active moiety (drugs)."""
    s = normalized
    stripped = False
    while True:
        new = _SALT_RE.sub("", s).strip()
        if new == s or not new:
            break
        s, stripped = new, True
    if stripped and s != normalized:
        return [Variant(string=s, applied=["salt_ester_strip"], scope="close")]
    return []


# --- disease qualifier stripping (rule id: qualifier_strip, predicate broadMatch) ----
_QUALIFIERS = (
    "severe", "moderate", "mild", "acute", "chronic", "advanced", "metastatic",
    "recurrent", "refractory", "relapsed", "relapsing", "newly diagnosed", "primary",
    "secondary", "malignant", "active", "persistent", "resistant", "unresectable",
    "locally advanced", "inoperable", "early", "late", "progressive", "generalized",
    "generalised", "localized", "localised",
)
# leading run of qualifiers, optionally joined by 'to'/'and'/'or'/'/'
_QUAL_RE = re.compile(
    r"^((?:" + "|".join(_QUALIFIERS) + r")(?:[\s,/]+(?:to|and|or)?[\s,/]*)?)+", re.I)


def qualifier_variants(normalized: str) -> list[Variant]:
    """Strip a leading clinical-qualifier run ('moderate to severe X' -> 'X')."""
    m = _QUAL_RE.match(normalized)
    if m and m.end() < len(normalized):
        rest = normalized[m.end():].strip()
        if rest and rest != normalized:
            return [Variant(string=rest, applied=["qualifier_strip"], scope="broad")]
    return []


# --- combination splitting (rule id: combination_split) ------------------------------
_COMBO_SPLIT = re.compile(r"\s*(?:;|/|\+|\band\b)\s*", re.I)


def split_combination(raw: str) -> list[str] | None:
    """Split a combination/list literal into components, or None if not a combination.

    Splits on ``; / +`` and the word ``and``. Deliberately NOT on bare commas (too many
    single disease names contain commas, e.g. 'diabetes mellitus, type 2')."""
    parts = [p.strip() for p in _COMBO_SPLIT.split(raw) if p.strip()]
    return parts if len(parts) > 1 else None


# --- formulation stripping (rule id: formulation_strip, predicate closeMatch) --------
# Source *product* strings (esp. India CDSCO) wrap the active ingredient in dose +
# dosage-form + release + pharmacopoeia noise ("Ferrous Sulphate 150mg Sustained Release").
# This deterministic stripper removes that noise; the cleaned residue re-enters the full
# match ladder (matcher.py) so it composes with salt-strip / fuzzy / combination-split,
# exactly like the Cyrillic rule. Closed word lists live in conf/grounding_formulation.yaml.
#
# GUARDS (the hard part):
#   - strength/unit removal is numeric-anchored (a digit must precede the unit), so
#     token-internal meaningful digits survive: 'b12', 'omega-3', 'vitamin d3',
#     '2-deoxy-d-glucose' are NOT stripped (their digits are not "<number><unit>");
#   - form/release/pharmacopoeia words match only on whole-word boundaries, so a real
#     ingredient token can't be clipped by a substring collision;
#   - the rule refuses to return an empty or <3-char residue (that would be a false strip):
#     it returns [] and the original string stays unresolved rather than mis-grounding.

_FORMULATION_CONF = "conf/grounding_formulation.yaml"

# unit / concentration tokens; strength is numeric-anchored (guard: a digit must precede)
_UNIT = r"(?:mg|mcg|microgram|micrograms|µg|ug|gm|kg|ml|meq|mmol|iu|units?|g|l|%)"
_CONC = r"(?:m/?v|w/?v|w/?w|v/?v)"
_STRENGTH_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*" + _CONC + r"?\s*" + _UNIT +
    r"(?:\s*/\s*(?:\d+(?:\.\d+)?\s*)?" + _UNIT + r")*\b", re.I)
_PERCENT_RE = re.compile(r"\b\d+(?:\.\d+)?\s*%\s*" + _CONC + r"?", re.I)
_CONC_BARE_RE = re.compile(r"\b" + _CONC + r"\b", re.I)
_DOSE_LIST_RE = re.compile(r"\b\d+(?:\.\d+)?(?:\s*/\s*\d+(?:\.\d+)?)+\s*" + _UNIT + r"?", re.I)
_PARENS_RE = re.compile(r"\([^)]*\)")
_LIST_MARK_RE = re.compile(r"\b(?:i{1,3}|iv|v|vi{0,3})\)\s*", re.I)  # 'i)Cefaclor ii)...'
# non-ingredient tags dropped in place (not line-truncating): 'FDC of X' -> 'X'
_TAG_RE = re.compile(r"\b(?:fdc\s+of|highly\s+purified)\b", re.I)
# indication/dosage-form annotations truncate everything after them (trailing metadata)
_TRAIL_META_RE = re.compile(
    r"\b(?:add(?:l|itional|\.)?|for\s+(?:paediatric|pediatric|veterinary|human|dog|cat)\b).*$",
    re.I)
_STRAY_RE = re.compile(r"\b(?:q\.?s|multi\s*dose|single\s*use|per\s+day|prefilled|in\s+a|in|for|of)\b", re.I)


def _load_formulation_words(path: str = _FORMULATION_CONF) -> "re.Pattern[str]":
    import yaml
    words: list[str] = []
    if os.path.exists(path):
        with open(path) as fh:
            data = yaml.safe_load(fh) or {}
        for key in ("dosage_forms", "release_qualifiers", "pharmacopoeia"):
            words.extend(str(w) for w in (data.get(key) or []))
    # longest-first so multi-word forms win over their constituents
    words = sorted(set(words), key=len, reverse=True)
    if not words:
        return re.compile(r"(?!x)x")  # matches nothing
    return re.compile(r"\b(?:" + "|".join(re.escape(w) for w in words) + r")\b", re.I)


_FORM_RE: "re.Pattern[str] | None" = None


def _form_re() -> "re.Pattern[str]":
    global _FORM_RE
    if _FORM_RE is None:
        _FORM_RE = _load_formulation_words()
    return _FORM_RE


def strip_formulation(s: str) -> str:
    """Remove dose/form/release/pharmacopoeia noise, returning the ingredient residue.

    Numeric-anchored strength removal + whole-word form/qualifier removal. See GUARDS above.
    The residue is NOT re-normalized here — the matcher feeds it back through the ladder."""
    s = _PARENS_RE.sub(" ", s)
    s = _TAG_RE.sub(" ", s)
    s = _TRAIL_META_RE.sub(" ", s)
    s = _LIST_MARK_RE.sub(" ", s)
    s = _DOSE_LIST_RE.sub(" ", s)
    s = _PERCENT_RE.sub(" ", s)
    s = _STRENGTH_RE.sub(" ", s)
    s = _DOSE_LIST_RE.sub(" ", s)
    s = _PERCENT_RE.sub(" ", s)
    s = _CONC_BARE_RE.sub(" ", s)
    s = _form_re().sub(" ", s)
    s = _STRAY_RE.sub(" ", s)
    # leftover bare dose numbers only when whitespace/edge-delimited (guard: keep 'omega-3',
    # 'd3', 'b12' — a digit glued to a letter or hyphen is part of the ingredient token).
    s = re.sub(r"(?<![-\w])\d+(?:\.\d+)?(?![-\w])", " ", s)
    s = re.sub(r"[.,;&%]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" -/+")
    s = re.sub(r"^(?:and|or)\s+", "", s, flags=re.I)  # trim orphaned conjunctions
    s = re.sub(r"\s+(?:and|or)$", "", s, flags=re.I)
    s = re.sub(r"\b(and|or)\b(?:\s+\1\b)+", r"\1", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip(" -/+")


def formulation_variants(raw: str) -> list[Variant]:
    """Strip formulation noise from a *raw* product string -> one cleaned variant, or [].

    Applied to the raw (pre-normalization) string because dose/form tokens are cased and
    punctuated as they appear in the source. Guard: refuse empty / <3-char / unchanged
    residue (a false strip). The variant re-enters the ladder in matcher._single."""
    stripped = strip_formulation(raw)
    if len(stripped) < 3:
        return []
    if base_normalize(stripped) == base_normalize(raw):
        return []  # nothing meaningful removed
    return [Variant(string=stripped, applied=["formulation_strip"], scope="close")]


# --- foreign INN transliteration rules (family: spelling_inn) -------------------------
# Each produces at most one variant, tagged with its specific rule id. Over-application is
# self-limited: a variant is only accepted if it hits the index (native -in drugs like
# insulin/heparin have no '-ine' form in the vocab, so they can't false-match).
def inn_variants(normalized: str) -> list[Variant]:
    out: list[Variant] = []

    def add(new: str, rule_id: str) -> None:
        new = _WS.sub(" ", new).strip()
        if new and new != normalized and new not in {v.string for v in out}:
            out.append(Variant(string=new, applied=[rule_id], scope="close"))

    if normalized.endswith("in"):
        add(normalized + "e", "inn_suffix_in_to_ine")
    if "z" in normalized:
        add(normalized.replace("z", "s"), "inn_z_to_s")
    if "ph" in normalized:
        add(normalized.replace("ph", "f"), "inn_ph_to_f")
    if "ti" in normalized:
        add(re.sub(r"ti", "thi", normalized), "inn_ti_to_thi")
    if "ae" in normalized or "oe" in normalized:
        add(normalized.replace("ae", "e").replace("oe", "e"), "inn_ae_oe_to_e")
    return out


# --- Cyrillic transliteration (family: transliteration) ------------------------------
# Deterministic Russian->Latin map, oriented to drug INNs. Applied only when the string
# contains Cyrillic; the transliterated form then re-enters the full match ladder (so it
# composes with the INN spelling rules and fuzzy edit-1). Recovers ~17% of GRLS names.
_CYRILLIC_MAP = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}


def has_cyrillic(s: str) -> bool:
    return any("Ѐ" <= c <= "ӿ" for c in s)


def cyrillic_transliterate(s: str) -> str:
    return "".join(_CYRILLIC_MAP.get(c, _CYRILLIC_MAP.get(c.lower(), c)) for c in s)


# --- translation dictionary (family: translation) ------------------------------------
def load_translation_dict(path: str) -> dict[str, str]:
    import yaml
    with open(path) as fh:
        data = yaml.safe_load(fh) or {}
    return {base_normalize(k): v for k, v in (data.get("terms") or {}).items()}


def translation_variants(normalized: str, table: dict[str, str]) -> list[Variant]:
    """Whole-string curated translation ('foreign' -> English)."""
    hit = table.get(normalized)
    if hit:
        return [Variant(string=base_normalize(hit), applied=["translation_dictionary"], scope="close")]
    return []


def llm_translation_variants(normalized: str, enabled: bool = False) -> list[Variant]:
    """Deferred, review-only LLM translation. Disabled by default (non-deterministic)."""
    if not enabled:
        return []
    raise NotImplementedError("translation_llm is a deferred, review-only proposer (see spec §11)")


# --- fuzzy edit-distance-1 candidate generation (family: fuzzy) -----------------------
_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789 "


def edits1(word: str) -> set[str]:
    """All strings one insertion/deletion/substitution/transposition from ``word``."""
    splits = [(word[:i], word[i:]) for i in range(len(word) + 1)]
    out: set[str] = set()
    for a, b in splits:
        if b:
            out.add(a + b[1:])                       # deletion
        if len(b) > 1:
            out.add(a + b[1] + b[0] + b[2:])         # transposition
        for c in _ALPHABET:
            out.add(a + c + b)                       # insertion
            if b:
                out.add(a + c + b[1:])               # substitution
    out.discard(word)
    return out


# --- rule metadata (MUST mirror PreprocessingRuleEnum annotations in grounding.yaml) --
# tests/test_grounding_schema.py asserts these equal the schema; keep in sync.
RULE_CERTAINTY = {
    "base_normalization": 1.0,
    "comma_drop_type": 0.98, "hyphen_type": 0.98,
    "cell_hyphen_to_space": 0.97, "cell_space_to_hyphen": 0.97,
    "disease_to_disorder": 0.95, "disorder_to_disease": 0.95,
    "arabic_to_roman": 0.95, "roman_to_arabic": 0.95, "strip_leading_other": 0.70,
    "brit_to_am": 0.97, "am_to_brit": 0.97,
    "inn_suffix_in_to_ine": 0.90, "inn_z_to_s": 0.88, "inn_ph_to_f": 0.88,
    "inn_ti_to_thi": 0.85, "inn_ae_oe_to_e": 0.90,
    "salt_ester_strip": 0.90, "qualifier_strip": 0.75, "combination_split": 0.92,
    "srs_greek_encoding": 0.98,
    "formulation_strip": 0.80,
    "cyrillic_transliteration": 0.75,
    "fuzzy_edit1_unique": 0.60,
    "translation_dictionary": 0.85, "translation_llm": 0.50,
    "deepl_translation": 0.85,
    "rxnorm_resolve": 0.70,
}
RULE_PREDICATE = {
    "base_normalization": "skos:exactMatch",
    "comma_drop_type": "skos:exactMatch", "hyphen_type": "skos:exactMatch",
    "cell_hyphen_to_space": "skos:exactMatch", "cell_space_to_hyphen": "skos:exactMatch",
    "disease_to_disorder": "skos:exactMatch", "disorder_to_disease": "skos:exactMatch",
    "arabic_to_roman": "skos:exactMatch", "roman_to_arabic": "skos:exactMatch",
    "strip_leading_other": "skos:broadMatch",
    "brit_to_am": "skos:exactMatch", "am_to_brit": "skos:exactMatch",
    "inn_suffix_in_to_ine": "skos:closeMatch", "inn_z_to_s": "skos:closeMatch",
    "inn_ph_to_f": "skos:closeMatch", "inn_ti_to_thi": "skos:closeMatch",
    "inn_ae_oe_to_e": "skos:closeMatch",
    "salt_ester_strip": "skos:closeMatch", "qualifier_strip": "skos:broadMatch",
    "srs_greek_encoding": "skos:exactMatch",
    "combination_split": "skos:exactMatch", "formulation_strip": "skos:closeMatch",
    "cyrillic_transliteration": "skos:closeMatch",
    "fuzzy_edit1_unique": "skos:closeMatch",
    "translation_dictionary": "skos:closeMatch", "translation_llm": "skos:closeMatch",
    "deepl_translation": "skos:closeMatch",
    "rxnorm_resolve": "skos:closeMatch",
}

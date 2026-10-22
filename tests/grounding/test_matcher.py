import sqlite3

from medic.grounding.lexical.build import open_index
from medic.grounding.lexical.index import COLS
from medic.grounding.lexical.matcher import Matcher
from medic.grounding.lexical.preprocess import base_normalize
from medic.grounding.store import LiteralMappingStore


def _fixture_db(path, rows):
    con = sqlite3.connect(path)
    con.execute(f"CREATE TABLE lex ({', '.join(c + ' TEXT' for c in COLS)})")
    con.executemany(f"INSERT INTO lex VALUES ({', '.join('?' * len(COLS))})", rows)
    con.execute("CREATE INDEX ix_raw ON lex (raw_value, match_field)")
    con.execute("CREATE INDEX ix_norm ON lex (norm_value, match_field)")
    con.commit()
    con.close()


def _r(oid, value, field="label", prefix=None):
    prefix = prefix or oid.split(":")[0]
    return (oid, value, value, value.strip(), base_normalize(value), field, "exact", prefix)


def _matcher(tmp_path, rows, entity="diseases"):
    db = tmp_path / f"{entity}.db"
    _fixture_db(str(db), rows)
    store = LiteralMappingStore(str(tmp_path / "g.sssom.tsv"), entity)
    store.load()
    return Matcher(open_index(entity, str(db)), store)


def _one(matcher, name):
    ds = matcher.ground(name)
    return ds[0]


def test_exact_label(tmp_path):
    m = _matcher(tmp_path, [_r("MONDO:0007947", "Marfan syndrome")])
    d = _one(m, "Marfan syndrome")
    assert d.object_id == "MONDO:0007947" and d.grounding_quality == "lexical_exact"


def test_normalized_when_case_differs(tmp_path):
    m = _matcher(tmp_path, [_r("MONDO:0007947", "Marfan syndrome")])
    d = _one(m, "MARFAN   SYNDROME")
    assert d.object_id == "MONDO:0007947" and d.grounding_quality == "lexical_exact_normalized"


def test_vocab_major_synonym_mondo_beats_exact_label_hp(tmp_path):
    """Inverted deliberately (#96). This used to assert the opposite — that an HP *label*
    beats a MONDO *exact synonym* — because the loop ran fields outermost and consulted the
    configured `VOCAB_ORDER` only within one field.

    That is the defect #96 reports: a UMLS/HP label beating a MONDO exact synonym is how
    `hypertension` grounded to HP:0000822, and UMLS-grounded indication edges were wrong
    38.6% of the time against 22.1% for MONDO. `VOCAB_ORDER` names MONDO first; the loop now
    honours it across fields instead of only within one.

    The field order still applies inside the winning vocabulary, and the recorded
    confidence and `object_match_field` still reflect the field that actually matched, so a
    synonym win is not written down as a label match.
    """
    m = _matcher(tmp_path, [
        _r("MONDO:1", "foo bar", field="exactSynonym"),
        _r("HP:2", "foo bar", field="label")])
    assert _one(m, "foo bar").object_id == "MONDO:1"


def test_surgery_disease_to_disorder(tmp_path):
    m = _matcher(tmp_path, [_r("MONDO:3", "heart disorder")])
    d = _one(m, "heart disease")
    assert d.object_id == "MONDO:3" and d.subject_preprocessing == ["disease_to_disorder"]
    assert d.grounding_quality == "lexical_exact_surgery"


def test_ambiguous_is_unresolved(tmp_path):
    m = _matcher(tmp_path, [_r("MONDO:4", "xyz"), _r("MONDO:5", "xyz")])
    d = _one(m, "xyz")
    assert d.grounding_quality == "unresolved" and d.predicate_id == "sssom:NoTermFound"


def test_disease_does_not_match_related_synonym(tmp_path):
    # related-synonym matching is drugs-only
    m = _matcher(tmp_path, [_r("MONDO:6", "acyclovir foo", field="relatedSynonym")])
    assert _one(m, "acyclovir foo").object_id is None


def test_drug_related_synonym_closematch(tmp_path):
    m = _matcher(tmp_path, [_r("CHEBI:2453", "aciclovir", field="relatedSynonym")], entity="drugs")
    d = _one(m, "aciclovir")
    assert d.object_id == "CHEBI:2453"
    assert d.predicate_id == "skos:closeMatch"
    assert d.object_match_field == "oio:hasRelatedSynonym"


def test_drug_salt_strip(tmp_path):
    m = _matcher(tmp_path, [_r("CHEBI:2676", "amoxicillin")], entity="drugs")
    d = _one(m, "amoxicillin sodium")
    assert d.object_id == "CHEBI:2676"
    assert d.subject_preprocessing == ["salt_ester_strip"]
    assert d.predicate_id == "skos:closeMatch"


def test_drug_salt_prefers_parent_over_exact_salt_entry(tmp_path):
    # ChEBI has both 'fentanyl citrate' and 'fentanyl'. Before #95 the exact tier matched
    # the salt verbatim and the strip rule never fired; the parent moiety must win.
    m = _matcher(tmp_path, [_r("CHEBI:119915", "fentanyl"),
                            _r("CHEBI:31602", "fentanyl citrate")], entity="drugs")
    d = _one(m, "FENTANYL CITRATE")
    assert d.object_id == "CHEBI:119915"
    assert d.subject_preprocessing == ["salt_ester_strip"]
    assert d.predicate_id == "skos:closeMatch"
    assert d.confidence == 0.90
    # surgery-grade, not an exact quality: reliability may not auto-score it HIGH
    assert d.grounding_quality == "lexical_exact_surgery"


def test_drug_inorganic_salt_is_not_stripped_to_the_element(tmp_path):
    # The salt IS the drug: saline must not become the sodium element even though ChEBI
    # has 'sodium' (#95 part 1).
    m = _matcher(tmp_path, [_r("CHEBI:26710", "sodium chloride"), _r("CHEBI:26708", "sodium"),
                            _r("CHEBI:32599", "magnesium sulfate"),
                            _r("CHEBI:25107", "magnesium")], entity="drugs")
    for name, oid in (("sodium chloride", "CHEBI:26710"), ("magnesium sulfate", "CHEBI:32599")):
        d = _one(m, name)
        assert d.object_id == oid
        assert d.subject_preprocessing == []
        assert d.predicate_id == "skos:exactMatch" and d.grounding_quality == "lexical_exact"
    d = _one(m, "MAGNESIUM SULFATE")
    assert d.object_id == "CHEBI:32599" and d.grounding_quality == "lexical_exact_normalized"


def test_drug_inorganic_hydrate_strips_to_the_salt_not_the_element(tmp_path):
    # 'calcium acetate hydrate' -> 'calcium acetate' -> 'calcium': the first strip is a
    # legitimate parent, the second is a bare cation and must be skipped.
    m = _matcher(tmp_path, [_r("CHEBI:3310", "calcium acetate"), _r("CHEBI:22984", "calcium")],
                 entity="drugs")
    d = _one(m, "calcium acetate hydrate")
    assert d.object_id == "CHEBI:3310" and d.subject_preprocessing == ["salt_ester_strip"]
    assert _one(m, "calcium acetate").object_id == "CHEBI:3310"


def test_drug_inorganic_salt_absent_from_index_does_not_fall_to_element(tmp_path):
    # The guard also covers the tier-4 ladder rung: 'potassium chloride' with no verbatim
    # entry stays unresolved rather than grounding to the potassium element.
    m = _matcher(tmp_path, [_r("CHEBI:26216", "potassium")], entity="drugs")
    assert _one(m, "potassium chloride").object_id is None
    assert _one(m, "Potassium Chloride 20mEq Tablet").object_id is None


def test_drug_salt_without_parent_keeps_exact_salt_match(tmp_path):
    # Only the salt exists (risedronate sodium, carphenazine maleate): unchanged behaviour.
    m = _matcher(tmp_path, [_r("CHEBI:72994", "risedronate sodium")], entity="drugs")
    d = _one(m, "risedronate sodium")
    assert d.object_id == "CHEBI:72994"
    assert d.subject_preprocessing == []
    assert d.predicate_id == "skos:exactMatch"
    assert d.grounding_quality == "lexical_exact"
    d = _one(m, "RISEDRONATE SODIUM")
    assert d.object_id == "CHEBI:72994" and d.grounding_quality == "lexical_exact_normalized"


def test_drug_two_salts_collapse_to_one_parent(tmp_path):
    m = _matcher(tmp_path, [_r("CHEBI:6904", "metoprolol"),
                            _r("CHEBI:6906", "metoprolol tartrate"),
                            _r("CHEBI:6905", "metoprolol succinate")], entity="drugs")
    tart = _one(m, "METOPROLOL TARTRATE")
    succ = _one(m, "METOPROLOL SUCCINATE")
    assert tart.object_id == succ.object_id == "CHEBI:6904"
    assert tart.subject_preprocessing == succ.subject_preprocessing == ["salt_ester_strip"]


def test_drug_salt_parent_first_per_combination_component(tmp_path):
    # Orange Book multi-ingredient strings go through the split; each component gets the
    # same parent-first treatment, and a component with no parent keeps its salt id.
    m = _matcher(tmp_path, [_r("CHEBI:6904", "metoprolol"),
                            _r("CHEBI:6906", "metoprolol tartrate"),
                            _r("CHEBI:3654", "chlorthalidone")], entity="drugs")
    ds = m.ground("CHLORTHALIDONE; METOPROLOL TARTRATE")
    by_id = {d.object_id: d for d in ds}
    assert set(by_id) == {"CHEBI:3654", "CHEBI:6904"}
    assert by_id["CHEBI:6904"].subject_preprocessing == ["salt_ester_strip", "combination_split"]
    assert by_id["CHEBI:3654"].subject_preprocessing == ["combination_split"]


def test_disease_salt_like_word_is_unaffected(tmp_path):
    # salt stripping is drugs-only: a disease label ending in a salt word still matches
    # verbatim even when a shorter label exists.
    m = _matcher(tmp_path, [_r("MONDO:8", "hypersensitivity to sodium"),
                            _r("MONDO:9", "hypersensitivity to")])
    d = _one(m, "hypersensitivity to sodium")
    assert d.object_id == "MONDO:8"
    assert d.subject_preprocessing == [] and d.grounding_quality == "lexical_exact"


def test_disease_qualifier_strip_broadmatch(tmp_path):
    m = _matcher(tmp_path, [_r("MONDO:7", "plaque psoriasis")])
    d = _one(m, "moderate to severe plaque psoriasis")
    assert d.object_id == "MONDO:7"
    assert d.subject_preprocessing == ["qualifier_strip"]
    assert d.predicate_id == "skos:broadMatch"


def test_combination_split(tmp_path):
    m = _matcher(tmp_path, [_r("CHEBI:2676", "amoxicillin"), _r("CHEBI:3729", "clavulanic acid")],
                 entity="drugs")
    ds = m.ground("amoxicillin; clavulanic acid")
    assert {d.object_id for d in ds} == {"CHEBI:2676", "CHEBI:3729"}
    assert all("combination_split" in d.subject_preprocessing for d in ds)


def test_formulation_strip_single(tmp_path):
    m = _matcher(tmp_path, [_r("CHEBI:5855", "ibuprofen")], entity="drugs")
    d = _one(m, "Ibuprofen 200mg Tablet")
    assert d.object_id == "CHEBI:5855"
    assert d.subject_preprocessing == ["formulation_strip"]
    assert d.predicate_id == "skos:closeMatch"


def test_formulation_strip_composes_with_salt(tmp_path):
    # strip dose/form -> 'diclofenac sodium' -> salt-strip -> 'diclofenac'
    m = _matcher(tmp_path, [_r("CHEBI:47381", "diclofenac")], entity="drugs")
    d = _one(m, "Diclofenac Sodium 1gm Gel")
    assert d.object_id == "CHEBI:47381"
    assert d.subject_preprocessing == ["formulation_strip", "salt_ester_strip"]
    assert d.predicate_id == "skos:closeMatch"


def test_formulation_strip_then_combination_split(tmp_path):
    # residue is itself a combination: strip -> 'netupitant + palonosetron' -> split
    m = _matcher(tmp_path, [_r("CHEBI:85155", "netupitant"), _r("CHEBI:85161", "palonosetron")],
                 entity="drugs")
    ds = m.ground("Netupitant 300 mg + Palonosetron 0.5 mg Capsule")
    assert {d.object_id for d in ds} == {"CHEBI:85155", "CHEBI:85161"}
    for d in ds:
        assert d.subject_preprocessing == ["formulation_strip", "combination_split"]
        assert d.predicate_id == "skos:closeMatch"


def test_formulation_strip_confidence_capped(tmp_path):
    m = _matcher(tmp_path, [_r("CHEBI:5855", "ibuprofen")], entity="drugs")
    d = _one(m, "Ibuprofen 200mg Tablet")
    assert d.confidence <= 0.80  # capped at formulation_strip certainty


def test_drug_inn_suffix_in_to_ine(tmp_path):
    m = _matcher(tmp_path, [_r("CHEBI:9137", "sibutramine")], entity="drugs")
    d = _one(m, "Sibutramin")
    assert d.object_id == "CHEBI:9137"
    assert d.subject_preprocessing == ["inn_suffix_in_to_ine"]
    assert d.predicate_id == "skos:closeMatch"


def test_drug_inn_z_to_s(tmp_path):
    m = _matcher(tmp_path, [_r("CHEBI:135762", "methylprednisolone")], entity="drugs")
    d = _one(m, "Methylprednizolone")
    assert d.object_id == "CHEBI:135762" and d.subject_preprocessing == ["inn_z_to_s"]


def test_translation_dictionary(tmp_path):
    m = _matcher(tmp_path, [_r("CHEBI:1", "benzoyl peroxide")], entity="drugs")
    m.translation = {"benzoid peroxide": "benzoyl peroxide"}
    d = _one(m, "Benzoid peroxide")
    assert d.object_id == "CHEBI:1" and d.subject_preprocessing == ["translation_dictionary"]


def test_fuzzy_edit1_unique(tmp_path):
    m = _matcher(tmp_path, [_r("CHEBI:66903", "vismodegib")], entity="drugs")
    d = _one(m, "Vismodegi")  # one deletion away, unique
    assert d.object_id == "CHEBI:66903"
    assert d.subject_preprocessing == ["fuzzy_edit1_unique"]
    assert d.predicate_id == "skos:closeMatch"


def test_cyrillic_transliteration_composes(tmp_path):
    # Cyrillic -> transliterate -> exact-match the Latin form
    m = _matcher(tmp_path, [_r("CHEBI:66876", "avanafil")], entity="drugs")
    d = _one(m, "Аванафил")
    assert d.object_id == "CHEBI:66876"
    assert "cyrillic_transliteration" in d.subject_preprocessing
    assert d.predicate_id == "skos:closeMatch"


def test_cyrillic_transliteration_composes_with_fuzzy(tmp_path):
    # Абакавир -> abakavir -> (fuzzy edit-1) -> abacavir
    m = _matcher(tmp_path, [_r("CHEBI:421707", "abacavir")], entity="drugs")
    d = _one(m, "Абакавир")
    assert d.object_id == "CHEBI:421707"
    assert d.subject_preprocessing == ["cyrillic_transliteration", "fuzzy_edit1_unique"]


def test_fuzzy_edit1_ambiguous_is_skipped(tmp_path):
    # "xat" is edit-1 from both "cat" and "bat" -> ambiguous -> unresolved
    m = _matcher(tmp_path, [_r("CHEBI:1", "cat"), _r("CHEBI:2", "bat")], entity="drugs")
    assert _one(m, "xat").object_id is None

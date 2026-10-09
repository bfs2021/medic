"""Curated combination files: identity, the deterministic checks, history and Biolink references.

Each check gets a known-bad case, so a check that silently stops firing fails here. The label
is a small synthetic SPL modelled on the aspirin/omeprazole label, and ontology lookups go
through a small fake term source, so the whole file runs offline.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from medic.curation import history, terms
from medic.curation.identity import combination_id, combination_name, combination_slug
from medic.curation.terms import TermInfo
from medic.curation.validate import Checker

REPO = Path(__file__).resolve().parent.parent
SETID = "00000000-test-4000-8000-combination01"
ASPIRIN, OMEPRAZOLE = "CHEBI:15365", "CHEBI:7772"
SLUG = "acetylsalicylic-acid-omeprazole"

SPL = f"""<?xml version="1.0" encoding="UTF-8"?>
<document xmlns="urn:hl7-org:v3">
  <setId root="{SETID}"/>
  <versionNumber value="3"/>
  <title>TESTCOMBO (aspirin and omeprazole) delayed-release tablets</title>
  <component><structuredBody>
    <component><section>
      <code code="34067-9" codeSystem="2.16.840.1.113883.6.1"/>
      <title>1 INDICATIONS AND USAGE</title>
      <text><paragraph>The aspirin component of TESTCOMBO is indicated for reducing the
        combined risk of death and nonfatal MI in patients with a previous MI or unstable
        angina pectoris. The omeprazole component of TESTCOMBO is indicated for decreasing
        the risk of developing aspirin associated gastric ulcers in patients at risk for
        developing aspirin associated gastric ulcers due to age (&#8805; 55). TESTCOMBO is
        also indicated after acute myocardial infarction. Aspirin and Omeprazole Tablets are
        indicated for the relief of heartburn.</paragraph>
        <paragraph>Limitations of Use: Not for use as the initial dose of aspirin
        therapy during onset of acute coronary syndrome.</paragraph></text>
    </section></component>
    <component><section>
      <code code="34070-3" codeSystem="2.16.840.1.113883.6.1"/>
      <title>4 CONTRAINDICATIONS</title>
      <text><paragraph>TESTCOMBO is contraindicated in patients with known allergy to aspirin
        and other NSAIDs. TESTCOMBO is contraindicated in patients with known
        hypersensitivity to aspirin, omeprazole, substituted benzimidazoles, or to any of the
        excipients. TESTCOMBO is contraindicated in pediatric patients with suspected viral
        infections, with or without fever, because of the risk of Reye's syndrome with
        concomitant use of aspirin.</paragraph></text>
    </section></component>
  </structuredBody></component>
  <subject><manufacturedProduct><manufacturedProduct>
    <name>Aspirin and Omeprazole Tablets</name>
    <ingredient classCode="ACTIB"><ingredientSubstance>
      <activeMoiety><activeMoiety><name>ASPIRIN</name></activeMoiety></activeMoiety>
    </ingredientSubstance></ingredient>
    <ingredient classCode="ACTIB"><ingredientSubstance>
      <activeMoiety><activeMoiety><name>OMEPRAZOLE</name></activeMoiety></activeMoiety>
    </ingredientSubstance></ingredient>
  </manufacturedProduct></manufacturedProduct></subject>
</document>
"""

MI_QUOTE = ("The aspirin component of TESTCOMBO is indicated for reducing the combined risk "
            "of death and nonfatal MI in patients with a previous MI or unstable angina pectoris")
ULCER_QUOTE = ("The omeprazole component of TESTCOMBO is indicated for decreasing the risk of "
               "developing aspirin associated gastric ulcers")
NSAID_QUOTE = "TESTCOMBO is contraindicated in patients with known allergy to aspirin and other NSAIDs"
OWN_QUOTE = ("TESTCOMBO is contraindicated in patients with known hypersensitivity to aspirin, "
             "omeprazole, substituted benzimidazoles, or to any of the excipients")
VIRAL_QUOTE = ("TESTCOMBO is contraindicated in pediatric patients with suspected viral "
               "infections, with or without fever, because of the risk of Reye's syndrome with "
               "concomitant use of aspirin")
ACUTE_QUOTE = "TESTCOMBO is also indicated after acute myocardial infarction"
GENERIC_QUOTE = "Aspirin and Omeprazole Tablets are indicated for the relief of heartburn"


# --- a fake term source: just enough ontology for the checks ------------------------------
_TERMS = {t.id: t for t in [
    TermInfo("MONDO:0024643", "myocardial disorder"),
    TermInfo("MONDO:0005068", "myocardial infarction", exact_synonyms=["MI"],
             parents=["MONDO:0024643"]),
    TermInfo("MONDO:0004781", "acute myocardial infarction", parents=["MONDO:0005068"]),
    TermInfo("MONDO:0004247", "peptic ulcer disease"),
    TermInfo("MONDO:0001126", "gastric ulcer", parents=["MONDO:0004247"]),
    TermInfo("MONDO:0000605", "hypersensitivity reaction disease",
             exact_synonyms=["hypersensitivity"]),
    TermInfo("MONDO:0005271", "allergic disease", exact_synonyms=["allergy"],
             parents=["MONDO:0000605"]),
    TermInfo("MONDO:0000775", "drug allergy", parents=["MONDO:0005271"]),
    TermInfo("MONDO:0005550", "infectious disease", exact_synonyms=["infection"]),
    TermInfo("MONDO:0005108", "viral infectious disease", parents=["MONDO:0005550"]),
    TermInfo("MONDO:0000001", "retired thing", obsolete=True, replaced_by="MONDO:0005068"),
    TermInfo("HP:0001681", "Angina pectoris"),
    TermInfo("CHEBI:15365", "acetylsalicylic acid"),
    TermInfo("CHEBI:7772", "omeprazole"),
    TermInfo("CHEBI:35475", "non-steroidal anti-inflammatory drug"),
    TermInfo("CHEBI:22715", "benzimidazoles"),
    TermInfo("CHEBI:36622", "benzimidazole", parents=["CHEBI:22715"]),
]}


class FakeSource:
    def lookup(self, curie):
        return _TERMS.get(curie)

    def ancestors(self, curie):
        seen, stack = set(), list(_TERMS[curie].parents) if curie in _TERMS else []
        while stack:
            p = stack.pop()
            if p not in seen:
                seen.add(p)
                stack.extend(_TERMS[p].parents if p in _TERMS else [])
        return seen

    def exact_matches(self, prefix, text):
        low = text.lower()
        return tuple(t.id for t in _TERMS.values()
                     if t.id.startswith(prefix + ":") and not t.obsolete
                     and low in [t.label.lower(), *(s.lower() for s in t.exact_synonyms)])

    def search(self, prefix, query, limit=15):
        return []


def _term(curie: str) -> dict:
    return {"id": curie, "label": _TERMS[curie].label}


def _valid_record() -> dict:
    ids = [ASPIRIN, OMEPRAZOLE]
    labels = ["acetylsalicylic acid", "omeprazole"]
    return {
        "id": combination_id(ids),
        "name": combination_name(labels),
        "ingredients": [{"id": i, "label": lbl} for i, lbl in zip(ids, labels)],
        "curation_status": "DRAFT",
        "products": [{
            "source": "DAILYMED",
            "jurisdiction": "USA",
            "document": {"setid": SETID, "version": 3, "title": "TESTCOMBO"},
            "product_name": "TESTCOMBO",
            "claims": [
                {
                    "relation": "PREVENTS",
                    "section": "INDICATIONS",
                    "quote": MI_QUOTE,
                    "object": {"text": "nonfatal MI", "term": _term("MONDO:0005068"),
                               "binding": "EXACT"},
                    "attributed_to": [{"ingredient": ASPIRIN, "basis": "COMPONENT_STATEMENT"}],
                    "disease_context": {"text": "unstable angina pectoris",
                                        "term_gap": "no unstable-angina term in the test ontology"},
                    "status": "DRAFT",
                },
                {
                    "relation": "PREVENTS",
                    "section": "INDICATIONS",
                    "quote": ULCER_QUOTE,
                    "object": {"text": "aspirin associated gastric ulcers",
                               "term": _term("MONDO:0001126"), "binding": "BROADER"},
                    "object_specialization": [{"text": "aspirin", "term": _term(ASPIRIN),
                                               "binding": "EXACT"}],
                    "attributed_to": [{"ingredient": OMEPRAZOLE,
                                       "basis": "COMPONENT_STATEMENT"}],
                    "status": "DRAFT",
                },
                {
                    "relation": "CONTRAINDICATED",
                    "section": "CONTRAINDICATIONS",
                    "quote": NSAID_QUOTE,
                    "object": {"text": "allergy", "term": _term("MONDO:0000775"),
                               "binding": "NARROWER"},
                    "object_specialization": [{"text": "NSAIDs", "term": _term("CHEBI:35475"),
                                               "binding": "EXACT"}],
                    "status": "DRAFT",
                },
                {
                    "relation": "CONTRAINDICATED",
                    "section": "CONTRAINDICATIONS",
                    "quote": VIRAL_QUOTE,
                    "object": {"text": "suspected viral infections",
                               "term": _term("MONDO:0005108"), "binding": "EXACT"},
                    "life_stage": {"text": "pediatric patients",
                                   "term_gap": "not checked in the test ontology"},
                    "attributed_to": [{"ingredient": ASPIRIN, "basis": "STATED_REASON"}],
                    "status": "DRAFT",
                },
            ],
        }],
    }


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A repo-shaped working directory: real decision stores, a cached label, a fake ontology."""
    (tmp_path / "mappings").symlink_to(REPO / "mappings")
    label_dir = tmp_path / "cache" / "labels" / "dailymed"
    label_dir.mkdir(parents=True)
    (label_dir / f"{SETID}.v3.xml").write_text(SPL)
    (tmp_path / "kb" / "combinations").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    fake = FakeSource()
    monkeypatch.setattr(terms, "source", lambda prefix: fake)
    return tmp_path


def _write(workspace: Path, record: dict, slug: str = SLUG, with_history: bool = True) -> Path:
    path = Path("kb/combinations") / f"{slug}.yaml"
    path.write_text(yaml.safe_dump(record, sort_keys=False))
    if with_history:
        history.main(["new", "--kind", "combination", "--slug", slug, "--event", "CREATE",
                      "--summary", "Create", "--model", "claude-opus-5-5[1m]",
                      "--agent-tool", "claude-code", "--details", "test"])
    return path


def _findings(path: Path) -> tuple[list[str], list[str]]:
    checker = Checker(check_terms=True, offline=True)
    checker.check_file(path)
    errors = [f"{f.where}: {f.message}" for f in checker.findings if f.level == "ERROR"]
    warnings = [f"{f.where}: {f.message}" for f in checker.findings if f.level == "WARNING"]
    return errors, warnings


def _claim_errors(workspace, index, mutate) -> tuple[list[str], list[str]]:
    record = _valid_record()
    mutate(record["products"][0]["claims"][index])
    return _findings(_write(workspace, record))


# --- identity -----------------------------------------------------------------------------
def test_identity_is_order_independent():
    assert combination_id([ASPIRIN, OMEPRAZOLE]) == combination_id([OMEPRAZOLE, ASPIRIN])
    assert combination_id([ASPIRIN, OMEPRAZOLE]).startswith("MEDICNE:combo-")
    assert combination_name(["omeprazole", "acetylsalicylic acid"]) == \
        "acetylsalicylic acid / omeprazole"
    assert combination_slug(["omeprazole", "acetylsalicylic acid"]) == SLUG


def test_valid_file_has_no_errors(workspace):
    errors, _ = _findings(_write(workspace, _valid_record()))
    assert errors == []


def test_hand_written_identity_is_an_error(workspace):
    record = _valid_record()
    record["id"] = "MEDICNE:combo-made-up"
    record["name"] = "aspirin / omeprazole"
    errors, _ = _findings(_write(workspace, record))
    assert any(e.startswith("id:") for e in errors)
    assert any(e.startswith("name:") for e in errors)


def test_file_name_is_computed(workspace):
    errors, _ = _findings(_write(workspace, _valid_record(), slug="aspirin-omeprazole"))
    assert any("file name" in e for e in errors)


def test_ingredient_set_must_match_the_label(workspace):
    record = _valid_record()
    record["ingredients"] = [{"id": ASPIRIN, "label": "acetylsalicylic acid"},
                             {"id": "CHEBI:2549", "label": "albuterol"}]
    record["id"] = combination_id([ASPIRIN, "CHEBI:2549"])
    record["name"] = combination_name(["acetylsalicylic acid", "albuterol"])
    for claim in record["products"][0]["claims"]:
        claim.pop("attributed_to", None)
    errors, _ = _findings(_write(workspace, record, slug="acetylsalicylic-acid-albuterol"))
    assert any("must equal the label's active ingredients" in e for e in errors)


# --- quotes and descriptor text -----------------------------------------------------------
def test_paraphrased_quote_is_an_error(workspace):
    errors, _ = _claim_errors(workspace, 0, lambda c: c.update(
        quote="Aspirin reduces the risk of death and nonfatal MI"))
    assert any("not a verbatim substring" in e for e in errors)


def test_quote_from_limitations_of_use_is_an_error(workspace):
    def mutate(c):
        c.update(quote="Not for use as the initial dose of aspirin therapy during onset of "
                       "acute coronary syndrome", attributed_to=[],
                 object={"text": "acute coronary syndrome",
                         "term_gap": "not in the test ontology"})
    errors, _ = _claim_errors(workspace, 0, mutate)
    assert any("Limitations of Use" in e for e in errors)


def test_contraindication_must_quote_the_contraindications_section(workspace):
    errors, _ = _claim_errors(workspace, 0, lambda c: c.update(relation="CONTRAINDICATED"))
    assert any("CONTRAINDICATIONS section" in e for e in errors)


def test_descriptor_text_must_be_inside_the_quote(workspace):
    errors, _ = _claim_errors(workspace, 0, lambda c: c["object"].update(text="heart attack"))
    assert any("object.text" in e for e in errors)
    errors, _ = _claim_errors(workspace, 0, lambda c: c["disease_context"].update(text="CAD"))
    assert any("disease_context.text" in e for e in errors)


# --- descriptor binding -------------------------------------------------------------------
def test_unbound_descriptor_needs_a_term_gap(workspace):
    errors, _ = _claim_errors(workspace, 0, lambda c: c["disease_context"].pop("term_gap"))
    assert any("term_gap" in e for e in errors)


def test_bound_descriptor_needs_a_binding(workspace):
    errors, _ = _claim_errors(workspace, 0, lambda c: c["object"].pop("binding"))
    assert any("binding" in e for e in errors)


def test_obsolete_and_mislabelled_terms_are_errors(workspace):
    errors, _ = _claim_errors(workspace, 0, lambda c: c["object"].update(
        term={"id": "MONDO:0000001", "label": "retired thing"}))
    assert any("obsolete" in e for e in errors)
    errors, _ = _claim_errors(workspace, 0, lambda c: c["object"].update(
        term={"id": "MONDO:0005068", "label": "heart attack"}))
    assert any("is labelled" in e for e in errors)


def test_hp_context_is_allowed(workspace):
    errors, warnings = _claim_errors(workspace, 0, lambda c: c["disease_context"].update(
        text="angina pectoris", term=_term("HP:0001681"), binding="EXACT"))
    assert errors == []
    assert not any("usually uses" in w for w in warnings)


# --- specificity: no broader than the label's own words -----------------------------------
def test_term_broader_than_the_words_is_an_error(workspace):
    def mutate(c):
        c.update(quote=ACUTE_QUOTE, attributed_to=[], disease_context=None,
                 object={"text": "acute myocardial infarction",
                         "term": _term("MONDO:0005068"), "binding": "BROADER"})
    errors, _ = _claim_errors(workspace, 0, mutate)
    assert any("broader than the label's own words" in e for e in errors)


def test_term_more_specific_than_the_words_is_allowed(workspace):
    errors, _ = _claim_errors(workspace, 0, lambda c: c["object"].update(
        term=_term("MONDO:0004781"), binding="NARROWER"))
    assert errors == []


def test_term_unrelated_to_a_store_row_is_an_error(workspace):
    # The grounding store maps "MI" to myocardial infarction.
    errors, _ = _claim_errors(workspace, 0, lambda c: c["object"].update(
        term=_term("MONDO:0001126")))
    assert any("unrelated" in e for e in errors)


def test_store_row_in_another_ontology_is_only_a_warning(workspace):
    errors, warnings = _claim_errors(workspace, 0, lambda c: c["object"].update(
        term=_term("HP:0001681"), binding="BROADER"))
    assert errors == []
    assert any("another ontology" in w for w in warnings)


# --- attribution and allergy --------------------------------------------------------------
def test_attribution_must_name_an_ingredient(workspace):
    errors, _ = _claim_errors(workspace, 0, lambda c: c.update(
        attributed_to=[{"ingredient": "CHEBI:2549", "basis": "COMPONENT_STATEMENT"}]))
    assert any("not one of this combination's ingredients" in e for e in errors)


def test_component_statement_needs_the_component_wording(workspace):
    errors, _ = _claim_errors(workspace, 0, lambda c: c.update(
        attributed_to=[{"ingredient": OMEPRAZOLE, "basis": "COMPONENT_STATEMENT"}]))
    assert any("'the omeprazole component'" in e for e in errors)


def test_product_name_is_not_an_attribution(workspace):
    """Review repro A: a generic product name names every ingredient and attributes nothing."""
    def mutate(c):
        c.update(quote=GENERIC_QUOTE, relation="RELIEVES_SYMPTOMS", disease_context=None,
                 object={"text": "heartburn", "term_gap": "not in the test ontology"},
                 attributed_to=[{"ingredient": ASPIRIN, "basis": "COMPONENT_STATEMENT"}])
    errors, _ = _claim_errors(workspace, 0, mutate)
    assert any("COMPONENT_STATEMENT needs" in e for e in errors)

    def as_reason(c):
        mutate(c)
        c["attributed_to"] = [{"ingredient": ASPIRIN, "basis": "STATED_REASON"}]
    errors, _ = _claim_errors(workspace, 0, as_reason)
    assert any("STATED_REASON needs" in e for e in errors)


def test_second_ingredient_mentioned_in_the_quote_is_not_attributed(workspace):
    """Review repro B: "aspirin associated" in the omeprazole claim is not an aspirin claim."""
    errors, _ = _claim_errors(workspace, 1, lambda c: c["attributed_to"].append(
        {"ingredient": ASPIRIN, "basis": "COMPONENT_STATEMENT"}))
    assert any("'the aspirin component'" in e for e in errors)


def test_stated_reason_needs_the_ingredient_in_the_causal_clause(workspace):
    errors, _ = _claim_errors(workspace, 3, lambda c: c.update(
        attributed_to=[{"ingredient": OMEPRAZOLE, "basis": "STATED_REASON"}]))
    assert any("STATED_REASON needs" in e for e in errors)
    errors, _ = _claim_errors(workspace, 2, lambda c: c.update(
        attributed_to=[{"ingredient": ASPIRIN, "basis": "STATED_REASON"}]))
    assert any("STATED_REASON needs" in e for e in errors)


def test_own_ingredient_allergy_is_not_curated(workspace):
    def mutate(c):
        c.update(quote=OWN_QUOTE, object={"text": "hypersensitivity",
                                          "term_gap": "no hypersensitivity term in the test"},
                 object_specialization=[
                     {"text": "aspirin", "term": _term(ASPIRIN), "binding": "EXACT"},
                     {"text": "any of the excipients", "term_gap": "excipients have no term"}])
    errors, _ = _claim_errors(workspace, 2, mutate)
    assert any("own ingredients is not curated" in e for e in errors)


def test_cross_sensitivity_is_curated(workspace):
    errors, _ = _findings(_write(workspace, _valid_record()))
    assert not any("own ingredients" in e for e in errors)


# --- other claim checks -------------------------------------------------------------------
def test_reviewed_claim_needs_a_reviewer(workspace):
    errors, _ = _claim_errors(workspace, 0, lambda c: c.update(status="REVIEWED"))
    assert any("requires a reviewer" in e for e in errors)


def test_claims_differing_only_in_context_are_not_duplicates(workspace):
    record = _valid_record()
    claims = record["products"][0]["claims"]
    second = copy.deepcopy(claims[0])
    second["disease_context"] = {"text": "previous MI", "term": _term("MONDO:0005068"),
                                 "binding": "EXACT"}
    claims.insert(1, second)
    _, warnings = _findings(_write(workspace, record))
    assert not any("duplicate" in w for w in warnings)
    claims.insert(1, copy.deepcopy(second))
    _, warnings = _findings(_write(workspace, record, with_history=False))
    assert any("duplicate" in w for w in warnings)


def test_negation_cue_is_flagged(workspace):
    def mutate(c):
        c.update(relation="TREATS", section="INDICATIONS", life_stage=None,
                 quote="Not for use as the initial dose of aspirin therapy during onset of "
                       "acute coronary syndrome",
                 object={"text": "acute coronary syndrome", "term_gap": "not in the test"})
    _, warnings = _claim_errors(workspace, 3, mutate)
    assert any("negation cue" in w for w in warnings)


# --- history -------------------------------------------------------------------------------
def test_missing_history_is_an_error(workspace):
    errors, _ = _findings(_write(workspace, _valid_record(), with_history=False))
    assert any(e.startswith("history:") for e in errors)


def test_history_requires_a_model_for_ai_agents(workspace):
    _write(workspace, _valid_record(), with_history=False)
    with pytest.raises(SystemExit):
        history.main(["new", "--kind", "combination", "--slug", SLUG, "--summary", "x"])


def test_history_record_is_valid_and_records_the_bare_model_id(workspace):
    _write(workspace, _valid_record())
    records = history.records_for(f"kb/combinations/{SLUG}.yaml")
    assert len(records) == 1
    assert history.check_record(records[0]) == []
    data = yaml.safe_load(records[0].read_text())
    assert data["session"]["actors"][0]["model"] == "claude-opus-5-5"


# --- terms ---------------------------------------------------------------------------------
def test_obo_parser_reads_labels_obsolescence_synonyms_and_parents():
    parsed = terms.parse_obo("""format-version: 1.2

[Term]
id: MONDO:0005068
name: myocardial infarction
synonym: "heart attack" EXACT []
synonym: "MI" RELATED []
is_a: MONDO:0024643 ! myocardial disorder

[Term]
id: MONDO:0000001
name: obsolete thing
is_obsolete: true
replaced_by: MONDO:0005068

[Typedef]
id: part_of
name: part of
""".splitlines(keepends=True))
    assert parsed["MONDO:0005068"].label == "myocardial infarction"
    assert parsed["MONDO:0005068"].exact_synonyms == ["heart attack"]
    assert parsed["MONDO:0005068"].parents == ["MONDO:0024643"]
    assert parsed["MONDO:0000001"].obsolete and parsed["MONDO:0000001"].replaced_by
    assert "part_of" not in parsed


def test_term_source_is_configurable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert terms.source_selector("MONDO") == "obo"
    (tmp_path / "conf").mkdir()
    (tmp_path / "conf" / "curation_terms.yaml").write_text(
        "default: obo\nsources:\n  HP: ols:hp\n")
    assert terms.source_selector("HP") == "ols:hp"
    monkeypatch.setenv("MEDIC_TERM_SOURCE_MONDO", "sqlite:obo:mondo")
    assert terms.source_selector("MONDO") == "sqlite:obo:mondo"


# --- Biolink references -----------------------------------------------------------------------
def test_every_biolink_reference_in_the_schema_exists():
    """Semantic references only — but each must name a real term in the pinned release."""
    from medic.export.kgx import biolink as bl

    schema = yaml.safe_load((REPO / "src/medic/schema/combination.yaml").read_text())
    refs: list[tuple[str, str]] = []
    for enum in schema["enums"].values():
        for pv in (enum.get("permissible_values") or {}).values():
            if (pv or {}).get("meaning", "").startswith("biolink:"):
                refs.append(("predicate", pv["meaning"]))
    for slot in schema["slots"].values():
        for key in ("exact_mappings", "close_mappings", "related_mappings"):
            refs += [("slot", c) for c in (slot or {}).get(key, []) if c.startswith("biolink:")]
    assert refs
    for kind, curie in refs:
        assert bl.model_name(curie) in bl.biolink_slots(), curie
        if kind == "predicate":
            assert bl.is_predicate(curie), f"{curie} is not a Biolink predicate"


def test_relation_enum_covers_every_relation_the_validator_knows():
    from medic.curation.validate import RELATION_SECTION

    schema = yaml.safe_load((REPO / "src/medic/schema/combination.yaml").read_text())
    assert set(schema["enums"]["ClaimRelationEnum"]["permissible_values"]) == \
        set(RELATION_SECTION)


# --- review round 1: reproductions that used to pass ------------------------------------------
def _own_allergy(c, specs):
    c.update(quote=OWN_QUOTE,
             object={"text": "hypersensitivity", "term": _term("MONDO:0000775"),
                     "binding": "NARROWER"},
             object_specialization=specs)


def test_allergy_without_agents_is_an_error(workspace):
    """Review repro E: dropping the specializations used to skip the own-ingredient rule."""
    errors, _ = _claim_errors(workspace, 2, lambda c: _own_allergy(c, None))
    assert any("must name the agents" in e for e in errors)


def test_untermed_own_ingredient_is_still_own(workspace):
    errors, _ = _claim_errors(workspace, 2, lambda c: _own_allergy(
        c, [{"text": "omeprazole", "term_gap": "left unbound on purpose"}]))
    assert any("own ingredients is not curated" in e for e in errors)


def test_mixed_allergy_list_keeps_only_other_agents(workspace):
    errors, _ = _claim_errors(workspace, 2, lambda c: _own_allergy(c, [
        {"text": "aspirin", "term": _term(ASPIRIN), "binding": "EXACT"},
        {"text": "substituted benzimidazoles", "term": _term("CHEBI:22715"),
         "binding": "EXACT"}]))
    assert any("drop the product's own ingredients" in e for e in errors)
    errors, _ = _claim_errors(workspace, 2, lambda c: _own_allergy(c, [
        {"text": "substituted benzimidazoles", "term": _term("CHEBI:22715"),
         "binding": "EXACT"}]))
    assert errors == []


def test_chemical_class_plurals_are_not_folded(workspace):
    """"benzimidazoles" is the class; folding it to "benzimidazole" (the compound) is wrong."""
    errors, _ = _claim_errors(workspace, 2, lambda c: _own_allergy(c, [
        {"text": "benzimidazoles", "term": _term("CHEBI:22715"), "binding": "EXACT"}]))
    assert not any("broader than the label's own words" in e for e in errors)


def test_context_term_must_agree_with_its_text(workspace):
    """Review repro G: a context is checked against its own words, like the object."""
    errors, _ = _claim_errors(workspace, 0, lambda c: c.update(disease_context={
        "text": "previous MI", "term": _term("MONDO:0001126"), "binding": "EXACT"}))
    assert any("disease_context" in e and "unrelated" in e for e in errors)


def test_specialization_term_must_agree_with_its_text(workspace):
    """Review repro J: "aspirin" bound to omeprazole."""
    errors, _ = _claim_errors(workspace, 1, lambda c: c["object_specialization"][0].update(
        term=_term(OMEPRAZOLE)))
    assert any("object_specialization" in e and "unrelated" in e for e in errors)


def test_unconfigured_ontology_is_an_error(workspace):
    """Review repro B (finding 4): an uncheckable term used to skip every check."""
    errors, _ = _claim_errors(workspace, 0, lambda c: c["object"].update(
        term={"id": "NCIT:C2991", "label": "Whatever I like"}))
    assert any("no term source is configured" in e for e in errors)


def test_plural_text_still_anchors(workspace):
    errors, _ = _claim_errors(workspace, 1, lambda c: c["object"].update(
        term=_term("MONDO:0004247")))
    assert any("broader than the label's own words" in e for e in errors)


def test_binding_must_match_what_the_words_name(workspace):
    """Review finding 5: the binding value used to be unchecked."""
    # "allergy" names allergic disease outright: EXACT, not BROADER.
    errors, _ = _claim_errors(workspace, 2, lambda c: c["object"].update(
        term=_term("MONDO:0005271"), binding="BROADER"))
    assert any("must be EXACT" in e for e in errors)
    # drug allergy is more specific than what "allergy" names: NARROWER, not EXACT.
    errors, _ = _claim_errors(workspace, 2, lambda c: c["object"].update(binding="EXACT"))
    assert any("must be NARROWER" in e for e in errors)
    # "MI" is only part of "nonfatal MI": its term cannot be NARROWER than the text.
    errors, _ = _claim_errors(workspace, 0, lambda c: c["object"].update(binding="NARROWER"))
    assert any("cannot be NARROWER" in e for e in errors)


def test_part_of_text_anchor_does_not_dictate_binding(workspace):
    """"infection" in "suspected viral infections" names infectious disease; BROADER or EXACT
    for the whole text is the curator's call, not forced to NARROWER."""
    errors, _ = _claim_errors(workspace, 3, lambda c: c["object"].update(binding="BROADER"))
    assert not any("binding" in e for e in errors)


def test_ingredient_label_must_be_the_resolvers(workspace):
    """Review finding 6: identity is computed from labels, so the labels are checked."""
    record = _valid_record()
    record["ingredients"][0]["label"] = "aspirin"
    record["name"] = combination_name(["aspirin", "omeprazole"])
    errors, _ = _findings(_write(workspace, record, slug="aspirin-omeprazole"))
    assert any("must be labelled 'acetylsalicylic acid'" in e for e in errors)


def test_one_file_per_combination(workspace):
    record = _valid_record()
    first = _write(workspace, record)
    second = Path("kb/combinations/copy.yaml")
    second.write_text(first.read_text())
    checker = Checker(check_terms=True, offline=True)
    checker.check_file(first)
    checker.check_file(second)
    assert any("same combination as" in f.message for f in checker.findings
               if f.level == "ERROR")


def test_curator_reference_must_resolve(workspace):
    import subprocess

    record = _valid_record()
    record["curators"] = [{"curator_type": "AI_AGENT", "name": "skill",
                           "curator_id": "UNCOMMITTED skill"}]
    errors, _ = _findings(_write(workspace, record))
    assert any("commit-pinned blob URL" in e for e in errors)

    skill = Path(".claude/skills/medic-combination-curation/SKILL.md")
    skill.parent.mkdir(parents=True)
    skill.write_text("skill text\n")
    for cmd in (["init", "-q"], ["add", str(skill)],
                ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "skill"]):
        subprocess.run(["git", *cmd], check=True, capture_output=True)
    rev = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                         text=True).stdout.strip()
    url = f"https://github.com/monarch-initiative/medic/blob/{{}}/{skill.as_posix()}"
    record["curators"][0]["curator_id"] = url.format(rev)
    errors, _ = _findings(_write(workspace, record, with_history=False))
    assert not any("curator_id" in e for e in errors)
    record["curators"][0]["curator_id"] = url.format("0" * 40)
    errors, _ = _findings(_write(workspace, record, with_history=False))
    assert any("does not exist at commit" in e for e in errors)


def test_history_rejects_runtime_suffixed_model_ids(workspace):
    _write(workspace, _valid_record())
    record = history.records_for(f"kb/combinations/{SLUG}.yaml")[0]
    data = yaml.safe_load(record.read_text())
    data["session"]["actors"][0]["model"] = "claude-opus-5-5[1m]"
    record.write_text(yaml.safe_dump(data, sort_keys=False))
    assert any("runtime suffix" in p for p in history.check_record(record))


def test_missing_decision_store_fails_loudly(tmp_path, monkeypatch):
    """Review finding 8: from the wrong directory the stores used to load empty."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError, match="decision store"):
        Checker(check_terms=False, offline=True)


def test_labels_are_cached_by_version(workspace):
    from medic.curation.label import fetch_label

    assert fetch_label(SETID, version=3, offline=True).version == 3
    # A pin that is not cached falls back to the newest cached copy; the validator warns.
    record = _valid_record()
    record["products"][0]["document"]["version"] = 2
    _, warnings = _findings(_write(workspace, record))
    assert any("which is not cached" in w for w in warnings)


def test_enter_repo_root_resolves_paths_first(tmp_path, monkeypatch):
    from medic.curation.paths import REPO_ROOT, enter_repo_root

    monkeypatch.chdir(REPO_ROOT / "kb")
    assert enter_repo_root(["combinations/x.yaml"]) == [Path("kb/combinations/x.yaml")]
    assert Path.cwd() == REPO_ROOT

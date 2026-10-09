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
        indicated for the relief of heartburn. TESTCOMBO is indicated for complicated urinary
        tract infections including pyelonephritis.</paragraph>
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
        concomitant use of aspirin. TESTCOMBO is contraindicated in patients with a history of
        angioedema. TESTCOMBO is contraindicated in patients with severe renal impairment.
        TESTCOMBO is contraindicated after gastric bypass because of poor absorption of Aspirin
        and Omeprazole Tablets. TESTCOMBO is contraindicated in patients with hereditary
        angioedema. TESTCOMBO is contraindicated in patients with pollinosis. Omeprazole is
        contraindicated in patients with severe hepatic impairment.</paragraph></text>
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
TWO_SENTENCE_QUOTE = MI_QUOTE + ". " + ULCER_QUOTE
NEIGHBOUR_QUOTE = (NSAID_QUOTE + ". " + OWN_QUOTE + ". " + VIRAL_QUOTE)
ANGIOEDEMA_QUOTE = "TESTCOMBO is contraindicated in patients with a history of angioedema"
RENAL_QUOTE = "TESTCOMBO is contraindicated in patients with severe renal impairment"
BYPASS_QUOTE = ("TESTCOMBO is contraindicated after gastric bypass because of poor absorption of "
                "Aspirin and Omeprazole Tablets")
HAE_QUOTE = "TESTCOMBO is contraindicated in patients with hereditary angioedema"
POLLINOSIS_QUOTE = "TESTCOMBO is contraindicated in patients with pollinosis"
SUBJECT_QUOTE = "Omeprazole is contraindicated in patients with severe hepatic impairment"
EARLIER_COMPONENT_QUOTE = (
    "The omeprazole component of TESTCOMBO is indicated for decreasing the risk of developing "
    "aspirin associated gastric ulcers in patients at risk for developing aspirin associated "
    "gastric ulcers due to age (\u2265 55). TESTCOMBO is also indicated after acute myocardial "
    "infarction")
UTI_QUOTE = ("TESTCOMBO is indicated for complicated urinary tract infections including "
             "pyelonephritis")


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
    TermInfo("MONDO:0010481", "angioedema"),
    TermInfo("MONDO:0019623", "hereditary angioedema", parents=["MONDO:0010481"]),
    TermInfo("MONDO:9999901", "pollinosis", parents=["MONDO:0005271"]),
    TermInfo("HP:0012823", "Clinical modifier"),
    TermInfo("HP:0012824", "Severity", parents=["HP:0012823"]),
    TermInfo("HP:0012828", "Severe", parents=["HP:0012824"]),
    TermInfo("HP:0000083", "Renal insufficiency"),
    TermInfo("MONDO:0005047", "urinary tract infection"),
    TermInfo("MONDO:0006939", "pyelonephritis", parents=["MONDO:0005047"]),
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
    from medic.curation.label import parse_label, write_pin

    write_pin(parse_label(SPL.encode()), SPL.encode(), tmp_path / "curation/labels/dailymed")
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


def test_curator_reference_names_the_skill_by_content(workspace):
    """Round 2 B1/S7: a commit reference broke in shallow clones and accepted any file."""
    import subprocess

    from medic.curation.label import SKILL_PATH, SKILL_REF

    record = _valid_record()

    def errors_for(ref):
        record["curators"] = [{"curator_type": "AI_AGENT", "name": "skill", "curator_id": ref}]
        return _findings(_write(workspace, record, with_history=False))

    _write(workspace, _valid_record())  # one history record for the slug
    assert any("git blob id" in e for e in errors_for("UNCOMMITTED skill")[0])
    fork = f"https://github.com/evil/fork/blob/{'a' * 40}/README.md#git-blob={'b' * 40}"
    assert any("git blob id" in e for e in errors_for(fork)[0])

    skill = Path(SKILL_PATH)
    skill.parent.mkdir(parents=True)
    skill.write_text("skill text\n")
    blob = subprocess.run(["git", "hash-object", str(skill)], capture_output=True,
                          text=True).stdout.strip()
    errors, warnings = errors_for(SKILL_REF.format(rev="a" * 40, blob=blob))
    assert not any("curator_id" in e for e in errors)
    assert not any("earlier skill text" in w for w in warnings)
    errors, warnings = errors_for(SKILL_REF.format(rev="a" * 40, blob="c" * 40))
    assert not any("curator_id" in e for e in errors)
    assert any("earlier skill text" in w for w in warnings)


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


def test_files_are_checked_against_committed_pins(workspace):
    from medic.curation.label import load_pin, pinned_label

    assert pinned_label(SETID, 3).sections["CONTRAINDICATIONS"]
    assert load_pin(SETID, 2) is None
    record = _valid_record()
    record["products"][0]["document"]["version"] = 2
    errors, _ = _findings(_write(workspace, record))
    assert any("no committed label pin" in e for e in errors)


def test_enter_repo_root_resolves_paths_first(tmp_path, monkeypatch):
    from medic.curation.paths import REPO_ROOT, enter_repo_root

    monkeypatch.chdir(REPO_ROOT / "kb")
    assert enter_repo_root(["combinations/x.yaml"]) == [Path("kb/combinations/x.yaml")]
    assert Path.cwd() == REPO_ROOT


# --- review round 2 ---------------------------------------------------------------------------
def test_component_statement_is_scoped_to_the_objects_sentence(workspace):
    """Round 2 S1 (A2): a later "The omeprazole component" sentence credits nothing earlier."""
    errors, _ = _claim_errors(workspace, 0, lambda c: c.update(
        quote=TWO_SENTENCE_QUOTE,
        attributed_to=[{"ingredient": OMEPRAZOLE, "basis": "COMPONENT_STATEMENT"}]))
    assert any("COMPONENT_STATEMENT needs" in e for e in errors)
    errors, _ = _claim_errors(workspace, 0, lambda c: c.update(quote=TWO_SENTENCE_QUOTE))
    assert not any("attributed_to" in e for e in errors)


def test_stated_reason_is_scoped_to_the_objects_sentence(workspace):
    """Round 2 S1 (A3/A4): a neighbouring sentence's cause is not this claim's."""
    errors, _ = _claim_errors(workspace, 2, lambda c: c.update(
        quote=NEIGHBOUR_QUOTE,
        attributed_to=[{"ingredient": ASPIRIN, "basis": "STATED_REASON"}]))
    assert any("STATED_REASON needs" in e for e in errors)


def test_product_name_after_a_cause_is_not_a_stated_reason(workspace):
    """Round 2 S9: pins the product-name stripping (this fails without it)."""
    def mutate(c):
        c.update(quote=BYPASS_QUOTE, life_stage=None,
                 object={"text": "gastric bypass", "term_gap": "procedure; not a condition"},
                 attributed_to=[{"ingredient": ASPIRIN, "basis": "STATED_REASON"}])
    errors, _ = _claim_errors(workspace, 3, mutate)
    assert any("STATED_REASON needs" in e for e in errors)


def test_own_ingredient_rule_applies_only_to_allergies(workspace):
    """Round 2 S2: a drug-caused condition may name the product's own ingredient."""
    errors, _ = _claim_errors(workspace, 3, lambda c: c.update(object_specialization=[
        {"text": "aspirin", "term": _term(ASPIRIN), "binding": "EXACT"}]))
    assert not any("object_specialization" in e for e in errors)


def _contra(c, quote, obj, **extra):
    c.update({"quote": quote, "life_stage": None, "attributed_to": None, "object": obj,
              **extra})


def test_allergy_is_recognised_by_hierarchy(workspace):
    """Round 2 S3: "pollinosis" has no allergy word but sits under allergic disease."""
    errors, _ = _claim_errors(workspace, 3, lambda c: _contra(
        c, POLLINOSIS_QUOTE,
        {"text": "pollinosis", "term": _term("MONDO:9999901"), "binding": "EXACT"}))
    assert any("must name the agents" in e for e in errors)


def test_hereditary_angioedema_is_not_an_allergy(workspace):
    """Round 3 S1: an ACE-inhibitor contraindication that names no agent stays curatable."""
    errors, _ = _claim_errors(workspace, 3, lambda c: _contra(
        c, HAE_QUOTE,
        {"text": "hereditary angioedema", "term": _term("MONDO:0019623"), "binding": "EXACT"}))
    assert errors == []


def test_hp_clinical_modifiers_do_not_anchor(workspace):
    """Round 2 S4: "severe" matches HP Severe, which names no condition."""
    errors, _ = _claim_errors(workspace, 3, lambda c: c.update(
        quote=RENAL_QUOTE, life_stage=None, attributed_to=None,
        object={"text": "severe renal impairment", "term": _term("HP:0000083"),
                "binding": "BROADER"}))
    assert errors == []


def test_inclusion_lists_do_not_make_the_head_term_too_broad(workspace):
    """Round 2 S5: "complicated urinary tract infections including pyelonephritis"."""
    errors, _ = _claim_errors(workspace, 0, lambda c: c.update(
        quote=UTI_QUOTE, relation="TREATS", attributed_to=None, disease_context=None,
        object={"text": "complicated urinary tract infections including pyelonephritis",
                "term": _term("MONDO:0005047"), "binding": "BROADER"}))
    assert errors == []


def test_exact_on_part_of_the_text_warns_when_words_could_narrow(workspace):
    """Round 2 S6: "aspirin associated gastric ulcers" is more than gastric ulcer."""
    _, warnings = _claim_errors(workspace, 1, lambda c: c["object"].update(binding="EXACT"))
    assert any("may narrow the condition" in w for w in warnings)
    # "nonfatal" describes the event, not a narrower condition.
    _, warnings = _claim_errors(workspace, 0, lambda c: None)
    assert not any("may narrow" in w for w in warnings)


# --- review round 3 ---------------------------------------------------------------------------
def test_component_statement_does_not_reach_back_into_an_earlier_sentence(workspace):
    """Round 3: pins the sentence *start* (an earlier sentence's component phrase)."""
    errors, _ = _claim_errors(workspace, 0, lambda c: c.update(
        quote=EARLIER_COMPONENT_QUOTE, disease_context=None,
        object={"text": "acute myocardial infarction", "term": _term("MONDO:0004781"),
                "binding": "EXACT"},
        attributed_to=[{"ingredient": OMEPRAZOLE, "basis": "COMPONENT_STATEMENT"}]))
    assert any("COMPONENT_STATEMENT needs" in e for e in errors)


def test_ingredient_as_sentence_subject(workspace):
    """Co-packaged labels: "Omeprazole is contraindicated in patients with ..."."""
    def mutate(ingredient):
        def m(c):
            _contra(c, SUBJECT_QUOTE,
                    {"text": "hepatic impairment", "term_gap": "not in the test ontology"},
                    attributed_to=[{"ingredient": ingredient, "basis": "INGREDIENT_SUBJECT"}])
        return m
    errors, _ = _claim_errors(workspace, 3, mutate(OMEPRAZOLE))
    assert not any("attributed_to" in e for e in errors)
    errors, _ = _claim_errors(workspace, 3, mutate(ASPIRIN))
    assert any("INGREDIENT_SUBJECT needs" in e for e in errors)


def test_pin_must_hold_the_label_it_is_named_for(workspace):
    """Round 3 S2: a pin whose contents name another label/version is refused."""
    pin = Path(f"curation/labels/dailymed/{SETID}.v3.yaml")
    data = yaml.safe_load(pin.read_text())
    data["version"] = 4
    pin.write_text(yaml.safe_dump(data, sort_keys=False))
    errors, _ = _findings(_write(workspace, _valid_record()))
    assert any("pins are written by the label helper" in e for e in errors)


def test_pins_are_never_silently_overwritten(workspace):
    from medic.curation.label import PinMismatch, parse_label, write_pin

    xml = SPL.encode()
    pin = Path(f"curation/labels/dailymed/{SETID}.v3.yaml")
    before = pin.read_text()
    write_pin(parse_label(xml), xml, Path("curation/labels/dailymed"))  # identical: a no-op
    assert pin.read_text() == before
    edited = SPL.replace("relief of heartburn", "relief of gout").encode()
    with pytest.raises(PinMismatch):
        write_pin(parse_label(edited), edited, Path("curation/labels/dailymed"))


def test_verify_pin_detects_a_hand_edit(workspace):
    from medic.curation.label import verify_pin

    cache = Path("cache/labels/dailymed")
    cache.mkdir(parents=True)
    (cache / f"{SETID}.v3.xml").write_text(SPL)
    assert verify_pin(SETID, 3).startswith("OK")
    pin = Path(f"curation/labels/dailymed/{SETID}.v3.yaml")
    pin.write_text(pin.read_text().replace("heartburn", "gout"))
    assert verify_pin(SETID, 3).startswith("MISMATCH")

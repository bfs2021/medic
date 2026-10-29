"""Tests for the Biolink KGX export.

See ``specs/2026-08-13-kgx-export-design.md``. The tests are grouped by the spec's
implementation phases: the pinned vocabulary and the conformance gate first, because the
gate is what proves the rest of the export is Biolink-valid rather than plausible-looking.
"""

from __future__ import annotations

import copy

import pytest

from medic.export.kgx import biolink as bl


# ---------------------------------------------------------------------------
# Phase 1a — CURIE <-> Biolink model name
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "curie,expected",
    [
        ("biolink:Drug", "drug"),
        ("biolink:ChemicalEntity", "chemical entity"),
        ("biolink:DiseaseOrPhenotypicFeature", "disease or phenotypic feature"),
        ("biolink:treats", "treats"),
        ("biolink:contraindicated_in", "contraindicated in"),
        ("biolink:has_side_effect", "has side effect"),
    ],
)
def test_curie_maps_to_biolink_model_name(curie, expected):
    assert bl.model_name(curie) == expected


# ---------------------------------------------------------------------------
# Phase 1b — every term MeDIC emits exists in the pinned Biolink model
# ---------------------------------------------------------------------------
def test_pinned_biolink_version_is_the_installed_one():
    assert bl.BIOLINK_VERSION == bl.installed_biolink_version()


def test_every_emitted_category_is_a_biolink_class():
    classes = bl.biolink_classes()
    for category in bl.all_categories():
        assert bl.model_name(category) in classes, f"{category} is not a Biolink class"


def test_every_emitted_predicate_descends_from_related_to():
    for predicate in bl.all_predicates():
        assert bl.is_predicate(predicate), f"{predicate} is not a Biolink predicate"


def test_contraindication_predicate_is_contraindicated_in():
    """Regression for D-1: `biolink:contraindicated_for` does not exist in Biolink 4.x."""
    assert bl.CONTRAINDICATION_PREDICATE == "biolink:contraindicated_in"
    assert not bl.is_predicate("biolink:contraindicated_for")


def test_every_knowledge_level_is_permissible():
    permissible = bl.enum_values("KnowledgeLevelEnum")
    for value in set(bl.KNOWLEDGE_LEVELS.values()):
        assert value in permissible


def test_every_agent_type_is_permissible():
    permissible = bl.enum_values("AgentTypeEnum")
    for value in set(bl.AGENT_TYPE_BY_METHOD.values()) | {bl.DEFAULT_AGENT_TYPE}:
        assert value in permissible


def test_every_predicate_has_an_association_class_that_admits_it():
    """The class is the edge's `category`; its predicate constraint must hold.

    The pydantic class types `predicate` as a plain string, so this is the one rule the
    constructor cannot enforce and the gate checks instead.
    """
    for predicate in bl.all_predicates():
        category = bl.association_category(predicate)
        assert bl.model_name(category) in bl.biolink_classes(), category
        assert "association" in bl.model_name(category)
        constraint = bl.class_predicate_constraint(category)
        assert not constraint or constraint in bl.predicate_ancestors(predicate), (
            f"{predicate} does not specialise {constraint}, which {category} requires")


def test_predicate_ancestry_follows_mixins_as_well_as_is_a():
    """`preventative for condition` is a `treats` only through its mixin."""
    assert "treats" in bl.predicate_ancestors("biolink:preventative_for_condition")
    assert "treats or applied or studied to treat" in bl.predicate_ancestors(
        "biolink:applied_to_treat")
    assert "treats" not in bl.predicate_ancestors("biolink:contraindicated_in")


def test_off_class_edge_slots_are_real_biolink_slots_and_pinned():
    """The slots that bypass the class constructor: each must exist in the model, and the
    list is pinned so it cannot grow without a stated reason in biolink.py."""
    slots = bl.biolink_slots()
    for slot in bl.OFF_CLASS_EDGE_SLOTS:
        assert slot.replace("_", " ") in slots, f"{slot} is not a Biolink slot"
    assert set(bl.OFF_CLASS_EDGE_SLOTS) == {
        "clinical_approval_status", "max_research_phase",
        "temporal_context_qualifier", "sex_qualifier",
        "subject_location_in_text", "object_location_in_text",
    }
    for slot in bl.OFF_CLASS_ENUMS:
        assert slot in bl.OFF_CLASS_EDGE_SLOTS


def test_every_off_class_slot_is_absent_from_at_least_one_emitting_class():
    """The list exists for slots some class lacks. If a Biolink release attaches one to
    every class the exporter writes, it belongs in the constructor path, not here. (A slot
    a *particular* class declares already goes through that class: `_build` only bypasses
    what the class lacks, so `clinical_approval_status` is validated on `treats` edges.)
    `population_context_qualifier` left the list this way when 4.4.6 attached it everywhere."""
    classes = {bl.model_class(c) for c in set(bl.ASSOCIATION_CLASS.values())}
    for slot in bl.OFF_CLASS_EDGE_SLOTS:
        lacking = [c.__name__ for c in classes if slot not in c.model_fields]
        assert lacking, f"every class declares {slot}; drop it from OFF_CLASS_EDGE_SLOTS"


def test_sex_qualifier_terms_are_biological_sex_ids():
    assert set(bl.SEX_QUALIFIER_TERMS.values()) == {"PATO:0000383", "PATO:0000384"}


def test_medic_extension_prefix_cannot_shadow_a_biolink_slot():
    """A `medic_x` key must never collide with a Biolink slot named `x`."""
    slots = bl.biolink_slots()
    assert bl.EXTENSION_PREFIX == "medic_"
    assert all(name.startswith(bl.EXTENSION_PREFIX) for name in bl.EXTENSION_SLOTS)
    collisions = {
        name: name[len(bl.EXTENSION_PREFIX):].replace("_", " ")
        for name in bl.EXTENSION_SLOTS
        if name[len(bl.EXTENSION_PREFIX):].replace("_", " ") in slots
    }
    assert not collisions, f"extension names shadow Biolink slots: {collisions}"


def test_mapped_clinical_approval_statuses_are_permissible():
    permissible = bl.enum_values("ClinicalApprovalStatusEnum")
    for value in bl.CLINICAL_APPROVAL_STATUS.values():
        assert value in permissible


def test_mapped_research_phases_are_permissible():
    permissible = bl.enum_values("ResearchPhaseEnum")
    for value in bl.RESEARCH_PHASE.values():
        assert value in permissible


def test_fda_approval_gets_the_fda_specific_status():
    assert bl.clinical_approval_status("APPROVED", "FDA") == "fda_approved_for_condition"
    assert bl.clinical_approval_status("APPROVED", "EMA") == "approved_for_condition"


def test_unmappable_medic_values_degrade_to_not_provided():
    """MeDIC has values Biolink's enums lack; they must degrade, never be mis-mapped."""
    assert bl.research_phase("CASE_REPORT") == "not_provided"
    assert bl.clinical_approval_status("DISCONTINUED") == "not_provided"


def test_agent_type_is_derived_from_recorded_method():
    """D-2 regression: LLM-extracted claims are not `manual_agent`."""
    assert bl.agent_type(method="LLM") == "text_mining_agent"
    assert bl.agent_type(method="STRUCTURED_FIELD") == "data_analysis_pipeline"
    assert bl.agent_type(agent_kind="HUMAN") == "manual_agent"
    assert bl.agent_type() == "not_provided"


# ---------------------------------------------------------------------------
# Phase 1c — the conformance gate
# ---------------------------------------------------------------------------
GOOD_NODES = [
    {"id": "CHEBI:10023", "name": "voriconazole", "category": ["biolink:Drug"]},
    {"id": "MONDO:0000240", "name": "invasive aspergillosis", "category": ["biolink:Disease"]},
]
GOOD_EDGE = {
    "id": "MEDICEDGE:1",
    "category": ["biolink:ChemicalOrDrugOrTreatmentToDiseaseOrPhenotypicFeatureAssociation"],
    "subject": "CHEBI:10023",
    "predicate": "biolink:treats",
    "object": "MONDO:0000240",
    "primary_knowledge_source": "infores:ema",
    "aggregator_knowledge_source": ["infores:medic"],
    "knowledge_level": "knowledge_assertion",
    "agent_type": "text_mining_agent",
    "medic_jurisdiction": "EU",
    "medic_source": "EMA",
}


def _messages(report):
    return " ".join(f"{p.severity}:{p.message}" for p in report.problems)


def test_gate_accepts_a_conformant_graph():
    from medic.export.kgx import validate

    report = validate.check(GOOD_NODES, [GOOD_EDGE])
    assert report.ok, _messages(report)


def test_gate_rejects_an_unknown_predicate():
    """D-1 regression: the gate must fail the export we ship today."""
    from medic.export.kgx import validate

    edge = {**GOOD_EDGE, "predicate": "biolink:contraindicated_for"}
    report = validate.check(GOOD_NODES, [edge])
    assert not report.ok
    assert "contraindicated_for" in _messages(report)


def test_gate_rejects_list_valued_primary_knowledge_source():
    """D-3 regression: Biolink defines primary_knowledge_source as single-valued."""
    from medic.export.kgx import validate

    edge = {**GOOD_EDGE, "primary_knowledge_source": ["infores:ema"]}
    report = validate.check(GOOD_NODES, [edge])
    assert not report.ok
    assert "primary_knowledge_source" in _messages(report)


def test_gate_rejects_a_dangling_edge_endpoint():
    from medic.export.kgx import validate

    edge = {**GOOD_EDGE, "object": "MONDO:9999999"}
    report = validate.check(GOOD_NODES, [edge])
    assert not report.ok
    assert "MONDO:9999999" in _messages(report)


def test_gate_rejects_an_edge_without_a_category():
    from medic.export.kgx import validate

    edge = {k: v for k, v in GOOD_EDGE.items() if k != "category"}
    report = validate.check(GOOD_NODES, [edge])
    assert not report.ok
    assert "no category" in _messages(report)


def test_gate_rejects_a_predicate_the_association_class_does_not_admit():
    """`contraindicated_in` is not a `treats or applied or studied to treat`."""
    from medic.export.kgx import validate

    edge = {**GOOD_EDGE, "predicate": "biolink:contraindicated_in"}
    report = validate.check(GOOD_NODES, [edge])
    assert not report.ok
    assert "does not specialise" in _messages(report)


def test_gate_rejects_a_biolink_slot_the_class_does_not_declare():
    """A real Biolink slot is not enough: it must be on the edge's class (or in the pinned
    off-class list). `supporting_text_section_type` exists in the model, on
    TextMiningStudyResult only."""
    from medic.export.kgx import validate

    edge = {**GOOD_EDGE, "supporting_text_section_type": "BODY"}
    report = validate.check(GOOD_NODES, [edge])
    assert not report.ok
    assert "supporting_text_section_type" in _messages(report)


def test_gate_rejects_an_impermissible_enum_value():
    from medic.export.kgx import validate

    edge = {**GOOD_EDGE, "knowledge_level": "pretty_sure"}
    report = validate.check(GOOD_NODES, [edge])
    assert not report.ok
    assert "knowledge_level" in _messages(report)

    edge = {**GOOD_EDGE, "clinical_approval_status": "approved_ish"}
    report = validate.check(GOOD_NODES, [edge])
    assert not report.ok
    assert "ClinicalApprovalStatusEnum" in _messages(report)


def test_gate_accepts_a_nested_retrieval_source():
    from medic.export.kgx import validate

    edge = {**GOOD_EDGE, "sources": [{
        "id": "infores:ema", "resource_id": "infores:ema",
        "resource_role": "primary_knowledge_source",
        "source_record_urls": ["EMA:vfend", "https://ema.europa.eu/x"]}]}
    report = validate.check(GOOD_NODES, [edge])
    assert report.ok, _messages(report)

    edge = {**GOOD_EDGE, "sources": [{"id": "infores:ema", "resource_role": "primary_knowledge_source"}]}
    report = validate.check(GOOD_NODES, [edge])
    assert not report.ok                      # resource_id is required
    assert "resource_id" in _messages(report)


def test_gate_rejects_a_node_with_two_categories():
    """The class fixes `category` to itself; a consumer expands the hierarchy."""
    from medic.export.kgx import validate

    nodes = [*GOOD_NODES, {"id": "X:1", "category": ["biolink:Drug", "biolink:ChemicalEntity"]}]
    report = validate.check(nodes, [GOOD_EDGE])
    assert not report.ok
    assert "expected one" in _messages(report)


def test_gate_rejects_an_unknown_node_category():
    from medic.export.kgx import validate

    nodes = [*GOOD_NODES, {"id": "X:1", "category": ["biolink:NotAThing"]}]
    report = validate.check(nodes, [GOOD_EDGE])
    assert not report.ok
    assert "NotAThing" in _messages(report)


def test_gate_rejects_an_unnamespaced_non_biolink_property():
    from medic.export.kgx import validate

    edge = {**GOOD_EDGE, "snippet": "some label text"}
    report = validate.check(GOOD_NODES, [edge])
    assert not report.ok
    assert "snippet" in _messages(report)


def test_gate_accepts_medic_namespaced_properties():
    from medic.export.kgx import validate

    edge = {**GOOD_EDGE, "medic_snippet": "some label text", "medic_confidence_overall": 0.9}
    report = validate.check(GOOD_NODES, [edge])
    assert report.ok, _messages(report)


def test_gate_flags_source_isolation_violation():
    """I-1 echo: an edge's jurisdiction must match its source's own jurisdiction."""
    from medic.export.kgx import validate

    edge = {**GOOD_EDGE, "medic_source": "DAILYMED", "medic_jurisdiction": "EU"}
    report = validate.check(GOOD_NODES, [edge])
    assert not report.ok
    assert "isolation" in _messages(report).lower()


def test_gate_warns_when_an_edge_falls_back_to_the_aggregator_as_primary_source():
    """An unmapped regulatory source is a coverage gap that must stay visible."""
    from medic.export.kgx import validate

    edge = {**GOOD_EDGE, "primary_knowledge_source": bl.AGGREGATOR}
    report = validate.check(GOOD_NODES, [edge])
    assert report.ok, _messages(report)
    assert any(p.severity == "warning" and "primary" in p.message.lower()
               for p in report.problems)


def test_gate_warns_but_does_not_fail_on_unexpected_id_prefix():
    from medic.export.kgx import validate

    nodes = [*GOOD_NODES, {"id": "DRON:00010000", "category": ["biolink:ChemicalEntity"]}]
    edge = {**GOOD_EDGE, "subject": "DRON:00010000"}
    report = validate.check(nodes, [edge])
    assert report.ok, _messages(report)
    assert any(p.severity == "warning" and "DRON" in p.message for p in report.problems)


# ---------------------------------------------------------------------------
# Phase 2 — nodes
# ---------------------------------------------------------------------------
DRUG_RECORD = {
    "identity": {
        "id": "MEDICNE:1d3f4d29",
        "original_literal": "NALIDIXIC ACID",
        "mention_source": "ORANGEBOOK",
        "resolved_id": "CHEBI:100147",
        "resolved_label": "nalidixic acid",
        "resolution": {
            "confidence": 0.85,
            "pipeline": [
                {"category": "GROUNDING", "quality": "lexical_exact",
                 "source_vocabulary": "CHEBI"},
                {"category": "NORMALIZATION", "quality": "identity"},
            ],
        },
    },
    "approvals": [
        {"authority": "FDA", "source": "ORANGEBOOK", "status": "APPROVED",
         "approval_date": "19820101", "marketing_status": "DISCN",
         "application_number": "014214",
         "regulatory_document_url": "https://accessdata.fda.gov/x"},
        {"authority": "MOH_RUSSIA", "source": "GRLS", "status": "APPROVED",
         "approval_date": "20060818"},
    ],
    "source_ingredients": ["NALIDIXIC ACID"],
    "synonyms": ["nalidixinic acid"],
    "alternate_ids": ["DRUGBANK:DB00779"],
    "features": ["ANTIMICROBIAL"],
    "atc": {"codes": ["J01MB02"], "main": "J", "level1": "J01"},
    "smiles": "CCN1C=C(C(=O)O)C(=O)c2ccc(C)nc21",
    "drug_class": "Quinolone antibacterial",
}

DISEASE_RECORD = {
    "category_class": "MONDO:0000240",
    "label": "invasive aspergillosis",
    "definition": "An aspergillosis that involves tissue invasion.",
    "synonyms": ["invasive Aspergillus infection"],
    "crossreferences": ["UMLS:C0004030", "MESH:D055744"],
    "subsets": ["mondo:gard_rare"],
    "f_is_rare": True,
    "f_is_infectious": True,
    "f_is_cancer": False,
    "f_in_scope": True,
}


def test_drug_node_carries_canonical_identity_and_categories():
    from medic.export.kgx import nodes

    node = nodes.drug_node(DRUG_RECORD)
    assert node["id"] == "CHEBI:100147"
    assert node["name"] == "nalidixic acid"
    assert node["category"] == [bl.DRUG_CATEGORY] == ["biolink:Drug"]
    assert node["provided_by"] == [bl.AGGREGATOR]


def test_drug_node_unions_synonyms_and_xrefs():
    from medic.export.kgx import nodes

    node = nodes.drug_node(DRUG_RECORD)
    assert "nalidixinic acid" in node["synonym"]
    assert "NALIDIXIC ACID" in node["synonym"]          # the verbatim source literal
    assert node["xref"] == ["DRUGBANK:DB00779"]
    assert node["synonym"] == sorted(set(node["synonym"]))


def test_drug_node_xref_unions_source_xrefs_with_alternate_ids():
    """Source-copied cross-references (PHAROS, RxNorm) reach `xref` alongside the ids
    MeDIC's own resolution established — one joinable list, deduplicated (#94)."""
    from medic.export.kgx import nodes

    record = {
        **DRUG_RECORD,
        "alternate_ids": ["DRUGBANK:DB00779", "UNII:3B91HWA56B"],
        "source_xrefs": ["CHEMBL.COMPOUND:CHEMBL5", "RXCUI:7319", "UNII:3B91HWA56B"],
    }
    node = nodes.drug_node(record)
    assert node["xref"] == [
        "CHEMBL.COMPOUND:CHEMBL5", "DRUGBANK:DB00779", "RXCUI:7319", "UNII:3B91HWA56B",
    ]
    assert "source_xrefs" not in node       # folded into xref, not a slot of its own


def test_drug_node_summarises_approvals_across_jurisdictions():
    from medic.export.kgx import nodes

    node = nodes.drug_node(DRUG_RECORD)
    assert node["medic_approved_authorities"] == ["FDA", "MOH_RUSSIA"]
    assert node["medic_approved_jurisdictions"] == ["russia", "usa"]
    assert node["medic_earliest_approval_date"] == "19820101"
    assert node["medic_marketing_status_usa"] == "DISCN"
    assert "available_from" not in node         # DISCN is not an availability
    assert node["medic_application_numbers"] == ["014214"]


def test_us_marketing_status_maps_onto_available_from_where_it_can():
    """RX and OTC are exactly Biolink's two DrugAvailabilityEnum values."""
    import copy

    from medic.export.kgx import nodes

    record = copy.deepcopy(DRUG_RECORD)
    record["approvals"][0]["marketing_status"] = "RX"
    node = nodes.drug_node(record)
    assert node["available_from"] == ["prescription"]
    assert node["medic_marketing_status_usa"] == "RX"


def test_drug_node_carries_chemistry_and_the_provenance_join_key():
    """The ATC levels are prefixes of the code, and the grounding quality, source and
    literal are the mapping store's columns, reachable through the mention id — none of
    them earns a column on 4,000 nodes."""
    from medic.export.kgx import nodes

    node = nodes.drug_node(DRUG_RECORD)
    assert node["medic_atc_codes"] == ["J01MB02"]
    assert "medic_atc_main" not in node
    assert "medic_atc_level1" not in node
    assert node["medic_smiles"].startswith("CCN1")
    assert node["medic_features"] == ["ANTIMICROBIAL"]
    assert node["medic_mention_id"] == "MEDICNE:1d3f4d29"
    assert "medic_original_literal" not in node   # it is in `synonym`
    assert "medic_grounding_quality" not in node
    assert "NALIDIXIC ACID" in node["synonym"]


def test_every_node_property_is_declared():
    """Nothing un-namespaced that the node's Biolink class lacks; nothing namespaced that
    EXTENSION_SLOTS does not list."""
    from medic.export.kgx import nodes

    for node in (nodes.drug_node(DRUG_RECORD), nodes.disease_node(DISEASE_RECORD),
                 nodes.stub_node("ORPHA:1", "x")):
        fields = bl.model_class(node["category"][0]).model_fields
        for key in node:
            if key.startswith(bl.EXTENSION_PREFIX):
                assert key in bl.EXTENSION_SLOTS or key.startswith("medic_f_"), key
            else:
                assert key in fields, key


def test_node_omits_empty_values_entirely():
    """An absent fact is an absent key, not an empty string a consumer must special-case."""
    from medic.export.kgx import nodes

    node = nodes.drug_node({"identity": {"resolved_id": "CHEBI:1", "resolved_label": "x"}})
    assert "medic_smiles" not in node
    assert "xref" not in node
    assert "medic_atc_codes" not in node
    assert not any(v in ("", [], {}, None) for v in node.values())


def test_drug_without_a_resolved_id_is_not_exported():
    from medic.export.kgx import nodes

    assert nodes.drug_node({"identity": {"original_literal": "SOMETHING"}}) is None


def test_disease_node_maps_definition_and_crossreferences():
    from medic.export.kgx import nodes

    node = nodes.disease_node(DISEASE_RECORD)
    assert node["id"] == "MONDO:0000240"
    assert node["category"] == [bl.DISEASE_CATEGORY]
    assert node["description"].startswith("An aspergillosis")
    assert node["xref"] == ["MESH:D055744", "UMLS:C0004030"]
    assert node["subsets"] == ["mondo:gard_rare"]


def test_disease_node_emits_only_true_filter_flags():
    """26 booleans x 23k diseases of `false` is megabytes of nothing."""
    from medic.export.kgx import nodes

    node = nodes.disease_node(DISEASE_RECORD)
    assert node["medic_f_is_rare"] is True
    assert node["medic_f_is_infectious"] is True
    assert "medic_f_is_cancer" not in node


def test_stub_node_infers_category_from_prefix_and_is_marked():
    from medic.export.kgx import nodes

    stub = nodes.stub_node("ORPHA:295193", "some rare disease")
    assert stub["category"] == ["biolink:Disease"]
    assert stub["name"] == "some rare disease"
    assert stub["medic_stub"] is True

    assert nodes.stub_node("UNII:YB18NF020M")["category"] == ["biolink:ChemicalEntity"]
    assert nodes.stub_node("WEIRD:1")["category"] == [bl.FALLBACK_CATEGORY]


def test_build_nodes_deduplicates_and_sorts_by_id():
    from medic.export.kgx import nodes

    built = nodes.build_nodes([DRUG_RECORD], [DISEASE_RECORD],
                              referenced={"CHEBI:100147": "", "ORPHA:1": "rare thing"})
    ids = [n["id"] for n in built]
    assert ids == sorted(ids)
    assert len(ids) == len(set(ids))
    assert "ORPHA:1" in ids                 # unknown endpoint became a stub
    assert not any(n.get("medic_stub") for n in built if n["id"] == "CHEBI:100147")


# ---------------------------------------------------------------------------
# Phase 3 — association edges
# ---------------------------------------------------------------------------
def _mention(mention_id, literal, resolved, label, source, *, start=None, end=None,
             rules=(), quality="lexical_exact", translated=False):
    pipeline = [{
        "category": "EXTRACTION", "quality": "verbatim", "span_role": "BODY",
        "span_index": 0,
        **({"char_start": start, "char_end": end} if start is not None else {}),
    }]
    if translated:
        pipeline.append({"category": "TRANSLATION", "quality": "exact"})
    pipeline.append({
        "category": "GROUNDING", "quality": quality, "applied_rules": list(rules),
        "flags": ["fuzzy"] if quality == "fuzzy" else [], "source_vocabulary": "MONDO",
    })
    pipeline.append({"category": "NORMALIZATION", "quality": "identity"})
    return {
        "id": mention_id, "original_literal": literal, "mention_source": source,
        "resolved_id": resolved, "resolved_label": label,
        "resolution": {"confidence": 0.9, "pipeline": pipeline},
    }


def _assertion(source="EMA", document="EMA:vfend", authority="EMA", jurisdiction="EU",
               status="APPROVED", method="LLM"):
    return {
        "source": source,
        "jurisdiction": jurisdiction,
        "document": document,
        # The span the extraction read, with offsets that genuinely select the literals in
        # it. The old fixture paired a 24-character span with offsets at 150-172, encoding
        # the very mismatch the export was shipping.
        "spans": [{"role": "BODY",
                   "text": "voriconazole is indicated in adults with invasive aspergillosis"}],
        "drug": _mention("MEDICNE:d1", "voriconazole", "CHEBI:10023", "voriconazole",
                         source, start=0, end=12),
        "disease": _mention("MEDICNE:s1", "invasive aspergillosis", "MONDO:0000240",
                            "invasive aspergillosis", source, start=41, end=63,
                            rules=["base_normalization"]),
        "assertion": {
            "method": method,
            "agent": {"agent_name": "anthropic/claude-haiku-4-5-20251001",
                      "agent_type": "AI_AGENT",
                      "agent_version": "claude-haiku-4-5-20251001"},
            "confidence": {"subject": 0.9, "object": 1.0, "relationship": 1.0,
                           "overall": 0.9, "basis": "MEASURED"},
            "flags": [], "trigger_cue": "indication_phrase",
            "trigger_span": "is indicated in", "span_index": 0,
            "tool": "medic-extractor", "tool_version": "1",
        },
        "evidence": {
            "source_type": "REGULATORY", "jurisdiction": jurisdiction,
            "reference": "https://ema.europa.eu/x", "snippet": "is indicated in adults",
            "approval_status": "APPROVED", "confidence": "HIGH",
            "explanation": "EMA approved indication",
        },
        "regulatory_status": {
            "authority": authority, "source": source, "status": status,
            "source_role": "PRIMARY", "approval_date": "20020319",
            "regulatory_document_url": "https://ema.europa.eu/x",
        },
    }


PAIR = {
    "drug_id": "CHEBI:10023", "drug_label": "voriconazole",
    "disease_id": "MONDO:0000240", "disease_label": "invasive aspergillosis",
    "relationship_type": "INDICATION",
    "reliability": "HIGH",
    "confidence": {"overall": 0.97, "n_assertions": 2, "method": "noisy_or"},
    "assertions": [
        _assertion(),
        _assertion(source="PMDA", document="PMDA:12345", authority="PMDA",
                   jurisdiction="JAPAN"),
    ],
}


def test_pair_emits_one_edge_per_source_assertion():
    from medic.export.kgx import edges

    built = edges.association_edges(PAIR)
    assert len(built) == 2
    assert {e["primary_knowledge_source"] for e in built} == {"infores:ema", "infores:pmda"}


def test_edge_ids_are_deterministic_and_distinct_per_document():
    from medic.export.kgx import edges

    first = edges.association_edges(PAIR)
    second = edges.association_edges(PAIR)
    assert [e["id"] for e in first] == [e["id"] for e in second]
    assert len({e["id"] for e in first}) == 2
    assert all(e["id"].startswith("MEDICEDGE:") for e in first)


def test_approved_indication_is_asserted_as_treats():
    from medic.export.kgx import edges

    edge = edges.association_edges(PAIR)[0]
    assert edge["predicate"] == "biolink:treats"
    assert edge["category"] == [
        "biolink:ChemicalOrDrugOrTreatmentToDiseaseOrPhenotypicFeatureAssociation"]
    assert edge["knowledge_level"] == "knowledge_assertion"
    assert edge["clinical_approval_status"] == "approved_for_condition"


def test_every_edge_is_a_valid_instance_of_its_association_class():
    """The construction path: un-namespaced properties are exactly the class's fields plus
    the pinned off-class slots, and the class re-validates the dumped record."""
    from medic.export.kgx import edges

    contraindication = {**PAIR, "relationship_type": "CONTRAINDICATION", "is_allergen": True}
    for edge in [*edges.association_edges(PAIR),
                 *edges.association_edges(contraindication),
                 *edges.research_edges(RESEARCH)]:
        cls = bl.model_class(edge["category"][0])
        core = {k: v for k, v in edge.items()
                if not k.startswith(bl.EXTENSION_PREFIX) and k not in bl.OFF_CLASS_EDGE_SLOTS}
        cls.model_validate(core)                 # raises on any undeclared property
        for key in edge:
            if key.startswith(bl.EXTENSION_PREFIX):
                assert key in bl.EXTENSION_SLOTS, key


def test_the_source_record_lives_on_the_retrieval_source():
    """Biolink's `source_record_urls` has domain `retrieval source`, so the document CURIE
    and its URLs ship as one nested `sources` entry for the primary source — not as six
    flat `medic_*_url` properties."""
    from medic.export.kgx import edges

    edge = edges.association_edges(PAIR)[0]
    assert len(edge["sources"]) == 1
    source = edge["sources"][0]
    assert source["resource_id"] == "infores:ema" == edge["primary_knowledge_source"]
    assert source["resource_role"] == "primary_knowledge_source"
    assert source["category"] == ["biolink:RetrievalSource"]
    assert source["source_record_urls"] == ["EMA:vfend", "https://ema.europa.eu/x"]
    assert not [k for k in edge if k.endswith("_url") or k in ("medic_document", "medic_setid")]


def test_a_regulatory_explanation_is_build_level_not_a_description_on_every_edge():
    """"EMA approved indication" is the ingester's one sentence per source; on the edge it
    was ~4 MB of a valid slot saying the same thing 70,000 times. It is recorded once in the
    metadata (see the metadata test below)."""
    from medic.export.kgx import edges

    edge = edges.association_edges(PAIR)[0]
    assert "description" not in edge
    assert "medic_explanation" not in edge


def test_a_research_explanation_is_per_claim_and_stays_on_the_edge():
    from medic.export.kgx import edges

    record = copy.deepcopy(RESEARCH)
    record["evidence"][0]["explanation"] = "Phase 3 ReDUX4 trial met its primary endpoint"
    edge = edges.research_edges(record)[0]
    assert edge["description"] == "Phase 3 ReDUX4 trial met its primary endpoint"


def test_unapproved_indication_drops_to_the_grouping_predicate():
    """Biolink reserves asserted `treats` for approved/established use."""
    from medic.export.kgx import edges

    pair = {**PAIR, "assertions": [_assertion(status="INVESTIGATIONAL")]}
    edge = edges.association_edges(pair)[0]
    assert edge["predicate"] == "biolink:treats_or_applied_or_studied_to_treat"


def test_contraindication_uses_contraindicated_in():
    from medic.export.kgx import edges

    pair = {**PAIR, "relationship_type": "CONTRAINDICATION",
            "is_allergen": True}
    edge = edges.association_edges(pair)[0]
    assert edge["predicate"] == "biolink:contraindicated_in"
    assert edge["category"] == ["biolink:ChemicalEntityToDiseaseOrPhenotypicFeatureAssociation"]
    assert edge["medic_is_allergen"] is True


def test_primary_knowledge_source_is_a_single_value():
    """D-3 regression."""
    from medic.export.kgx import edges

    for edge in edges.association_edges(PAIR):
        assert isinstance(edge["primary_knowledge_source"], str)
        assert edge["aggregator_knowledge_source"] == ["infores:medic"]


def test_agent_type_reflects_that_an_llm_read_the_label():
    """D-2 regression: the old export hard-coded `manual_agent` here."""
    from medic.export.kgx import edges

    assert edges.association_edges(PAIR)[0]["agent_type"] == "text_mining_agent"


def test_edge_carries_verbatim_literals_on_standard_biolink_slots():
    from medic.export.kgx import edges

    edge = edges.association_edges(PAIR)[0]
    assert edge["original_subject"] == "voriconazole"
    assert edge["original_object"] == "invasive aspergillosis"


def test_edge_carries_supporting_text_and_character_offsets():
    from medic.export.kgx import edges

    edge = edges.association_edges(PAIR)[0]
    # A one-element list: Biolink 4.4.6 defines `supporting_text` as multivalued, and the
    # pydantic class rejects a bare string. (An earlier revision shipped a string on the
    # belief it was single-valued; the class is the authority now.)
    # supporting_text is the span the offsets index, not the evidence snippet — Biolink
    # defines *_location_in_text as offsets into this string.
    assert edge["supporting_text"] == [
        "voriconazole is indicated in adults with invasive aspergillosis"]
    assert edge["subject_location_in_text"] == [0, 12]
    assert edge["object_location_in_text"] == [41, 63]
    assert "supporting_text_section_type" not in edge   # TextMiningStudyResult only
    text = edge["supporting_text"][0]
    assert text[0:12] == "voriconazole"
    assert text[41:63] == "invasive aspergillosis"


def test_edge_carries_resolution_join_keys():
    from medic.export.kgx import edges

    edge = edges.association_edges(PAIR)[0]
    assert edge["medic_subject_mention_id"] == "MEDICNE:d1"
    assert edge["medic_object_mention_id"] == "MEDICNE:s1"
    # The rules, quality and flags are the SSSOM row's columns, reached through the id.
    assert "medic_object_applied_rules" not in edge
    assert "medic_object_grounding_quality" not in edge


def test_edge_repeats_pair_level_aggregates_on_every_edge():
    """So the collapsed pair view is a GROUP BY, not a re-derivation (spec §2.1)."""
    from medic.export.kgx import edges

    built = edges.association_edges(PAIR)
    for edge in built:
        assert edge["medic_pair_confidence"] == 0.97
        assert edge["medic_pair_n_assertions"] == 2
        assert edge["medic_pair_reliability"] == "HIGH"
        # Jurisdictions and authorities are a GROUP BY over `medic_jurisdiction` and
        # `primary_knowledge_source`, so they are not repeated.
        assert "medic_pair_jurisdictions" not in edge


def test_edge_confidence_is_this_assertions_own():
    from medic.export.kgx import edges

    edge = edges.association_edges(PAIR)[0]
    assert edge["has_confidence_score"] == 0.9
    assert "medic_confidence_subject" not in edge     # components stay in the products


def test_long_supporting_text_is_truncated_and_flagged():
    from medic.export.kgx import edges

    assertion = _assertion()
    assertion["spans"][0]["text"] = "x" * 5000
    pair = {**PAIR, "assertions": [assertion]}
    edge = edges.association_edges(pair)[0]
    assert len(edge["supporting_text"][0]) == edges.MAX_SUPPORTING_TEXT
    assert edge["medic_supporting_text_truncated"] is True
    # Offsets that no longer select their literal in the shipped string are withheld rather
    # than shipped pointing at the wrong words.
    assert "object_location_in_text" not in edge


def test_pair_without_ids_emits_nothing():
    from medic.export.kgx import edges

    assert edges.association_edges({**PAIR, "disease_id": ""}) == []


# ---------------------------------------------------------------------------
# Phase 4 — research and adverse-event edges
# ---------------------------------------------------------------------------
RESEARCH = {
    "drug_id": "CHEBI:131167", "drug_label": "losmapimod",
    "disease_id": "MONDO:0001347", "disease_label": "FSHD",
    "curation_status": "DRAFT",
    "evidence": [
        {"source_type": "LITERATURE", "reference": "PMID:12345",
         "snippet": "Phase 3 ReDUX4 trial", "max_research_phase": "PHASE_III",
         "evidence_source": "HUMAN_CLINICAL", "confidence": "MEDIUM"},
    ],
}


def test_trial_evidence_uses_in_clinical_trials_for():
    from medic.export.kgx import edges

    edge = edges.research_edges(RESEARCH)[0]
    assert edge["predicate"] == "biolink:in_clinical_trials_for"
    assert edge["knowledge_level"] == "observation"
    assert edge["max_research_phase"] == "clinical_trial_phase_3"
    assert edge["publications"] == ["PMID:12345"]


def test_case_report_evidence_uses_applied_to_treat():
    """Biolink's `applied to treat` is exactly 'actually taken by patients'."""
    from medic.export.kgx import edges

    record = {**RESEARCH, "evidence": [
        {"source_type": "DATABASE", "reference": "https://cure.ncats.io/",
         "max_research_phase": "CASE_REPORT", "approval_status": "OFF_LABEL"}]}
    edge = edges.research_edges(record)[0]
    assert edge["predicate"] == "biolink:applied_to_treat"
    assert edge["medic_research_phase_raw"] == "CASE_REPORT"
    assert edge["max_research_phase"] == "not_provided"


def test_other_literature_uses_studied_to_treat():
    from medic.export.kgx import edges

    record = {**RESEARCH, "evidence": [
        {"source_type": "LITERATURE", "reference": "PMID:999",
         "evidence_source": "IN_VITRO"}]}
    assert edges.research_edges(record)[0]["predicate"] == "biolink:studied_to_treat"


def test_uncitable_research_claim_is_attributed_to_medic_curation_not_the_aggregator():
    """The aggregator id must never appear as a primary knowledge source.

    A deep-research claim whose only reference is a bare website is asserted by MeDIC's own
    curation pipeline, and saying so is honest; saying `infores:medic` — the same id the
    edge already carries as its aggregator — tells a consumer nothing.
    """
    from medic.export.kgx import edges

    record = {**RESEARCH, "evidence": [
        {"source_type": "DATABASE", "reference": "https://www.fshdsociety.org/x"}]}
    edge = edges.research_edges(record)[0]
    assert edge["primary_knowledge_source"] == "infores:medic-research-curation"
    assert edge["primary_knowledge_source"] not in edge["aggregator_knowledge_source"]


def test_publications_take_only_curies_not_bare_urls():
    from medic.export.kgx import edges

    record = {**RESEARCH, "evidence": [
        {"source_type": "DATABASE", "reference": "https://www.fshdsociety.org/x",
         "max_research_phase": "PHASE_II"}]}
    edge = edges.research_edges(record)[0]
    assert "publications" not in edge
    assert edge["sources"][0]["source_record_urls"] == ["https://www.fshdsociety.org/x"]
    assert "medic_reference_url" not in edge


def test_label_mined_adverse_event_is_a_side_effect():
    """PVLens mines labels; Biolink's `has side effect` is the label-listed sense."""
    from medic.export.kgx import edges

    record = {"drug_id": "CHEBI:1", "adverse_event_id": "MedDRA:10019211",
              "adverse_event_label": "headache", "sources": ["PVLens"],
              "label_section": "ADVERSE_REACTIONS", "frequency": "common",
              "severity": "MILD", "evidence": []}
    edge = edges.adverse_event_edges(record)[0]
    assert edge["predicate"] == "biolink:has_side_effect"
    assert edge["category"] == ["biolink:ChemicalOrDrugOrTreatmentSideEffectAssociation"]
    assert edge["object"] == "MedDRA:10019211"
    assert edge["medic_label_section"] == "ADVERSE_REACTIONS"
    assert edge["medic_severity"] == "MILD"


def test_spontaneously_reported_adverse_event_is_an_adverse_event():
    from medic.export.kgx import edges

    record = {"drug_id": "CHEBI:1", "adverse_event_id": "MedDRA:1",
              "adverse_event_hpo_id": "HP:0002315", "sources": ["FAERS"],
              "evidence": []}
    edge = edges.adverse_event_edges(record)[0]
    assert edge["predicate"] == "biolink:has_adverse_event"
    assert edge["category"] == ["biolink:ChemicalOrDrugOrTreatmentAdverseEventAssociation"]
    assert edge["object"] == "HP:0002315"       # prefers the HPO mapping when present
    assert edge["knowledge_level"] == "observation"


# ---------------------------------------------------------------------------
# Phase 5 — assembly, serialization, metadata
# ---------------------------------------------------------------------------
import json  # noqa: E402
import yaml  # noqa: E402


def _write_products(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "drug_list.yaml").write_text(yaml.safe_dump({"drugs": [DRUG_RECORD]}))
    (tmp_path / "disease_list.yaml").write_text(
        yaml.safe_dump({"diseases": [DISEASE_RECORD]}))
    (tmp_path / "indication_list.yaml").write_text(
        yaml.safe_dump({"associations": [PAIR]}))
    (tmp_path / "contraindication_list.yaml").write_text(
        yaml.safe_dump({"associations": []}))
    (tmp_path / "research_list.yaml").write_text(
        yaml.safe_dump({"associations": [RESEARCH]}))
    (tmp_path / "adverse_event_list.yaml").write_text(
        yaml.safe_dump({"associations": []}))
    return tmp_path


def test_export_produces_a_graph_that_passes_the_gate(tmp_path):
    from medic.export.kgx import export_kgx, validate

    products = _write_products(tmp_path / "products")
    exports = tmp_path / "exports"
    export_kgx(products_dir=products, exports_dir=exports)

    report = validate.check_files(exports / "medic_nodes.jsonl",
                                  exports / "medic_edges.jsonl")
    assert report.ok, _messages(report)


def test_export_is_byte_identical_across_runs(tmp_path):
    from medic.export.kgx import export_kgx

    products = _write_products(tmp_path / "products")
    first, second = tmp_path / "a", tmp_path / "b"
    export_kgx(products_dir=products, exports_dir=first)
    export_kgx(products_dir=products, exports_dir=second)

    for name in ("medic_nodes.jsonl", "medic_edges.jsonl"):
        assert (first / name).read_bytes() == (second / name).read_bytes()


def test_export_writes_content_metadata(tmp_path):
    from medic.export.kgx import export_kgx

    products = _write_products(tmp_path / "products")
    exports = tmp_path / "exports"
    export_kgx(products_dir=products, exports_dir=exports)

    meta = yaml.safe_load((exports / "medic_kgx_metadata.yaml").read_text())
    assert meta["biolink_version"] == bl.BIOLINK_VERSION
    # 1 drug + 1 disease from the products, plus stubs for the three endpoints the
    # fixture products do not describe (the PAIR drug and both research endpoints).
    assert meta["nodes"]["total"] == 5
    assert meta["nodes"]["stubs"] == 3
    assert meta["edges"]["by_predicate"]["biolink:treats"] == 2
    assert "infores:ema" in meta["edges"]["by_primary_knowledge_source"]
    assert meta["edges"]["by_category"] == {
        "biolink:ChemicalOrDrugOrTreatmentToDiseaseOrPhenotypicFeatureAssociation": 3}


def test_build_level_agent_provenance_is_in_the_metadata_not_on_every_edge(tmp_path):
    """Which LLM and which extractor version produced the assertions is one fact per build;
    it used to ride on every edge as five constant `medic_*` columns."""
    from medic.export.kgx import export_kgx

    products = _write_products(tmp_path / "products")
    exports = tmp_path / "exports"
    _, built_edges = export_kgx(products_dir=products, exports_dir=exports)

    meta = yaml.safe_load((exports / "medic_kgx_metadata.yaml").read_text())
    assert meta["extraction_agents"] == [{
        "agent_name": "anthropic/claude-haiku-4-5-20251001",
        "agent_version": "claude-haiku-4-5-20251001",
        "tool": "medic-extractor", "tool_version": "1", "method": "LLM",
        "assertions": 2,
    }]
    assert not any("medic_agent_name" in e or "medic_tool" in e for e in built_edges)
    assert meta["source_explanations"] == [
        {"primary_knowledge_source": "infores:ema", "description": "EMA approved indication",
         "assertions": 1},
        {"primary_knowledge_source": "infores:pmda", "description": "EMA approved indication",
         "assertions": 1},
    ]
    assert not any("description" in e for e in built_edges
                   if e["primary_knowledge_source"] == "infores:ema")


def test_export_closes_dangling_endpoints_with_stubs(tmp_path):
    """A research association naming a drug the drug list lacks must not dangle."""
    from medic.export.kgx import export_kgx

    products = _write_products(tmp_path / "products")
    products.joinpath("research_list.yaml").write_text(yaml.safe_dump({"associations": [
        {**RESEARCH, "drug_id": "UNII:YB18NF020M", "drug_label": "Satralizumab"}]}))
    exports = tmp_path / "exports"
    export_kgx(products_dir=products, exports_dir=exports)

    nodes = [json.loads(line) for line in
             (exports / "medic_nodes.jsonl").read_text().splitlines()]
    stub = next(n for n in nodes if n["id"] == "UNII:YB18NF020M")
    assert stub["medic_stub"] is True
    assert stub["name"] == "Satralizumab"


def test_hyperrelational_qualifiers_are_not_emitted_yet():
    """Reserved in the spec (§5.5); must stay dormant until issue #9 populates them."""
    from medic.export.kgx import edges

    built = edges.association_edges(PAIR)
    for edge in built:
        assert not [k for k in edge if k.startswith("medic_context_")]
        assert not [k for k in edge if k.endswith("_qualifier")]


# ---------------------------------------------------------------------------
# Licensing — attribution must travel inside the machine-readable output
# ---------------------------------------------------------------------------
def test_kgx_metadata_carries_licence_and_attribution(tmp_path):
    """A sibling LICENSING.md is not enough — nobody reads sibling assets (issue #37)."""
    from medic.export.kgx import export_kgx

    products = _write_products(tmp_path / "products")
    exports = tmp_path / "exports"
    export_kgx(products_dir=products, exports_dir=exports)

    meta = yaml.safe_load((exports / "medic_kgx_metadata.yaml").read_text())
    assert "license" in meta
    notice = meta["license"]["attribution_notice"]
    assert "European Medicines Agency" in notice
    assert "Data has been edited" in notice          # PMDA Public Data License 1.0
    assert meta["license"]["medic_grant"] == "none"  # MeDIC licenses no upstream rights
    assert "LICENSING.md" in meta["license"]["terms"]


def test_kgx_metadata_notice_comes_from_the_release_manifest(tmp_path):
    """One source of truth: the notice cannot drift from conf/release_assets.yaml."""
    from medic import release_assets as ra
    from medic.export.kgx import export_kgx

    products = _write_products(tmp_path / "products")
    exports = tmp_path / "exports"
    export_kgx(products_dir=products, exports_dir=exports)

    meta = yaml.safe_load((exports / "medic_kgx_metadata.yaml").read_text())
    assert meta["license"]["attribution_notice"] == ra.load().notice


# ---------------------------------------------------------------------------
# Claims the export must not make (review #36, items C2 and D3)
# ---------------------------------------------------------------------------
def test_a_contraindication_carries_no_clinical_approval_status():
    """Biolink defines the slot as approval *for treating the object*.

    All 2,978 contraindication edges shipped `fda_approved_for_condition`, asserting the
    inverse of the claim. The drug's status stays on `medic_regulatory_status`.
    """
    from medic.export.kgx import edges

    pair = {**PAIR, "relationship_type": "CONTRAINDICATION"}
    edge = edges.association_edges(pair)[0]
    assert edge["predicate"] == "biolink:contraindicated_in"
    assert not edge.get("clinical_approval_status")   # pruned, not emitted empty
    assert edge["medic_approval_status_raw"] == "APPROVED"


def test_an_indication_still_carries_clinical_approval_status():
    from medic.export.kgx import edges

    edge = edges.association_edges(PAIR)[0]
    assert edge.get("clinical_approval_status")


def test_evidence_term_does_not_claim_human_review():
    """ECO:0000218 is `manual assertion` — an assertion method, and a false one here."""
    from medic.export.kgx import biolink as bl

    assert bl.ECO_REGULATORY_LABEL == "ECO:0006156"
    assert bl.ECO_REGULATORY_LABEL != "ECO:0000218"


def test_no_exported_edge_reports_a_detected_negation():
    """`medic_negation_detected` used to encode "a negation check ran", which was true of
    every edge. It then meant "a negation was found" — and 14 such edges shipped as
    positive `treats` (#93). Since those are now withheld entirely, the slot can only ever
    be falsy in the export, and this pins that rather than the old round-trip.

    The verdict itself is not lost: it stays on the claim in `kb/` and in the products.
    """
    from medic.export.kgx import edges

    edge = edges.association_edges(PAIR)[0]
    assert not edge.get("medic_negation_detected")

    assertion = _assertion()
    assertion["assertion"]["flags"] = ["negated_inversion"]
    assert edges.association_edges({**PAIR, "assertions": [assertion]}) == []


def test_an_unmapped_source_is_an_error_not_a_skip():
    """The `unmapped` default made the gate report success when it had lost track of the data."""
    from medic.export.kgx import validate

    edge = {**GOOD_EDGE, "medic_source": "NEW_REGISTRY", "medic_jurisdiction": "USA"}
    report = validate.Report()
    validate._check_edge(edge, {"CHEBI:1", "MONDO:1"}, report)
    assert any("SOURCE_JURISDICTION" in p.message for p in report.errors)


def test_india_edges_are_actually_checked_for_source_isolation():
    """The map keyed CDSCO while the exporter writes INDIA, exempting all 132 India edges."""
    from medic.export.kgx import biolink as bl
    from medic.export.kgx import validate

    assert bl.SOURCE_JURISDICTION.get("INDIA") == "INDIA"
    edge = {**GOOD_EDGE, "medic_source": "INDIA", "medic_jurisdiction": "EU"}
    report = validate.Report()
    validate._check_edge(edge, {"CHEBI:1", "MONDO:1"}, report)
    assert any("source isolation" in p.message for p in report.errors)


def test_offsets_are_withheld_when_they_do_not_select_the_literal():
    """Silence beats a confident wrong answer.

    Offsets are computed at merge against the span the extraction read. If the export ever
    ships a different string again, the offsets must vanish rather than point at whatever
    words happen to sit at those positions.
    """
    from medic.export.kgx import edges

    assertion = _assertion()
    assertion["spans"][0]["text"] = "a completely different sentence about something else"
    edge = edges.association_edges({**PAIR, "assertions": [assertion]})[0]
    assert "object_location_in_text" not in edge
    assert "subject_location_in_text" not in edge


# ---------------------------------------------------------------------------
# Registration numbers are kept apart from FDA application numbers (#52)
# ---------------------------------------------------------------------------
def test_registration_numbers_are_split_from_fda_application_numbers():
    """An FDA application number and a GRLS registration number are not interchangeable.

    The FDA's are US government public domain; the Russian ones are reproduced from a register
    that grants no open licence (LICENSING.md). Flattened into one field they looked like a
    single US-shaped identifier space, and there was no way to act on the restricted half
    without touching FDA data.
    """
    import copy

    from medic.export.kgx import nodes

    record = copy.deepcopy(DRUG_RECORD)
    record["approvals"][1]["application_number"] = "\u041b\u041f-000035"  # GRLS registration no.

    node = nodes.drug_node(record)
    assert node["medic_application_numbers"] == ["014214"]
    assert node["medic_registration_numbers"] == ["MOH_RUSSIA:\u041b\u041f-000035"]


def test_a_drug_with_no_foreign_registration_omits_the_field():
    """The unmodified fixture: the Russian approval carries no number, so nothing to publish.

    `_clean` drops empty values, so the field is absent rather than an empty list — the same
    convention every other optional node property follows.
    """
    from medic.export.kgx import nodes

    node = nodes.drug_node(DRUG_RECORD)
    assert "medic_registration_numbers" not in node
    assert node["medic_application_numbers"] == ["014214"]

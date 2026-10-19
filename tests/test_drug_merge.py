"""Tests for drug merge pipeline."""


import yaml

from medic import product_view as pv
from medic.merge.drug_merge import _merge_group, merge_drugs


def test_merge_groups_by_normalized_id(tmp_path):
    source1 = [
        {"source": "ORANGEBOOK", "source_name": "ASPIRIN",
         "normalized_id": "CHEBI:15365", "normalized_label": "aspirin",
         "alternate_ids": ["DRUGBANK:DB00945"],
         "grounding_confidence": 0.98, "grounding_service": "oak",
         "grounding_status": "accepted", "marketing_status_usa": "OTC",
         "approval_date": "19900101"},
    ]
    source2 = [
        {"source": "EMA", "source_name": "ACETYLSALICYLIC ACID",
         "normalized_id": "CHEBI:15365", "normalized_label": "aspirin",
         "alternate_ids": ["PUBCHEM.COMPOUND:2244"],
         "grounding_confidence": 0.95, "grounding_service": "nameres",
         "grounding_status": "accepted", "approval_date": "19950601"},
    ]
    ob_dir = tmp_path / "orangebook"
    ob_dir.mkdir()
    ema_dir = tmp_path / "ema"
    ema_dir.mkdir()
    (ob_dir / "orangebook.yaml").write_text(yaml.dump(source1))
    (ema_dir / "ema.yaml").write_text(yaml.dump(source2))

    result = merge_drugs(kb_dir=tmp_path, output_path=tmp_path / "drug_list.yaml")
    assert len(result) == 1
    drug = result[0]
    assert pv.drug_id(drug) == "CHEBI:15365"
    assert "ASPIRIN" in drug["source_ingredients"]
    assert "ACETYLSALICYLIC ACID" in drug["source_ingredients"]
    assert pv.approved_authorities(drug) == {"FDA", "EMA"}
    assert "DRUGBANK:DB00945" in drug["alternate_ids"]
    assert "PUBCHEM.COMPOUND:2244" in drug["alternate_ids"]
    assert pv.earliest_approval_date(drug) == "19900101"  # earliest


def test_merge_filters_unresolved(tmp_path):
    source = [
        {"source": "ORANGEBOOK", "source_name": "UNKNOWN_DRUG",
         "normalized_id": "CHEBI:99999", "normalized_label": "unknown",
         "alternate_ids": [], "grounding_confidence": 0.3,
         "grounding_service": "nameres", "grounding_status": "unresolved"},
    ]
    ob_dir = tmp_path / "orangebook"
    ob_dir.mkdir()
    (ob_dir / "orangebook.yaml").write_text(yaml.dump(source))

    result = merge_drugs(kb_dir=tmp_path, output_path=tmp_path / "drug_list.yaml")
    assert len(result) == 0  # unresolved drugs filtered out


def test_merge_marketing_status(tmp_path):
    source = [
        {"source": "ORANGEBOOK", "source_name": "DRUG_A",
         "normalized_id": "CHEBI:1", "normalized_label": "drug_a",
         "alternate_ids": [], "grounding_confidence": 0.98,
         "grounding_service": "oak", "grounding_status": "accepted",
         "marketing_status_usa": "RX"},
        {"source": "PURPLEBOOK", "source_name": "DRUG_A",
         "normalized_id": "CHEBI:1", "normalized_label": "drug_a",
         "alternate_ids": [], "grounding_confidence": 0.98,
         "grounding_service": "oak", "grounding_status": "accepted",
         "marketing_status_usa": "OTC"},
    ]
    ob_dir = tmp_path / "orangebook"
    ob_dir.mkdir()
    (ob_dir / "orangebook.yaml").write_text(yaml.dump(source))

    result = merge_drugs(kb_dir=tmp_path, output_path=tmp_path / "drug_list.yaml")
    assert len(result) == 1
    assert pv.marketing_status_usa(result[0]) == "OTC"  # most permissive


def test_merge_builds_mention_approvals_reliability(tmp_path):
    # English OB drug with a structured grounding object.
    ob = [{"source": "ORANGEBOOK", "source_name": "ASPIRIN",
           "mention_id": "MEDICNE:aspirin", "original_literal": "ASPIRIN",
           "normalized_id": "CHEBI:15365", "normalized_label": "acetylsalicylic acid",
           "grounding_status": "accepted", "marketing_status_usa": "OTC",
           "approval_date": "19900101", "application_number": "0123",
           "grounding": {"original_string": "ASPIRIN", "grounded_id": "CHEBI:15365",
                         "grounded_label": "acetylsalicylic acid",
                         "grounding_quality": "lexical_exact", "confidence": 1.0},
           "normalization": {"original_id": "CHEBI:15365", "normalized_id": "CHEBI:15365",
                             "normalized_label": "acetylsalicylic acid",
                             "normalization_quality": "none"}}]
    (tmp_path / "orangebook").mkdir()
    (tmp_path / "orangebook" / "ob.yaml").write_text(yaml.dump(ob))
    result = merge_drugs(kb_dir=tmp_path, output_path=tmp_path / "drug_list.yaml")
    drug = result[0]
    # mention
    assert drug["identity"]["id"] == "MEDICNE:aspirin"
    assert drug["identity"]["resolved_id"] == "CHEBI:15365"
    assert [s["category"] for s in drug["identity"]["resolution"]["pipeline"]] == [
        "EXTRACTION", "GROUNDING", "NORMALIZATION"]
    # approvals
    appr = drug["approvals"]
    assert len(appr) == 1
    assert appr[0]["authority"] == "FDA" and appr[0]["source"] == "ORANGEBOOK"
    assert appr[0]["marketing_status"] == "OTC"
    assert appr[0]["application_number"] == "0123"
    assert appr[0]["approval_date"] == "19900101"
    # reliability: exact grounding + verifiable provenance + approved -> HIGH
    assert drug["reliability"] == "HIGH"


def test_merge_translated_drug_reliability_medium(tmp_path):
    # China drug: unreviewed machine translation caps reliability at MEDIUM.
    cn = [{"source": "CHINA", "source_name": "Candesartan Cilexetil Tablets",
           "mention_id": "MEDICNE:cand", "original_literal": "坎地沙坦酯片",
           "normalized_id": "CHEBI:3348", "normalized_label": "Candesartan cilexetil",
           "grounding_status": "accepted", "approval_date": "20200101",
           "translation": {"source_value": "坎地沙坦酯片",
                           "translation_value": "Candesartan Cilexetil Tablets",
                           "source_language": "zh", "translation_language": "en",
                           "translator": "wikidata:Q116709136",
                           "translator_expertise": "ALGORITHM",
                           "translation_status": "CANDIDATE"},
           "grounding": {"original_string": "坎地沙坦酯片", "grounded_id": "CHEBI:3348",
                         "grounded_label": "Candesartan cilexetil",
                         "grounding_quality": "lexical_exact_surgery", "confidence": 0.8},
           "normalization": {"original_id": "CHEBI:3348", "normalized_id": "CHEBI:3348",
                             "normalized_label": "Candesartan cilexetil",
                             "normalization_quality": "none"}}]
    (tmp_path / "china").mkdir()
    (tmp_path / "china" / "cn.yaml").write_text(yaml.dump(cn, allow_unicode=True))
    result = merge_drugs(kb_dir=tmp_path, output_path=tmp_path / "drug_list.yaml")
    drug = result[0]
    cats = [s["category"] for s in drug["identity"]["resolution"]["pipeline"]]
    assert cats == ["EXTRACTION", "TRANSLATION", "GROUNDING", "NORMALIZATION"]
    assert drug["identity"]["source_language"] == "zh"
    assert drug["approvals"][0]["authority"] == "NMPA_CHINA"
    assert drug["reliability"] == "MEDIUM"


def test_merge_funnels_applied_rules_onto_grounding_step():
    # The Stage-1 preprocessing rules from the SSSOM store must land on the funneled
    # GroundingStep, keyed by (subject_id, grounded_id).
    rec = {"source": "INDIA", "source_name": "Ferrous Sulphate 150mg SR",
           "mention_id": "MEDICNE:fe", "original_literal": "Ferrous Sulphate 150mg SR",
           "normalized_id": "CHEBI:75832", "normalized_label": "iron(2+) sulfate",
           "grounding_status": "accepted",
           "grounding": {"original_string": "Ferrous Sulphate 150mg SR",
                         "grounded_id": "CHEBI:75832", "subject_id": "MEDICNE:fe",
                         "grounded_label": "iron(2+) sulfate",
                         "grounding_quality": "lexical_exact_normalized", "confidence": 0.9},
           "normalization": {"original_id": "CHEBI:75832", "normalized_id": "CHEBI:75832",
                             "normalized_label": "iron(2+) sulfate",
                             "normalization_quality": "none"}}
    by_id = {("MEDICNE:fe", "CHEBI:75832"): ["formulation_strip"]}
    drug = _merge_group("CHEBI:75832", [rec], rules_lookup=(by_id, {}))
    steps = drug["identity"]["resolution"]["pipeline"]
    assert steps[0]["category"] == "EXTRACTION"
    g = next(s for s in steps if s["category"] == "GROUNDING")
    assert g["applied_rules"] == ["formulation_strip"]


def test_merge_unions_source_xrefs_like_alternate_ids():
    """A record-level `source_xrefs` list survives the merge, deduplicated across records,
    and does not leak into `alternate_ids` (which the SSSOM export publishes as exactMatch)."""
    base = {"normalized_id": "CHEBI:15365", "normalized_label": "aspirin",
            "grounding_confidence": 0.98, "grounding_status": "accepted"}
    records = [
        {**base, "source": "ORANGEBOOK", "source_name": "ASPIRIN",
         "alternate_ids": ["DRUGBANK:DB00945"], "source_xrefs": ["UNII:R16CO5Y76E"]},
        {**base, "source": "EMA", "source_name": "ACETYLSALICYLIC ACID",
         "source_xrefs": ["UNII:R16CO5Y76E", "PUBCHEM.COMPOUND:2244"]},
    ]
    drug = _merge_group("CHEBI:15365", records)
    assert drug["source_xrefs"] == ["PUBCHEM.COMPOUND:2244", "UNII:R16CO5Y76E"]
    assert drug["alternate_ids"] == ["DRUGBANK:DB00945"]


def test_merge_omits_source_xrefs_when_no_record_has_any():
    drug = _merge_group("CHEBI:15365", [
        {"source": "EMA", "source_name": "ASPIRIN", "normalized_id": "CHEBI:15365",
         "normalized_label": "aspirin", "grounding_confidence": 0.98,
         "grounding_status": "accepted"},
    ])
    assert "source_xrefs" not in drug

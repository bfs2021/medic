# A complete Biolink KGX export for MeDIC

*Date: 2026-08-13* · **Status:** implemented (`src/medic/export/kgx/`, 66 tests)
· **Biolink Model pinned at:** `4.4.6` (was `4.3.7` until 2026-10-23; see §12)

> **As-built note.** Sections 4–5 record the design *as implemented*. Six decisions changed
> during implementation, each because checking the model contradicted the draft; they are
> marked **[revised]** and explained inline. The build this produced is in §11.

MeDIC's KGX export is the format most downstream consumers will actually read. Today it
ships 6,554 nodes carrying three properties each and 8,737 edges carrying five, while the
products behind it hold 12,694 source-scoped assertions with full transformation-provenance
trails, per-document URLs, verbatim source literals, snippets, confidence breakdowns and
reliability tiers, plus a 23,148-entry disease list that the export never touches.

This spec closes that gap: **every node and every edge MeDIC knows about, exported once, in
a form Translator can ingest unchanged.**

---

## 1. What is wrong with the current export

Each of these was verified against `biolink-model` v4.3.7 — the version already pinned in
the project's `export` dependency group — and against the current `exports/*.jsonl`.

| # | Defect | Evidence |
|---|---|---|
| D-1 | **`biolink:contraindicated_for` is not a Biolink predicate.** Biolink 4.x names it `contraindicated_in`. Every contraindication edge MeDIC ships carries an invalid predicate. | `export/kgx.py:75`; `biolink-model.yaml` defines `contraindicated in`, no `contraindicated for` |
| D-2 | **`agent_type: manual_agent` is asserted on every edge**, including DailyMed indications produced by LLM extraction over SPL text. The truthful value is already recorded per assertion (`assertion.method`, `assertion.agent.agent_type`) and is simply discarded. | `export/kgx.py:81`; `products/indication_list.yaml` assertions carry `method: LLM`, `agent_type: AI_AGENT` |
| D-3 | **`primary_knowledge_source` is emitted as a list.** Biolink defines it as single-valued — an edge has exactly one primary source. Collapsing several jurisdictions onto one edge forces the violation. | `export/kgx.py:94` |
| D-4 | **Edges have no `id`**, so they cannot be referenced, deduplicated or diffed between builds. | `export/kgx.py:77-96` |
| D-5 | **No evidence reaches the graph**: no snippet, no document URL, no publication, no approval date, no confidence, no reliability tier. The entire evidence model is dropped. | `export/kgx.py:77-96` |
| D-6 | **The disease list is not exported.** Disease nodes exist only as a side effect of appearing in an indication; 23,148 curated diseases with definitions, synonyms, xrefs and 26 filter flags are absent. | `export/kgx.py:66-71` |
| D-7 | **Research and adverse-event products are not exported at all.** Two of the six products in the SPEC §2 output contract are missing from the graph. | `export/kgx.py` reads only `drug_list`, `indication_list`, `contraindication_list` |
| D-8 | **No validation, no tests, no metadata.** Nothing checks that emitted categories/predicates/slots exist in any Biolink version; there is no `tests/test_kgx_export.py`. | `tests/` contains no KGX test |
| D-9 | `koza` and `biolink-model` are declared in the `export` extra and never imported. | `pyproject.toml`; no import in `src/` |

D-1 through D-3 are correctness bugs a Translator ingest would either reject or silently
mis-model. The rest are completeness gaps.

---

## 2. Design decisions

These four were settled before drafting and shape everything below.

**2.1 One edge per source assertion, not per pair.**
Each edge is single-sourced by construction — the same property invariant I-10 already
enforces on `SourceAssertion`. That gives every edge exactly one `primary_knowledge_source`
(fixing D-3), one document, one snippet, one set of mentions, one confidence. Pair-level
aggregates are *repeated onto each edge* (§5.4) so a consumer who wants the collapsed
drug–disease view recovers it by grouping on `(subject, predicate, object)` without
re-deriving anything. Expected volume: ~12,694 association edges against today's 8,737.

*Rejected:* one edge per pair (loses per-source attribution, forces parallel arrays that
cannot be re-associated); emitting both grains (doubles output and maintenance for a view
that is a `GROUP BY`).

**2.2 All 23,148 diseases are exported, connected or not.**
The disease list is a MeDIC product with real curated content. Most disease nodes will have
no edges; that is a true statement about MeDIC's coverage, and hiding it would misrepresent
the knowledge base. Consumers who want only the connected subgraph can filter.

**2.3 Provenance as flat summary + `MEDICNE` join keys.**
Edges carry the verbatim source literals, mention ids, grounding quality, applied
preprocessing rules, failure-mode flags, the four confidence components and the reliability
tier — all as flat scalars or lists of scalars, so they survive KGX's TSV serialization. The
*full* step trail stays where it already lives: the git-tracked `mappings/*.sssom.tsv` and
`drug_translation.babelon.tsv` decision stores, joinable on the `MEDICNE:` mention id (I-9).
Those stores ship with the release. Invariant I-8 replayability is therefore preserved at
the graph level without embedding JSON blobs in edge properties.

*Rejected:* JSON-encoded full trails (opaque to every query engine, ~10× edge size);
minimal provenance (discards MeDIC's main differentiator over other drug–disease KGs).

**2.4 Translator/Monarch ingest is the primary consumer.**
Consequences: the Biolink version is pinned and every emitted category, predicate and
un-namespaced slot is checked against it by a test (§7); extension properties are namespaced
`medic_*` so a strict consumer can ignore them wholesale; `infores:medic` gets a registry
entry (§8.3).

**2.5 No `koza`.**
Koza's value is declarative *raw source → KGX* ingest configuration. MeDIC's input is
already normalized, schema-validated YAML products, so koza would add a configuration layer
over data that needs no parsing, and its transform model does not express the
assertion-to-edge fan-out cleanly. A plain deterministic writer is smaller and easier to
test. **Action: drop `koza` from the `export` extra; keep `biolink-model` (now genuinely
used by the validation gate).**

---

## 3. Module layout

`src/medic/export/kgx.py` (129 lines) becomes a package, because the mapping tables alone
exceed what belongs in one file with the writers:

```
src/medic/export/kgx/
├── __init__.py        # public API: export_kgx()
├── __main__.py        # CLI entry point (just export-kgx)
├── biolink.py         # pinned vocabulary: categories, predicates, infores, enums, prefix map
├── nodes.py           # drug / disease / stub node builders
├── edges.py           # assertion / research / adverse-event edge builders
├── metadata.py        # KGX content metadata + infores catalog entry
├── validate.py        # the conformance gate (just validate-kgx)
└── writer.py          # deterministic JSONL serialization
```

Everything reads products through `src/medic/product_view.py` (the single read-side
accessor, per the established convention) and never touches the flat legacy shapes.

---

## 4. Node model

Two product-backed node types plus a stub type for referential closure.

### 4.1 Drug nodes — from `products/drug_list.yaml`

| KGX property | Biolink? | Source |
|---|---|---|
| `id` | ✔ | `pv.drug_id(drug)` — canonical CHEBI CURIE |
| `name` | ✔ | `pv.drug_label(drug)` |
| `category` | ✔ | `["biolink:Drug", "biolink:ChemicalEntity"]` |
| `description` | ✔ | `drug_class` / `therapeutic_area` / `drug_function` composed, when present |
| `synonym` | ✔ | `source_ingredients` ∪ `synonyms` ∪ `identity.original_literal`, deduplicated |
| `xref` | ✔ | `alternate_ids` ∪ `drugbank_id` |
| `provided_by` | ✔ | `["infores:medic"]` |
| `medic_smiles` | ✗ | `smiles` (Biolink has no SMILES slot) |
| `medic_atc_codes` | ✗ | `atc.codes` |
| `medic_atc_level1` … `medic_atc_level5`, `medic_atc_main` | ✗ | `atc.*` |
| `medic_features` | ✗ | `features` (the `DrugFeatureEnum` list) |
| `medic_is_combination_therapy`, `medic_combination_ingredients`, `medic_combination_ingredient_ids` | ✗ | combination fields |
| `medic_approved_authorities` | ✗ | `sorted(pv.approved_authorities(drug))` |
| `medic_approved_jurisdictions` | ✗ | `sorted(pv.approved_jurisdictions(drug))` |
| `medic_earliest_approval_date` | ✗ | `pv.earliest_approval_date(drug)` |
| `medic_application_numbers` | ✗ | `pv.application_numbers(drug)` |
| `medic_marketing_status_usa` | ✗ | `pv.marketing_status_usa(drug)` (raw enum value) |
| `medic_regulatory_document_urls` | ✗ | `approvals[].regulatory_document_url`, deduplicated |
| `medic_reliability` | ✗ | `score_reliability(drug)` |
| `medic_mention_id` | ✗ | `identity.id` — the `MEDICNE:` join key |
| `medic_original_literal` | ✗ | `identity.original_literal` (I-7 verbatim) |
| `medic_mention_source` | ✗ | `identity.mention_source` |
| `medic_grounding_quality` | ✗ | quality of the `GROUNDING` step in `identity.resolution.pipeline` |
| `medic_resolution_confidence` | ✗ | `identity.resolution.confidence` |

**Drug approvals stay node properties.** There is no Biolink predicate for
"drug ↔ regulatory authority approval" that would not be a misuse of an existing one, and
inventing a `medic:` predicate would produce edges no Biolink consumer can interpret.

**[revised] `highest_FDA_approval_status` is not emitted.** The draft mapped MeDIC's
`marketing_status_usa` onto it. Its range is Biolink's `ApprovalStatusEnum`, whose values
are FDA *review pathways* — `fda_fast_track`, `fda_accelerated_approval`, `fda_priority_review`,
`regular_fda_approval` — not marketing status. MeDIC knows RX/OTC/DISCN and does not know
which pathway a drug took, so filling the slot would mean inventing knowledge. The real
value stays on `medic_marketing_status_usa`. This also resolves the open question raised in
§10 of the draft.

### 4.2 Disease nodes — from `products/disease_list.yaml`

All 23,148, per §2.2.

| KGX property | Biolink? | Source |
|---|---|---|
| `id` | ✔ | `category_class` — MONDO CURIE |
| `name` | ✔ | `label` |
| `category` | ✔ | `["biolink:Disease"]` |
| `description` | ✔ | `definition` |
| `synonym` | ✔ | `synonyms` |
| `xref` | ✔ | `crossreferences` |
| `provided_by` | ✔ | `["infores:medic"]` |
| `subsets` | ✔ (since 4.4.6; was `medic_subsets`) | `subsets` |
| `medic_f_<flag>` × 26 | ✗ | every `f_*` boolean, emitted only when `True` |

Only true flags are emitted. Writing 26 booleans onto 23,148 nodes would add roughly 12 MB
of `false` to the file for no information.

> **Note for review:** SPEC §2 lists the disease-list product path as
> `kb/diseases/disease_list.yaml`, but `merge/disease_merge.py` writes
> `products/disease_list.yaml` and that is the file `validate-all` checks. The two files on
> disk differ. This export reads `products/disease_list.yaml` (the merged product) and falls
> back to the `kb/` path. **Follow-up issue filed** — the SPEC needs correcting, not this
> export.

### 4.3 Stub nodes — referential closure

Any edge endpoint not present in a product gets a minimal node: `id`, inferred `category`,
`name` from whatever label the association carries, `provided_by: ["infores:medic"]`, and
`medic_stub: true`. This matters because `kgx validate` treats a dangling edge endpoint as
an error, and MeDIC genuinely produces such endpoints: research associations carry
`UNII:` drug ids and `ORPHA:`/`UMLS:` disease ids that Stage-2 normalization could not map
to CHEBI/MONDO (SPEC §11, "non-Mondo disease IDs").

Category inference by prefix:

| Prefix | Category |
|---|---|
| `MONDO`, `ORPHA`, `UMLS`, `DOID`, `OMIM` | `biolink:Disease` |
| `HP` | `biolink:PhenotypicFeature` |
| `MedDRA` | `biolink:DiseaseOrPhenotypicFeature` |
| `CHEBI`, `DRON`, `UNII`, `RXNORM`, `DRUGBANK`, `PUBCHEM.COMPOUND`, `CHEMBL.COMPOUND` | `biolink:ChemicalEntity` |
| anything else | `biolink:NamedThing`, and the validator reports it |

Stub count is reported in the build log and in the metadata file — it is a coverage metric,
not an implementation detail to hide.

---

## 5. Edge model

### 5.1 Edge identity

```
MEDICEDGE:<uuid5(MEDICEDGE_NAMESPACE, "subject\tpredicate\tobject\tprimary_knowledge_source\tdocument")>
```

Deterministic, offline, byte-identical across reruns — mirroring `medic.mention`'s
`MEDICNE:` minting, including a fixed namespace constant derived from a stdlib namespace
(never random). Two builds of unchanged products produce identical edge ids, so releases
diff cleanly.

### 5.2 Predicate selection

Every predicate below was confirmed present in `biolink-model` 4.3.7.

| MeDIC statement | Predicate | `knowledge_level` |
|---|---|---|
| Indication, `regulatory_status.status == APPROVED` | `biolink:treats` | `knowledge_assertion` |
| Indication, status `INVESTIGATIONAL` / `OFF_LABEL` / absent | `biolink:treats_or_applied_or_studied_to_treat` | `not_provided` |
| Indication, status `WITHDRAWN` | `biolink:treats_or_applied_or_studied_to_treat` (+ `medic_withdrawn: true`) | `knowledge_assertion` |
| Contraindication | `biolink:contraindicated_in` | `knowledge_assertion` |
| Research — CURE-ID real-world case reports | `biolink:applied_to_treat` | `observation` |
| Research — trial evidence (`max_research_phase` ∈ PHASE_I..PHASE_IV, or `study_status` set) | `biolink:in_clinical_trials_for` | `observation` |
| Research — other literature | `biolink:studied_to_treat` | `observation` |
| Adverse event — PVLens (label-listed) | `biolink:has_side_effect` | `knowledge_assertion` |
| Adverse event — FAERS (post-market reports) | `biolink:has_adverse_event` | `observation` |

Two of these deserve their justification recorded, because they are the ones a reviewer
should challenge:

**Why `biolink:treats` for regulatory indications.** Biolink restricts asserted `treats`
edges to cases with strong supporting evidence, and names the qualifying case explicitly:
*"in some population(s) the intervention is approved for the condition"*. A regulator's
approved indication is the textbook instance. Indications whose regulatory status is not
`APPROVED` do not qualify and drop to the grouping predicate.

**Why PVLens and FAERS get different predicates.** Biolink distinguishes `has side effect`
("an unintended, but predictable, secondary effect… Side effects are listed on drug labels")
from `has adverse event` ("an untoward medical occurrence… may be caused by something other
than the drug"). That is precisely the PVLens (label-mined) versus FAERS (spontaneous
post-market report) distinction. Mapping both to one predicate would erase it.

`biolink:contraindicated_in` has range `biological entity`, which admits `biolink:Disease` —
no range violation.

**[revised] Research predicates are selected from recorded data, not from the source's
name.** The draft said "CURE-ID → `applied_to_treat`". The implementation keys on
`max_research_phase` instead: `CASE_REPORT` → `applied_to_treat` (which is exactly Biolink's
"actually taken by one or more patients"), a numbered phase or a set `study_status` →
`in_clinical_trials_for`, anything else → `studied_to_treat`. Same outcome for CURE-ID, but
expressed on data — which is the rule `medic/reliability.py` already follows, so the
idiosyncrasies of a source name never leak into the semantics, and any future case-report
source gets the right predicate for free.

### 5.3 `agent_type` — derived, not asserted

Replaces the blanket `manual_agent` (D-2). Derived per assertion from provenance MeDIC
already records:

| Assertion signal | `agent_type` |
|---|---|
| `assertion.method == "LLM"` (or `agent.agent_type == "AI_AGENT"`) | `text_mining_agent` |
| `assertion.method` ∈ {`STRUCTURED_FIELD`, `DETERMINISTIC_RULE`, `LEXICAL_MATCH`} | `data_analysis_pipeline` |
| curator `curator_type == "HUMAN"`, or review store CONFIRMED | `manual_agent` |
| nothing recorded | `not_provided` |

For DailyMed and EMA free-text indications this yields `text_mining_agent`, which is the
honest description: the regulator asserted the indication, an LLM extracted it.

### 5.4 Edge properties

**Biolink core** (un-namespaced, all validated against 4.3.7):

| Property | Source |
|---|---|
| `id` | §5.1 |
| `subject` / `object` | pair `drug_id` / `disease_id` |
| `predicate` | §5.2 |
| `primary_knowledge_source` | source artifact → infores, single-valued (`DAILYMED`→`infores:fda-dailymed`, `ORANGEBOOK`→`infores:fda-orange-book`, `PURPLEBOOK`→`infores:fda-purple-book`, `EMA_EPAR`→`infores:ema`, `PMDA`→`infores:pmda`, `CDSCO`→`infores:cdsco`, `GRLS`→`infores:grls`, `CDE_CHINA`→`infores:nmpa`; research→`infores:pubmed` / `infores:cure-id`) |
| `aggregator_knowledge_source` | `["infores:medic"]` |
| `knowledge_level` | §5.2 |
| `agent_type` | §5.3 |
| `publications` | evidence `reference` values that are `PMID:`/`PMC:`/`NCT:`/`DOI:` CURIEs; non-CURIE URLs go to `medic_reference_url` instead |
| `original_subject` | `assertion.drug.original_literal` — the verbatim source string (I-7) |
| `original_object` | `assertion.disease.original_literal` |
| `supporting_text` | the evidence snippet, truncated at 2,000 chars |
| `supporting_text_section_type` | the role of the span the claim was read from |
| `subject_location_in_text` / `object_location_in_text` | `[char_start, char_end]` of each mention's extraction step |
| `clinical_approval_status` | regulatory status, mapped to Biolink's enum (below) |
| `max_research_phase` | evidence research phase, mapped to Biolink's enum (below) |
| `has_confidence_score` | this assertion's `confidence.overall` |
| `has_evidence` | ECO term for the evidence class, where an unambiguous one exists (regulatory label → `ECO:0000218`); omitted otherwise rather than guessed |

**[revised] Seven more properties use standard slots than the draft assumed.** A sweep of
the model before writing the builders found Biolink already defines things the draft was
about to namespace: `original_subject`/`original_object` for the verbatim literals,
`supporting_text` and `supporting_text_section_type` for the quoted label span,
`subject_location_in_text`/`object_location_in_text` for the character offsets MeDIC records
on every extraction step, and `clinical_approval_status`/`max_research_phase`/
`has_confidence_score`. Using the standard slot is always preferred; `medic_*` is the
residue Biolink cannot express. The rule is now explicit in `biolink.py`.

Two candidates were checked and **rejected**: `source_record_urls` has domain
`retrieval source`, so putting it directly on an edge is a domain violation
(`medic_document_url` instead); `severity_qualifier`/`frequency_qualifier` take ontology
terms (`severity value`, `frequency value`) while MeDIC has an enum and free text, so
mapping them would need a term table (`medic_severity`/`medic_frequency` instead).

**Enum mapping.** Biolink's enums have their own value spaces, so MeDIC's are mapped, not
passed through, and the unmapped values are preserved beside the mapped slot:

| MeDIC | Biolink `ClinicalApprovalStatusEnum` |
|---|---|
| `APPROVED` (FDA) | `fda_approved_for_condition` |
| `APPROVED` (other) | `approved_for_condition` |
| `INVESTIGATIONAL` | `not_approved_for_condition` |
| `WITHDRAWN` | `post_approval_withdrawal` |
| `OFF_LABEL` | `off_label_use` |
| `DISCONTINUED` | `not_provided` — see below |

`DISCONTINUED` deliberately degrades. Biolink's nearest value, `post_approval_withdrawal`,
reads as a safety withdrawal; MeDIC's `DISCONTINUED` means marketing ceased, which is
usually commercial. Asserting the former would tell consumers a drug was pulled for safety
when it was not. The true value survives on `medic_approval_status_raw`.

MeDIC's `ResearchPhaseEnum` maps `PRE_CLINICAL` and `PHASE_I`–`PHASE_IV` onto Biolink's
equivalents; `CASE_REPORT`, `IN_VITRO` and `COMPUTATIONAL` have no counterpart and degrade
to `not_provided`, keeping their real value on `medic_research_phase_raw`.

**MeDIC extensions** (`medic_*`, flat scalars and lists only):

*Source and document*
`medic_source`, `medic_document`, `medic_jurisdiction`, `medic_authority`,
`medic_source_role`, `medic_regulatory_status`, `medic_document_url`,
`medic_source_document_url`, `medic_approval_date`, `medic_setid`,
`medic_application_number`

*Claim*
`medic_snippet` (the evidence quote, truncated at 2,000 chars with `medic_snippet_truncated`
set), `medic_explanation`, `medic_span_role`, `medic_trigger_cue`, `medic_trigger_span`,
`medic_negated`, `medic_assertion_flags`, `medic_assertion_method`, `medic_agent_name`,
`medic_agent_version`, `medic_tool`, `medic_tool_version`

*Confidence and quality*
`medic_confidence_subject`, `medic_confidence_object`, `medic_confidence_relationship`,
`medic_confidence_overall`, `medic_confidence_basis`, `medic_reliability`

*Resolution join keys (§2.3)*
`medic_subject_mention_id`, `medic_object_mention_id`, `medic_subject_grounding_quality`,
`medic_object_grounding_quality`, `medic_subject_applied_rules`,
`medic_object_applied_rules`, `medic_subject_grounding_flags`,
`medic_object_grounding_flags`, `medic_subject_translated`, `medic_object_translated`

*Pair aggregates — repeated on every edge of the pair (§2.1)*
`medic_pair_confidence` (noisy-OR, I-13), `medic_pair_n_assertions`,
`medic_pair_reliability`, `medic_pair_jurisdictions`, `medic_pair_authorities`

*Contraindication-specific*
`medic_is_allergen`, `medic_is_diagnostic_agent`

**Per-edge reliability** is computed by feeding `reliability.score_reliability` a synthetic
single-assertion record (`{**pair_scalars, "assertions": [this_assertion]}`). The existing
gates then apply unchanged to exactly one assertion's provenance — no scoring logic is
duplicated in the exporter.

### 5.5 Hyperrelational context — reserved, not emitted

Spec `2026-08-13-hyperrelational-context-integration-design.md` (issue #9) will populate
claim qualifiers — `patient_type`, `stage`, `coadministration`, `previous_history`,
`mutation` — that are exactly what Biolink's qualifier model exists for. Zero of the 12,694
current assertions carry a `hyperrelations` payload, so this export emits nothing for them.

The mapping is fixed now so that landing #9 is a data change rather than a schema
negotiation:

| Hyperrelational key | Target |
|---|---|
| `patient_type` | `medic_context_patient_type` |
| `stage` | `medic_context_stage` |
| `coadministration` | `medic_context_coadministration` |
| `previous_history` | `medic_context_previous_history` |
| `additional_details` | `medic_context_additional_details` |

**[revised] None of these can use a Biolink qualifier.** The draft proposed
`subject_form_or_variant_qualifier` for `patient_type` and `object_aspect_qualifier` for
`stage`. Checking the model rules both out: `population_context_qualifier` (the natural fit
for `patient_type`) has range `population of individual organisms` — an ontology class, not
the string enum `adult`/`pediatric`/`pregnant` the extractor produces — and
`stage_qualifier` means "stage during which gene or protein expression takes place", which
is developmental stage, not disease stage. Mapping either would require a term table that
does not exist. All five keys therefore stay namespaced until someone builds that mapping;
that is a decision for whoever lands #9, and it is recorded here so it is made deliberately
rather than by accident.

A test asserts no qualifier or `medic_context_*` property is emitted while the payloads are
empty, so the reservation cannot silently rot into dead code that half-fires.

### 5.6 What is deliberately not an edge

- **Drug approvals** → node properties (§4.1).
- **ATC classifications** → node properties, not class nodes with membership edges. ATC
  classes are not in the products; minting them is graph construction beyond exporting what
  MeDIC knows.
- **Combination-therapy ingredients** → node properties. The component CURIEs are recorded,
  but MeDIC does not assert a curated `has_part` relation and the export should not invent
  one.
- **Disease cross-references** → `xref` node property, not `same_as` edges.

Each of these is a defensible later addition; none is an *export* of existing MeDIC
knowledge, which is this spec's scope.

---

## 6. Outputs

| File | Content |
|---|---|
| `exports/medic_nodes.jsonl` | one JSON object per node, sorted by `id` |
| `exports/medic_edges.jsonl` | one JSON object per edge, sorted by `(subject, predicate, object, primary_knowledge_source, document)` |
| `exports/medic_kgx_metadata.yaml` | KGX content metadata (§8.1) |
| `exports/infores_medic.yaml` | proposed Translator information-resource entry (§8.3) |

Deterministic serialization: keys sorted within each object, records sorted as above, no
timestamps in the node/edge files (build date lives only in the metadata file). Two builds
of unchanged products produce byte-identical JSONL — checked by a test.

Estimated size: nodes ~25 MB (dominated by 23,148 disease definitions), edges ~20 MB
(dominated by snippets). Both are well within what consumers handle uncompressed; gzip is
not added until someone asks.

---

## 7. Validation gate

`just export-kgx` runs the gate on what it just built and exits non-zero on any error, so
`build-all` is covered without a second pass; `just validate-kgx` re-checks an
already-built export standalone. Both are backed by `tests/test_kgx_export.py`.

1. **Biolink conformance.** Load the pinned `biolink-model` YAML. Every `category` must be
   a class in the model; every `predicate` must be a slot with `predicate` as an ancestor or
   mixin; every un-namespaced node/edge property must be a slot in the model. Failure is an
   error.
2. **No namespace collision.** No `medic_*` key may match a Biolink slot name with
   underscores substituted. Failure is an error — it would mean an extension is shadowing a
   standard slot.
3. **Referential closure.** Every `subject`/`object` resolves to a node in the node file.
   Failure is an error.
4. **Single-valued discipline.** `primary_knowledge_source`, `knowledge_level`, `agent_type`
   are scalars, not lists (D-3 regression guard).
5. **Prefix sanity.** Node ids whose prefix is absent from the target category's Biolink
   `id_prefixes` are reported as warnings with counts (not errors — MeDIC legitimately
   carries `DRON:` and `MedDRA:` ids that Biolink's prefix lists omit).
6. **Determinism.** Re-running the export over unchanged products yields byte-identical
   files.
7. **Source isolation (I-1).** No edge may carry a `medic_jurisdiction` that disagrees with
   its `medic_source`'s own jurisdiction. This is the export-side echo of the project's
   hardest invariant, and it is cheap to check here.

---

## 8. Metadata and registration

**8.1 Content metadata** — `exports/medic_kgx_metadata.yaml`: MeDIC release version, Biolink
version, build date, node counts by category, edge counts by predicate and by
`primary_knowledge_source`, stub-node count, and the list of contributing sources with their
jurisdictions and roles.

**8.2 Release README** — the KGX section documents the `medic_*` extension vocabulary and
the `MEDICNE:` join into the shipped `mappings/` stores, so a consumer can follow a claim
from an edge back to the verbatim regulatory string without reading the source code.

**8.3 `infores:medic`** — MeDIC currently emits `aggregator_knowledge_source: infores:medic`
without a registry entry. `exports/infores_medic.yaml` is generated as a proposed entry for
submission to the Translator information-resource registry. Submitting it is a separate,
human-driven step; the export just prepares it.

**[revised] A second infores is needed: `infores:medic-research-curation`.** Some
deep-research associations cite only a bare website, so no external resource can be named as
the primary source. The first build attributed those to `infores:medic` — the same id the
edge already carries as its *aggregator*, which tells a consumer nothing and implies MeDIC
republished a claim it in fact asserted itself. Those edges are now attributed to MeDIC's
curation pipeline explicitly, and the entry is proposed alongside the main one.

When a *regulatory* source cannot be mapped to a known infores, the export falls back to the
aggregator rather than inventing an unregistered id — and the gate emits a **warning with a
count**, so the gap stays visible instead of hiding behind a plausible-looking identifier.
The current build has zero such edges.

---

## 9. Implementation phases

Each phase is independently reviewable and leaves the tree green.

| Phase | Content | Done when |
|---|---|---|
| **1. Vocabulary + gate** | `biolink.py` mapping tables; `validate.py`; `just validate-kgx`; tests over the tables. No exporter change yet. | Gate runs against the *existing* export and fails on D-1/D-3 — proving it detects real defects |
| **2. Nodes** | `nodes.py`: drugs (full property set), all diseases, stub nodes, closure. | Node file carries 4,250 drugs + 23,148 diseases + stubs; gate passes on nodes |
| **3. Association edges** | `edges.py`: assertion-grained indication + contraindication edges, all properties, per-edge reliability, deterministic ids. | ~12,694 edges; gate passes; determinism test passes |
| **4. Research + AE** | Research predicate selection from evidence; PVLens/FAERS AE builder (exercised by fixtures, since the product is an empty stub). | Research edges present; AE builder unit-tested against a synthetic product |
| **5. Metadata + docs** | `metadata.py`, infores entry, `docs/architecture.md` §11.2 rewrite, SPEC §2 export line, README. | `build-all` green end to end |

---

## 10. Risks and open questions

- **Volume of `treats` assertions.** ~12.7k assertion edges collapse to ~8.7k pairs. A
  consumer that does not group will double-count corroborated pairs. Mitigated by
  `medic_pair_n_assertions` on every edge and an explicit note in the release README, but it
  is the main consumer-facing consequence of decision 2.1 and reviewers should weigh it.
- **`has_evidence` ECO terms.** Only the regulatory-label case has an unambiguous ECO term.
  The slot is omitted elsewhere rather than populated with a plausible guess; a curator may
  later want a fuller ECO mapping table.
- **Stub nodes expose the normalization gap.** Making unmapped `UNII:`/`ORPHA:`/`UMLS:`
  endpoints visible as stubs is deliberate, but it will make the coverage gap in SPEC §11
  legible to consumers for the first time. That is the correct outcome; it is worth saying
  out loud before release.
- **Snippet licensing.** Edges will carry verbatim quotes from regulatory labels. All
  contributing sources are government publications covered by `LICENSING.md`, but shipping
  label text at scale is a broader distribution than the current exports do, and is worth a
  second look before release.

---

## 11. As built

Implemented across `src/medic/export/kgx/` (`biolink`, `nodes`, `edges`, `writer`,
`metadata`, `validate`, `__main__`), with 66 tests in `tests/test_kgx_export.py`. Full suite:
644 passed.

Build over the current products (`just export-kgx`):

| | Before | After |
|---|---|---|
| Nodes | 6,554 | **28,821** (4,323 drugs · 23,148 diseases · 1,274 stubs · 262 phenotypes) |
| Edges | 8,737 | **12,858** |
| Node properties | 3 | up to 30 |
| Edge properties | 5 | up to 45 |
| Predicates used | 2 (one invalid) | 4, all valid in 4.3.7 |
| Files | 2 | 4 (+ metadata, + infores entry) |
| Size | 2.9 MB | 57 MB (25 MB nodes, 32 MB edges) |

Edges by predicate: `biolink:treats` 9,716 · `biolink:contraindicated_in` 2,978 ·
`biolink:studied_to_treat` 164. By reliability: HIGH 10,672 · MEDIUM 1,455 · LOW 731.
By source: DailyMed 6,888 · EMA 3,054 · PMDA 2,620 · PubMed 159 · CDSCO 132 · MeDIC
curation 5.

Gate result: **conformant**, 2 warnings — 413 nodes use the `DRON:` prefix, which Biolink
does not list for `ChemicalEntity`/`Drug`. That is expected and documented (§7.5).

### Two data gaps this surfaced

Neither is an export defect; both are visible now because the export reads products that
were previously ignored.

1. **No research edge reaches `applied_to_treat` or `in_clinical_trials_for`**, because no
   evidence row in `products/research_list.yaml` carries `max_research_phase`. Both code
   paths are unit-tested and will light up as soon as the data does.
2. **CURE-ID has never been ingested** — `kb/research/cureid_associations.yaml` does not
   exist, so `just ingest-cureid` has not been run against the current tree. CURE-ID is the
   one source that sets `max_research_phase: CASE_REPORT`, so running it is what would
   populate `applied_to_treat`.

Both are filed as issues.

## 12. Revised 2026-10-23: built through the Biolink pydantic model

Review of the v2.0.0-rc1 graph in a KG build found three things this spec's "two layers"
design had let through: edges carried no association `category`; the extension layer had
grown to 62 edge and 30 node `medic_*` properties, most of them derivable, constant across
the build, or replayable from the products; and the document URLs sat in flat `medic_*`
properties although Biolink has a slot for them.

**Construction.** Every node and edge is now an instance of the matching `biolink-model`
pydantic class, dumped to JSON (`edges._build`, `nodes._build`). The class is strict
(`extra="forbid"`, enum validation, required slots, `category` fixed to the class), so the
guard rail is at construction rather than only at the gate. The gate re-validates every
record through the class its `category` names, and additionally checks the one rule the
class cannot — that the predicate specialises what the class's `slot_usage` restricts it to.

**Association classes** (`biolink.ASSOCIATION_CLASS`): the treats family, including
`preventative_for_condition` (a `treats` through its mixin) and the research predicates, is
`ChemicalOrDrugOrTreatmentToDiseaseOrPhenotypicFeatureAssociation`; `contraindicated_in`
and `diagnoses`, which no class restricts to, are
`ChemicalEntityToDiseaseOrPhenotypicFeatureAssociation`; adverse events use the two
dedicated classes. Nodes carry one category, the class's own (`biolink:Drug`, not
`[Drug, ChemicalEntity]` — the class literal rejects the pair).

**Off-class slots.** Biolink defines but does not attach to every class here:
`clinical_approval_status` and `max_research_phase` (4.3.7 had them on the contraindication
class only; 4.4.6 moved them to the `treats` class, so they are now constructor-validated on
49,789 edges and bypass it on the 676 contraindication edges that carry one),
`temporal_context_qualifier` / `sex_qualifier` (exposure and phenotype associations only),
`subject_location_in_text` / `object_location_in_text` (`TextMiningStudyResult` only). They
are written beside the dumped core where the class lacks them, listed once as
`biolink.OFF_CLASS_EDGE_SLOTS` with the reason, and pinned by a test.
`population_context_qualifier` was in the list under 4.3.7; 4.4.6 attaches it to all four
classes, so it now goes through the constructor.
`supporting_text_section_type` was in the same position and was dropped: it is recoverable
from `medic_label_section`, which names the SPL section the span came from, so it clears
the same bar as the rest of the removed list. `sex_qualifier` now takes the PATO term, the
slot's range being a class.

**`disease_context_qualifier` carries MONDO and nothing else (#110).** The slot's range is
`disease`, and neither guard on the way out enforces that. UMLS and HP are on the `disease`
class's `id_prefixes`, so the pydantic constructor accepts them; the field is typed as a
plain `str`, so it accepts free text. On the 95,170-edge build the slot was populated 20,342
times and only 10,314 of those were MONDO — 3,702 UMLS, 1,618 HP, and 4,708 free text over
1,022 distinct strings. Much of the free text is not a disease at all
(`radiological examination of the stomach and/or small intestine`), and several strings are
wording variants of one context (`non-neutropenic`, `non-neutropenic patients`), which also
splits otherwise identical edges.

Narrowed to MONDO rather than to the class's whole prefix list, so a context is a node in
the same disease space the edge's object already sits in — which is what a consumer joins
on. The rule takes the **first MONDO term among the grounded ids**, not the first id if it
happens to be MONDO: the ids arrive in the order the label named the contexts, which carries
no priority, and the positional reading blanks the slot on 481 edges that do have a MONDO
context sitting behind a CUI.

Nothing is dropped. Every grounded id, whatever its prefix, stays on
`medic_disease_context_ids`, and the label's own wording is on `medic_disease_context` —
one property added back to the extension layer, because by that layer's own bar the
verbatim context is not on a Biolink slot on the record once this slot is MONDO-only, is
not constant across the build, and is not replayable through a mention id, since a context
is never minted one. The two are **positionally aligned**, which is the contract
`record_claim_context` already writes in the products: the id at index *i* grounds the name
at index *i*, and is `""` where it did not ground. The edge builder used to compact the id
list, which was harmless while the names were not on the edge; with both there it would
re-pair every name after an ungrounded one with the wrong CURIE. The conformance gate
rejects any non-MONDO value on the slot, so a file from an older build or an edited export
is held to the rule too.

The UMLS and HP residue is **not** a context-specific grounding gap: `ground_context_names`
already resolves through `resolve_disease_onto_record`, the same two-stage
grounding + normalisation the edge objects get. A context that stays a CUI is the same
normalisation gap as an object that stays a CUI, so it is fixed there or not at all.

**`sources`.** The document CURIE, landing page and PDF URL go on one nested
`RetrievalSource` for the primary knowledge source as `source_record_urls` — the slot's
domain is `retrieval source`, which is why §5.4 had rejected putting it flat on the edge.
This is the only nested payload Biolink defines for association provenance, and it replaces
`medic_document`, `medic_document_url`, `medic_source_document_url`, `medic_reference`,
`medic_reference_url` and `medic_setid`. `supporting_text` is a one-element list: the slot
is multivalued in 4.3.7 and 4.4.6, and §5.4's "single-valued" claim was wrong.

A nested object is representable in KGX JSONL but not in KGX TSV: a TSV sink will stringify
the `sources` column. A consumer merging through TSV should take the document CURIE from
`source_record_urls[0]` before converting, or merge from the JSONL.

**Extension layer, after.** Edge: `medic_source`, `medic_jurisdiction`,
`medic_approval_status_raw`, `medic_approval_date`, `medic_assertion_flags`,
`medic_supporting_text_truncated`, `medic_subject_mention_id`, `medic_object_mention_id`,
`medic_pair_confidence`, `medic_pair_n_assertions`, `medic_pair_reliability`,
`medic_reliability`, `medic_disease_context`, `medic_disease_context_ids`,
`medic_co_administered_with`,
`medic_claim_relation`, `medic_is_allergen`, `medic_is_diagnostic_agent`,
`medic_research_phase_raw`, `medic_curation_status`, `medic_severity`, `medic_frequency`,
`medic_label_section`. Node: `medic_smiles`, `medic_atc_codes`, `medic_features`,
`medic_is_combination_therapy`, `medic_combination_ingredient_ids`,
`medic_approved_authorities`, `medic_approved_jurisdictions`,
`medic_earliest_approval_date`, `medic_marketing_status_usa`, `medic_application_numbers`,
`medic_registration_numbers`, `medic_reliability`, `medic_mention_id`, `medic_stub`,
`medic_f_*`.

Removed, and why: `medic_explanation` → the metadata's `source_explanations` (a regulatory
assertion's explanation is the ingester's one sentence per source, six distinct strings
across 70,000 edges, the same build-level shape as the agent columns; research edges keep
their per-claim explanation as `description`); `medic_marketing_status_usa`
additionally → `available_from` for RX/OTC; `medic_agent_name` / `_agent_version` /
`_tool` / `_tool_version` / `_assertion_method` were one value across the build and are now
`extraction_agents` in the metadata file; `medic_authority`, `medic_source_role`,
`medic_regulatory_status`, `medic_withdrawn`, `medic_negation_detected`,
`medic_pair_jurisdictions`, `medic_pair_authorities`, the ATC levels and `medic_atc_main`
were derivable from a slot on the same record or a `GROUP BY`; the five confidence
components, the per-side grounding quality / rules / flags / translated markers,
`medic_trigger_cue` / `_span`, `medic_claim_attributed_to` / `_basis`,
`medic_disease_context_labels`, `medic_support`, `medic_evidence_source`,
`medic_study_status`, `medic_reference_title`, `medic_application_number`,
`medic_combination_ingredients`, `medic_regulatory_document_urls`, `medic_original_literal`
(it is in `synonym`), `medic_mention_source`, `medic_grounding_quality` and
`medic_resolution_confidence` are the products' and the decision stores' columns, reachable
through the mention ids, which is what I-8 asks for.

**Pin moved to 4.4.6 (same day).** The question "can we use `…ToDisease` /
`…ToPhenotypicFeature` classes instead of the combo classes" has the answer *no* in both
4.3.7 and 4.4.6: the only chemical-subject association classes are the two
`…ToDiseaseOrPhenotypicFeature` combos and the two adverse-event classes, and the
object-split `EntityToDiseaseAssociation` / `EntityToPhenotypicFeatureAssociation` are
undescribed placeholders with no subject or predicate constraint and (in 4.3.7) none of
the context qualifiers. Checking that showed 4.4.6 attaches the clinical-trial and
regulatory-approval context mixin to `ChemicalOrDrugOrTreatmentToDiseaseOrPhenotypicFeatureAssociation`
and `population_context_qualifier` to every class the exporter writes, so the pin was moved:
the off-class list shrinks from seven to six, and `clinical_approval_status`,
`max_research_phase` and `population_context_qualifier` are constructor-validated on the
`treats` family. Nothing emitted changed shape; every predicate, enum value, class and slot
the exporter uses exists unchanged in 4.4.6. 4.4.6 also adds a `subsets` node slot
(`oboInOwl:inSubset` values, range string, multivalued) that is exactly what
`medic_subsets` carried on every disease node, so the MONDO subsets now ship under the
native name and `medic_subsets` is gone. Not yet used: 4.4.6's `treatment_outcome` and
`regulatory_approvals` on the same class.


# MeDIC — Specification

**Status:** living document · **Last updated:** 2026-08-19 · **Branch:** `medic2` (v2 pipeline)

This is the normative specification for MeDIC: what the system is, what it must produce, the rules
its data must obey, and the live record of decisions and outstanding work. It is the single source
of truth. For *how* the pipeline is built read [`docs/architecture.md`](docs/architecture.md); for
the source-isolation rule in full read [`docs/source-isolation.md`](docs/source-isolation.md). The
design-decision record and task ledger — formerly `docs/implementation-status.md` — now live in
§9–§11 below. When any doc disagrees with this file on a *requirement* or *invariant*, this file
wins — update the others to match.

> Maintenance: keep the **Status snapshot** (§8), **Design decisions** (§9), **Task ledger** (§10),
> and **Open items** (§11) current whenever products are rebuilt or a decision is made. Keep the
> **Requirements** and **Invariants** sections aligned with the schema and merge code — they are the
> contract, not a diary.

---

## 1. Purpose and scope

MeDIC (Medicines, Diseases, Indications, and Contraindications) is an open-source knowledge base
for drug-repurposing research (PMID:41385096, published at https://medic.renci.org). It builds
curated datasets **from government regulatory sources**, mapping drugs to diseases via approved
indications, contraindications, adverse events, and literature-derived research associations.

**In scope:** ingesting regulatory + literature sources; grounding all entities to canonical
ontology identifiers; merging across jurisdictions; emitting machine-readable products and
standard exports (KGX, SSSOM) with verifiable provenance.

**Out of scope (non-goals):**
- Clinical decision support or any patient-facing use.
- Inferring indications the regulator did not state (see Invariant I-3, no downfilling).
- Scraping sources whose terms forbid it (see Invariant I-5, WHO CC removed).
- Being a real-time service — MeDIC is a batch-built, versioned dataset.

---

## 2. Products (output contract)

MeDIC produces five products and three export families. Paths and schema classes are contractual;
consumers depend on them.

| Product | Schema class (`src/medic/schema/`) | Path | Sources | Status |
|---|---|---|---|---|
| **Drug List** | `DrugList` (`drug.yaml`) | `products/drug_list.yaml` | Orange Book, Purple Book, EMA, PMDA, GRLS, CDSCO, CDE, EveryCure | Implemented |
| **Disease List** | `DiseaseList` (`disease.yaml`) | `products/disease_list.yaml` | Mondo via `everycure/disease-list` | Implemented |
| **Indications List** | `IndicationList` (`indication.yaml`) | `products/indication_list.yaml` | DailyMed, EMA, PMDA, CDSCO (India) | Implemented |
| **Contraindications List** | `IndicationList` (`indication.yaml`) | `products/contraindication_list.yaml` | DailyMed | Implemented (FDA-only) |
| ~~Adverse Event List~~ | `AdverseEventList` (`adverse_event.yaml`) | — | PVLens, FAERS | **Deferred, not a v2.0.0 product** |
| **Research List** | `ResearchAssociationList` (`research_source.yaml`) | `products/research_list.yaml` | PubMed, deep research, CURE-ID | Placeholder (9 verified rows, #44) |

**Exports** (`exports/`): legacy CSV/XLSX matching the v1.0.0 column format (`drug_list_flexible.csv`,
`drug_list_stringent.csv`, per-source files); Biolink **KGX** (`medic_nodes.jsonl`, `medic_edges.jsonl`,
`medic_kgx_metadata.yaml`, `infores_medic.yaml` — the complete graph view of every product, Biolink
Model pinned at 4.4.6, one edge per source assertion; see
[`specs/2026-08-13-kgx-export-design.md`](specs/2026-08-13-kgx-export-design.md) and
`docs/architecture.md` §11.2); **SSSOM** drug cross-reference mappings
(`medic_drug_mappings.sssom.tsv`).

**Grounding decision stores** (`mappings/`, git-tracked, hand-editable, authoritative cache — see §6,
I-4): `disease_grounding.sssom.tsv` and `drug_grounding.sssom.tsv` (SSSOM literal profile — every
string→ID decision incl. `sssom:NoTermFound` failures; the `subject_id` column carries the mention's
`MEDICNE` id, I-9), plus `disease_normalization.sssom.tsv` and
`drug_normalization.sssom.tsv` (SSSOM term↔term — the ID→canonical-ID decisions), plus
`drug_translation.babelon.tsv` (Babelon — the Stage-0 non-English→English translation of every foreign
source mention, keyed by its `MEDICNE` id; see §6, I-8/I-9).

Release packaging: `just release` builds all products + exports; `just gh-release vX.Y.Z` uploads the
populated `products/*.yaml` and export artifacts as a GitHub release (draft by default).

---

## 3. Data sources

### 3.1 Inventory

| Source | Jurisdiction | Module | Acquisition | Role |
|---|---|---|---|---|
| Orange Book | USA (small molecule) | `medic.ingest.orangebook` | FDA ZIP download | PRIMARY |
| Purple Book | USA (biologics) | `medic.ingest.purplebook` | FDA CSV download | PRIMARY |
| EMA | EU | `medic.ingest.ema` | EMA XLSX download | PRIMARY |
| PMDA | Japan | `medic.ingest.pmda` | Consolidated PDF (`pmda.go.jp/files/000279952.pdf`) | PRIMARY |
| GRLS | Russia | `medic.ingest.russia` | Manually-provided raw GRLS export at `background/grls.zip` (8 register xlsx, Cyrillic; site IP-blocked; Russian→English DeepL-translated via the Stage-0 Babelon stage, Cyrillic-transliteration fallback) | PRIMARY |
| CDSCO | India | `medic.ingest.india` | Year-by-year PDFs (JSP-wrapped) | PRIMARY |
| CDE | China | `medic.ingest.china` | Manually-provided scrape at `background/cder_drugs_final_all.csv` (2-col: Chinese `drug_name` + `approval_date`; Chinese→English DeepL-translated via the Stage-0 Babelon stage; drug-list-only, no indications) | PRIMARY |
| DailyMed | USA | `medic.ingest.dailymed` | SPL XML via the v2 REST API (`just ingest-dailymed-acquire` → `data/raw/dailymed/`) | INTERMEDIARY |
| EveryCure | — | `medic.ingest.everycure_drugs` | HuggingFace `everycure/drug-list` (HF-only, no local fallback) | curated |
| CURE-ID | — | `medic.ingest.cureid` | NCATS TSV | research |
| PVLens / FAERS | USA | `medic.ingest.{pvlens,faers}` | FDA data | PRIMARY (AE) |

Downloadable source URLs live in `conf/source_urls.yaml` — never hardcoded. **No source falls back to
an old MeDIC v1.0.0 table** (all `*_norm.*` / `matrix_*` fallbacks were removed): a missing primary /
manually-provided source raises a clear, actionable error rather than silently degrading. DailyMed's
SPL-XML path is primary — an empty `data/raw/dailymed/` is a hard error (`just ingest-dailymed-acquire`
first).

### 3.2 Source priority hierarchy (normative)

1. **Primary regulatory approvals** (Orange/Purple Book, EMA EPAR, PMDA approvals) — authoritative
   record *that a drug is approved*.
2. **Regulatory indication documents** (DailyMed SPLs, EMA SmPCs, PMDA package inserts) —
   authoritative record *that a drug is approved for a specific indication*.
3. **Research evidence** (ClinicalTrials.gov, deep research, PubMed) — supporting evidence for
   investigational or off-label use; complements, never replaces, regulatory data.

US approvals (Orange/Purple Book) outrank DailyMed, which is a secondary republisher of FDA SPLs.
DailyMed covers only ~34% of Orange Book drugs, so it is **not** a complete FDA indication source;
coverage gaps must be filled by other means, not papered over.

---

## 4. Invariants (MUST hold)

These are hard rules. A change that breaks one is a bug, not a tradeoff.

- **I-1 Source isolation.** Each ingester emits evidence **only** for the jurisdiction it itself
  originates (DailyMed→USA, EMA→EU, PMDA→JAPAN, Orange/Purple Book→USA, GRLS→RUSSIA, CDSCO→INDIA,
  CDE→CHINA). Cross-jurisdiction *merging* is allowed **only** in `src/medic/merge/on_label_merge.py`.
  Cross-jurisdiction *emission at ingest is forbidden*, even when a raw file carries cross-jurisdiction
  flag columns — strip them, never synthesise rows. See `docs/source-isolation.md`.

  **Stated once, in `src/medic/source_isolation.py`, and enforced in two places**: over the
  products by `build_qc.check_source_isolation`, and at the export boundary by
  `export.kgx.validate`. Neither is redundant — the first catches a breach that never reaches
  the export, the second is the last check before a consumer sees an edge. The schema cannot
  catch this on its own: a DailyMed row relabelled `jurisdiction: EU` holds a legal
  `JurisdictionEnum` value, so only a source-vs-jurisdiction cross-check sees it. **An
  unrecognised source is a violation, not a skip** — the KGX gate defaulted unknown sources to
  "nothing to check" and thereby exempted all 132 India edges, because its private copy of the
  table keyed the authority (`CDSCO`) while the exporter writes the ingester (`INDIA`).
- **I-2 Deterministic two-stage grounding.** Stage-1 grounding resolves a source string to an ID in
  the **highest-priority cascade vocabulary** that yields a deterministic single match (diseases
  MONDO>HP>UMLS, drugs CHEBI>DRON) — it is **not** forced to canonical Mondo/ChEBI at this stage.
  **Canonicalization to Mondo (disease) / ChEBI (drug) is the separate Stage-2 normalization**, and
  uses only mappings the target namespace itself publishes. The same input always yields the same
  result (offline, byte-identical reruns). See §6 and `specs/2026-07-22-deterministic-grounding-design.md`.
- **I-3 No Mondo downfilling.** Indications are never propagated *down* the disease hierarchy.
  Annotations travel up in ontology reasoning; a broad approval does not imply every subtype.
  Hierarchical inference is a query-time concern for consumers, not baked into the data.
- **I-4 Every grounding/normalization decision is persisted.** Not just failures — **every** decision
  (resolved, ambiguous, or unmatched) is written as an SSSOM row in the `mappings/` decision stores
  (unresolved as `predicate_id: sssom:NoTermFound`), so the stores are a complete, diffable,
  hand-editable audit of every string→ID and ID→ID decision. Nothing is silently dropped.
- **I-5 No terms-of-service-violating scraping.** The WHO Collaborating Centre ATC scrape is
  permanently removed; higher ATC coverage must come from a licensed WHO ATC/DDD download, not scraping.
- **I-6 CURIE handling.** All CURIE parsing goes through `src/medic/curie_utils.py` (bioregistry-backed
  `curies` converter). Never `str.split(":")`.
- **I-7 Source-string faithfulness.** The verbatim source string is preserved on the record as
  `original_string` and is **never mutated** by any resolution stage. All string transforms applied to
  reach a match (normalization, surgery, salt strip, translation, fuzzy) live only in the grounding
  SSSOM row's `subject_preprocessing`, never overwriting the original.
- **I-8 Full transformation traceability.** Every step on the path from the verbatim source string to
  the final canonical id — **translation, string preprocessing, grounding, and normalization** — MUST
  be captured as a **named step recording both its incoming and outgoing value**. Every transformation
  *action* is a value of a controlled enum (`PreprocessingRuleEnum` for string transforms — including
  `deepl_translation` for the Stage-0 translation; the `GroundingQualityEnum` / `NormalizationQualityEnum`
  for stage outcomes) — no transform may be applied anonymously or in-place. **Adding a new manipulation
  means adding an enum value first** (mirrored in `RULE_CERTAINTY`/`RULE_PREDICATE`, guarded by a test),
  then emitting the in→out pair into provenance: the `Translation` object + Babelon row (Stage-0), the
  SSSOM `subject_preprocessing` + transformed `match_string` (Stage-1), and the `Grounding` /
  `Normalization` objects. The chain must be replayable from the record alone.
- **I-9 Mention identity (MEDICNE).** Every extracted source mention is assigned a stable
  `MEDICNE:<uuid5>` id **at extraction time** — the single identifier of the original source literal,
  carried on the record from the very start. `medic.mention.assign_mention(record, entity_type)` stamps
  two uniform fields on every source record: `original_literal` (the verbatim source string, I-7
  faithful) and `mention_id` (a deterministic `uuid5` of `(entity_type, base-normalized literal)` —
  offline, per-surface-form, byte-identical; `mint_mention_id`). The translation (Babelon `subject_id`),
  grounding (SSSOM `subject_id` column + the `Grounding.subject_id` object slot) and downstream product
  records are all anchored on that id, so the full transformation trail (verbatim string → English
  translation → grounded id → canonical id) is join-able for the user-facing UI. Minting only *reads*
  the source string; it never mutates it (I-7).

- **I-10 Assertion source consistency.** A `SourceAssertion` is internally single-source: its
  drug mention, its disease mention and every one of its spans come from the *same* source
  document (`drug.mention_source == disease.mention_source == source`, and every
  `span.document == assertion.document`). Cross-jurisdiction *merging* happens one level up, on
  the pair. Enforced by `provenance_build.validate_source_assertion`.
- **I-15 Approval-date authority.** An `approval_date` on a `RegulatoryStatus` must be a date the
  authority naming that row issued. A missing date is filled only from that drug's approvals **for
  the same authority**, never from the earliest across all of them — `min()` over every authority put
  warfarin's Russian registration date (`20061229`) on its FDA/DailyMed row, and the same smear onto
  2,194 edges at reliability HIGH. Neither I-1 gate can see this: both compare source against
  jurisdiction and never read the row's content, so an FDA row carrying a Russian date is well-formed
  to both. Enforced by `build_qc.check_approval_date_authority`, which flags a date another authority
  demonstrably issued (a date no authority has on record is the normal case — an indication document
  carries its own).
- **I-11 Confidence completeness.** Every transformation step declares both `confidence` and
  `confidence_basis` (`MEASURED` / `DETERMINISTIC` / `PRIOR`), and every assertion carries all
  four components of a `ConfidenceBreakdown` whose `overall` is the product of the other three.
  An unmeasured step takes a calibrated prior from `conf/confidence_priors.yaml` rather than
  contributing nothing. **Every confidence in MeDIC is a data-quality number — how sure we are
  the linking is right — never evidence strength about the claim**
  (`docs/sepio-sieve-alignment.md` §3).
- **I-12 Terminal normalization.** A resolution chain ending in a CURIE ends with a
  `NORMALIZATION` step, identity or not, so a reader can tell "no normalization was needed" from
  "none was recorded".
- **I-14 Canonical labels, and no restricted term text.** Two rules about the strings MeDIC
  publishes as entity names:
  1. **The label follows the id.** Once a resolution chain lands on a canonical id, the
     canonical vocabulary's label is what names it — Stage-2 normalization rewrites the label
     as well as the id. It previously rewrote only the id, so 961 of 5,544 MONDO-resolved
     pairs shipped a label from whatever vocabulary the grounder passed through (`MYCOSES`,
     `Ulcer of esophagus NOS`, `Crisis addisonian` on `MONDO:0019801`).
  2. **A licence-restricted vocabulary may be matched against, but never published.** MedDRA
     arrives inside the UMLS Metathesaurus rather than by separate download, so it entered the
     disease index without a decision (`grounding/lexical/loaders/umls.py: DEFAULT_DISEASE_SAB`).
     Matching is internal lookup and stays; emitting one of its strings as `object_label`
     publishes it into the SSSOM stores and every downstream product, which is redistribution.
     `LABEL_SAB_DECISIONS` is an **allowlist**: it names every vocabulary that may supply a
     published label, each with the decision permitting it, and `LABEL_SAB_PREFERENCE` picks
     between them. A vocabulary absent from the map fails closed. A concept known *only* to
     such vocabularies ships **unnamed** — an empty label is honest; the id still resolves and
     the mapping still works. The index build logs the count rather than letting it be
     discovered later.

     This was a blocklist (`RESTRICTED_LABEL_SAB_PREFIXES = ("MDR",)`) until 2026-08-16, and
     the direction was the defect: a blocklist enumerates what is forbidden, so anything nobody
     thought of publishes silently. **WHO ICD-10 was publishing verbatim rubrics** as KGX node
     names on that basis (UMLS Appendix 1 Category 3 — publication expressly excluded), while
     `SNOMEDCT_US` and `ICD10CM` carried unexamined scope caveats. Reversing to an allowlist
     makes adding a vocabulary a licensing decision that has to be written down; a test asserts
     every preferred vocabulary carries one. See LICENSING.md for the per-vocabulary record.

     **SNOMED CT is not restricted** (decided 2026-08-15 on the Global Patient Set). One caveat
     is recorded rather than resolved: the GPS is a *subset* of SNOMED CT while the index
     allowlists the whole `SNOMEDCT_US`, so the GPS licence covers some published SNOMED labels
     and not necessarily all. See `LICENSING.md`.

  **The policy applies to every store that carries a label, and is checked over the artefacts.**
  Scoping it to `disease_grounding.sssom.tsv` alone is how it shipped broken: the grounding store
  correctly blanked 240 MedDRA-only concepts, `disease_normalization.sssom.tsv` was never
  regenerated and kept labels for 238 of them, the merge read the label from there, and 104 MedDRA
  strings reached `exports/medic_nodes.jsonl` with all 758 tests green — one of which
  (`test_a_non_mondo_record_is_left_alone`) asserted the leaking behaviour.
  `scripts/refresh_grounding_labels.py` now covers all four stores;
  `GroundingStoreView.label_for()` returns `""` for "ships unnamed" and `None` for "unknown", and
  `_canonical_disease_label` honours the difference rather than falling back to the record's own
  label; and `tests/test_restricted_labels_not_published.py` asserts the invariant against
  `mappings/`, the products and the KGX nodes rather than against a function.

  This is a licensing boundary, not a style rule: adding a vocabulary to the index means
  deciding whether its term text may be published, and recording that in `LICENSING.md`.
  The grounder's own `output_label` on a GroundingStep is exempt from rule 1 — it records what
  the grounder actually produced (I-7/I-8), so a retired label is corrected upstream, in what
  the index emits, never by rewriting the trail. The `mappings/` store is authoritative for the
  *decision* and the label is derived from it, so `scripts/refresh_grounding_labels.py`
  propagates a label-policy change without re-grounding, and the merge refreshes a record's
  stale `grounded_label` from the store rather than trusting whenever its ingest last ran.
- **I-13 Pair aggregation.** A pair's `confidence.overall` is the noisy-OR
  (`1 - Π(1 - cᵢ)`) over its **distinct sources**, each taken at its best-resolved
  assertion; `n_assertions` matches the list length and `n_sources` the number of terms fed
  to the aggregate. Corroboration raises confidence — the opposite direction from a chain,
  where each step is another chance to have linked the wrong entity. **Grouping by source is
  part of the invariant, not an optimisation:** noisy-OR assumes independent failures, and
  two documents from one regulator carry the same sentence read by the same extractor
  against the same index. Aggregating flat over assertions drove
  `hydrochlorothiazide → hypertension` to exactly 1.0 off 24 copies of one DailyMed label.

## 5. Data requirements (per record)

**Every approved indication MUST carry:**
1. Approval date from the regulatory authority, when available from any source.
2. A **direct, verifiable regulatory document URL** — the official document that establishes the
   approved indication, not a search URL.
3. The regulatory authority (FDA, EMA, PMDA, …).
4. Source role: `PRIMARY` vs `INTERMEDIARY` (`DataSourceRoleEnum`).

**Every drug approval MUST carry:**
1. Earliest known approval date per jurisdiction.
2. Application/registration number where available (NDA/ANDA, BLA, EPAR number, …).

**Regulatory document URL policy** (see `docs/architecture.md` §5.6 for the full per-source table):
emit a direct `source_document_url` only where it is deterministic from source metadata (DailyMed
setid, EMA EPAR slug, PMDA review-report PDF). For Drugs@FDA and Purple Book, emit the detail-page
URL only — FDA publishes no deterministic per-NDA/BLA PDF URL, and scraping for one is not worth the
maintenance cost.

**Evidence model** (`evidence.yaml`, shared by indications/AEs/research): each `EvidenceItem` records
`source_type`, `jurisdiction`, `reference` (+ title), `snippet` (exact, verifiable quote),
`support`, `confidence`, `evidence_source`, `approval_status`, `max_research_phase`, and an inlined
`curator` `Agent` (id, type, name) for provenance.

---

## 6. Grounding contract

Entity resolution is a **two-stage, deterministic, offline** pipeline (full detail in
[`specs/2026-07-22-deterministic-grounding-design.md`](specs/2026-07-22-deterministic-grounding-design.md)),
preceded by an optional Stage-0 translation for non-English sources. It replaces the previous
non-deterministic OAK→Gilda→NameRes→OLS + 0.80-threshold + LLM-rerank cascade.

0. **Stage 0 — translation (non-English string → English)** (`src/medic/translation/`). For non-English
   sources (China `zh`, Russia `ru`), each unique source mention is minted a `MEDICNE` id (I-9) and
   translated to English with **DeepL** through the `babelon` translator service. Every translation is a
   row in the Babelon store `mappings/drug_translation.babelon.tsv` (keyed by the `MEDICNE` id, stamped
   `translator: wikidata:Q116709136`, `translator_expertise: ALGORITHM`), attached to the record as a
   `translation` object, and the English `translation_value` **replaces** the string the grounder sees.
   The git-tracked Babelon table is the authoritative, deterministic cache (a filled row is never
   re-translated); `MEDIC_SKIP_EXPENSIVE_CALLS` leaves mentions untranslated (they will not ground).
   Names DeepL cannot translate keep their original value, so Russia's Cyrillic-transliteration ladder
   still catches those in Stage 1.
1. **Stage 1 — grounding (string → initial ID)** (`src/medic/grounding/`). A custom lexical matcher
   queries an on-disk SQLite index built from the source vocabularies (diseases MONDO+HP+UMLS, drugs
   CHEBI+DRON; ICD10CM and PR built then dropped as reversible config). A tiered cascade — exact →
   normalized → minor surgery → entity-scoped rules (drugs: related-synonym, INN spelling, salt strip;
   diseases: qualifier strip) → translation dictionary → **fuzzy edit-1 (unique-hit guard)** —
   yields the first single-ID hit; ambiguity is never auto-resolved. Combinations are split and each
   component grounded. The result lands in the highest-priority cascade vocab (not forced to
   MONDO/CHEBI — see I-2).
2. **Stage 2 — normalization (initial ID → canonical ID)** (`src/medic/normalization/`) maps the
   grounded ID to the canonical target (MONDO for disease, CHEBI for drug) using **only** cross-refs
   the target namespace itself publishes (`skos:exactMatch`, obsolete `replaced_by`), never synthesised.

Every decision — resolved, ambiguous, or unmatched — is persisted as a row in the SSSOM decision
stores under `mappings/` (see §2, I-4); those files are the hand-editable authoritative cache and
manual edits survive regeneration. No network call at resolve time; reruns are byte-identical.

**LLM / fuzzy scope.** Fuzzy edit-1 matching is implemented and deterministic, but its rows are
written as curator-reviewable proposals (`skos:closeMatch`, tagged `fuzzy_edit1_unique`), not silently
trusted. LLM translation of genuinely foreign names is **deferred** (`translation_llm`, scaffolded but
disabled). Measured grounding recall: diseases ~80.6%, drugs ~82.3%.

---

## 7. Quality and validation

Three-layer validation stack, all invoked from the justfile; `build-all` runs them:

1. **Schema** (`just validate-schema`, `linkml-validate` against `medic.yaml`) — structure, enums, ranges.
2. **Terms** (`just validate-terms`, `linkml-term-validator` with `conf/oak_config.yaml`) — CURIEs
   exist and labels match canonical.
3. **References** (`just validate-references`, `linkml-reference-validator`) — evidence snippets
   actually appear in the cited source (anti-hallucination). Snippet-level checking is verified at
   extraction time by `curate_snippets.py`; the upstream feature request that would move it into the
   validator is drafted but not yet filed (§11).

CI runs a subset on every push: `.github/workflows/ci.yml` runs ruff and the test suite, checks the
manual-source archive against its fingerprints, builds the QC report (`scripts/build_qc.py`), and
schema-validates changed `kb/` files.

Reproducibility and cost are first-class: all expensive operations cache to `cache/`;
`MEDIC_SKIP_EXPENSIVE_CALLS=1` bypasses every LLM/rate-limited call for fast iteration (with degraded
grounding on non-English sources). A full run is ~$5–10 (vs ~$50–100 in v1.0.0).

---

## 8. Status snapshot

The pipeline is being rebuilt for the first time on the new deterministic grounder; the first full
fill build regenerates every product. Known counts from that run (`redesign` branch):

| Product | Count |
|---|---|
| Drug list | 4,250 drugs (lexical-grounded; more unresolved than v1 as non-English/formulation strings now fail loudly rather than via LLM/NameRes) |
| Disease list | 23,224 |
| Sources ingested | Orange Book 2,725 (2,606 grounded) · Purple Book 642 · EMA 995 + 3,185 indications · PMDA 1,174 + 1,966 indications · Russia 5,885 (992 grounded, Cyrillic) · India 112 (0 grounded — formulation noise) · DailyMed 1,819 SPLs |
| Indications / Contraindications | 7,483 indication pairs / 11,360 assertions · 2,659 contraindication pairs / 3,305 assertions |
| Research associations | **9** (was 164; see below) |

**The research axis was cut to 9 associations on 2026-08-15** (#44). 155 of the 164 evidence rows
cited a PubMed record that did not support the claim — real PMIDs attached to unrelated papers
(`everolimus → tuberous sclerosis` cited PMID:20047325, "Blueberry supplementation improves memory in
older adults"). Only 14 of 155 cited records mentioned both the drug and the disease. What survives
is nine hand-verified landmark trials whose snippets are verbatim from the cited abstract. The
`validate-references` layer §7 describes as running never ran against `products/research_list.yaml`;
wiring it in is the precondition for growing the axis back.

**Grounding recall (deterministic, offline):** diseases ~80.6%, drugs ~82.3%. Russia Cyrillic 17% via
transliteration + fuzzy (0 without it). Every decision (incl. `sssom:NoTermFound`) is recorded in the
`mappings/*.sssom.tsv` stores; the structured `grounding`/`normalization` objects are funneled through
the merge onto the released `products/*` records.

**Done this cycle (on top of v1.0.0 → redesign):** replaced the non-deterministic NameRes/LLM grounding
cascade with the two-stage deterministic lexical grounder + SSSOM decision stores (wired into every
source); `PreprocessingRuleEnum` catalogue (surgery, salt/ester, combination split, qualifier strip,
INN spelling, Cyrillic transliteration, fuzzy edit-1, translation) with `certainty` annotations driving
per-match confidence; **Russia rebuilt** off the manual `background/grls.zip` (was v1 `russia_norm.csv`);
**DailyMed migrated** to real SPL-XML via the v2 API (was v1 Excel matrix); **all old-MeDIC-table
fallbacks removed** (fail-loud); grounding metadata funneled through to products (schema + datamodel +
merge). Prior redesign work retained: self-contained ETL, source isolation, schema source-separation,
KGX + SSSOM exports, PMDA/India primary-source PDF migrations, deep-linked regulatory document URLs.

---

## 9. Design decisions

### Finalised

- **Schema source separation.** `RegulatoryAuthorityEnum` + `DataSourceRoleEnum` + `RegulatoryStatus`
  class (`authority.yaml`) replace the raw `fda`/`ema`/`pmda` booleans on `IndicationAssociation`.
  Added additively; legacy booleans retained for backwards compatibility.
- **Disease ID normalization at merge.** Non-Mondo IDs are normalized inside the merge step
  (`on_label_merge.py`), not a separate script.
- **DailyMed SPL-XML is primary; no legacy fallback.** DailyMed acquires real SPL XML via the v2 API
  (`ingest.dailymed.acquire`, driven by the USA drugs in `products/drug_list.yaml`) and mines the
  Indications (LOINC 34067-9) / Contraindications (34070-3) sections. The v1.0 Excel matrix fallback
  was removed — an empty `data/raw/dailymed/` is now a hard error (`just ingest-dailymed-acquire` first).
- **EveryCure and Disease List are HuggingFace-only.** No local-file fallback: `everycure/drug-list`
  and `everycure/disease-list` are the single source of truth; if HuggingFace is unreachable the ingest
  fails loud rather than degrading to a stale local table.
- **No silent legacy fallbacks anywhere.** Purple Book, PMDA, India, DailyMed, and Russia read only
  their fresh primary / manually-provided source; a missing source is a clear hard error, never a
  degrade to a v1.0.0 `*_norm.*` / `matrix_*` table.
- **No Mondo downfilling.** `downfilled_from_mondo` removed from schema and datamodels (see I-3).
- **WHO CC scraping removed.** Replaced by ChEMBL + fallbacks (see I-5).
- **FDA/Purple Book document URLs.** Detail-page only; no per-NDA/BLA PDF scraping (see §5).
- **Deterministic two-stage grounding.** The non-deterministic NameRes/LLM cascade is replaced by an
  offline lexical grounder (Stage 1) + target-namespace normalization (Stage 2), each backed by a
  hand-editable SSSOM decision store under `mappings/`. See §6, I-2/I-4/I-7, and
  `specs/2026-07-22-deterministic-grounding-design.md`.

### Pending user input

- ~~**Russia GRLS migration.**~~ RESOLVED — Russia now reads a manually-provided raw GRLS export at
  `background/grls.zip` (8 register xlsx, Cyrillic), replacing the v1.0.0 `russia_norm.csv`. GRLS remains
  IP-blocked, so the export is provided manually. Cyrillic names ground via a deterministic
  transliteration rule (~17%) + the shared disease-name LLM extraction elsewhere.
- ~~**China CDE indications.**~~ RESOLVED — the manually-provided CDE scrape
  (`background/cder_drugs_final_all.csv`) has only a Chinese `drug_name` and an `approval_date`, with
  **no indication text**. China therefore contributes a **drug list only** (same as Russia); indication
  ingest would require scraping a different CDE/NMPA page and is out of scope for the current source.

---

## 10. Task ledger

Phase-level status of the redesign work. `DONE` = implemented and validating; `PARTIAL` = works but
with a known coverage gap; `NOT STARTED` = specified, not built.

| Phase / task | Status | Notes |
|---|---|---|
| 1.1 Remove downfill field | DONE | `downfilled_from_mondo` gone from schema + datamodels |
| 1.2 Schema source separation | DONE (additive) | `authority.yaml`; `regulatory_status` alongside legacy booleans |
| 2.1 Filter `Error` drug IDs | DONE | `on_label_merge` skips records with `Error` in IDs |
| 2.2 Normalize non-Mondo disease IDs | PARTIAL | Crossref-based; only IDs with xrefs in disease_list (few). Needs OLS-based fallback |
| 3.1 Orange Book NDA numbers | DONE | `application_number` carried through into FDA evidence URLs |
| 3.2 DailyMed legacy Excel fallback | REMOVED | The v1 Excel matrix fallback was deleted (fail-loud); SPL-XML is the only path |
| 3.3 DailyMed SPL-XML path (primary) | DONE | `acquire.py` fetches SPL XML per setid from the v2 API into `data/raw/dailymed/`; ~1,976 SPLs acquired (1,819 with data); no legacy fallback (empty dir = hard error); LLM disease/contra extraction per SPL |
| 4.1 EMA EPAR URLs | DONE | Extracted from EMA spreadsheet; 932 drugs with direct EPAR URLs |
| 4.2 EMA primary indications | DONE | `_build_ema_indication_records` → `kb/indications/ema/` |
| 5. PMDA primary indications | DONE | `_build_pmda_indication_records` → `kb/indications/pmda/` |
| 6. EMA/PMDA contraindications | NOT STARTED | Source data lacks contraindications; needs SmPC/insert PDF parsing |
| 7a. PMDA primary-source migration | DONE (2026-04-29) | `pmda/{fetch_primary,parse_pdf}.py`; consolidated PDF → ~1,178 ingredients |
| 7b. India primary-source migration | DONE (2026-04-29) | `india/{fetch_primary,parse_pdf}.py`; 39 CDSCO year PDFs via JSP+iframe |
| 7c. Russia migration | DONE | Rebuilt off manual `background/grls.zip` (8 register xlsx, Cyrillic) — replaces v1 `russia_norm.csv`; 5,885 drugs; fail-loud if the zip is missing |
| 7d. China migration | DONE | Rebuilt off manual `background/cder_drugs_final_all.csv` (2-col scrape); Chinese `drug_name` → English INN via cached LLM translation (`cache/enrichment/china_translation.json`), then shared grounding → ChEBI; fail-loud if the CSV is missing. Drug-list-only (no indication text in the scrape) |
| 8. Research integration | NOT STARTED | ClinicalTrials.gov + systematic PubMed beyond deep research |
| 9. Verification | DONE | Full rebuild + cross-source overlap report; all products validate |
| 10. Deterministic grounding rework | DONE | Two-stage lexical grounder + normalization **wired into every source** (default backend `lexical`); `grounding.yaml` imported into `medic.yaml` + datamodel regenerated; `grounding`/`normalization` objects funneled through the merge onto `products/*`; SSSOM stores under `mappings/`; ~270 tests. Recall diseases ~80.6% / drugs ~82.3%. First full fill rebuild **complete** (exit 0, all products validated; drug + indication metadata funneled to `products/*`). See `specs/2026-07-22-...` |
| 11. Remove old-MeDIC-table fallbacks | DONE | Purple Book / PMDA / India / DailyMed / EveryCure / Disease-List legacy fallbacks removed; every source is single-path + fail-loud |
| 12. LLM stack fix | DONE | `litellm` was un-declared/broken → added to `pyproject.toml`; `conf/llm_config.yaml` model → `claude-sonnet-4-5-20250929`; `llm_call` gains `num_retries=4`+`timeout`; DailyMed LLM calls guarded per-SPL |
| 13. Formulation-string grounding | DONE | `formulation_strip` rule (strength/unit, dosage form, release qualifier, pharmacopoeia tag; `conf/grounding_formulation.yaml`), composes with salt/combination/fuzzy in the ladder. India **0% → 78%** (87/112), OB +4, PB +17; guards prevent false strips |
| 14. RxNorm substance resolver | DONE (opt-in) | `enrichment/rxnorm_resolve.py`: RxNav approximate-match → ingredient INN → re-ground → CHEBI/DRON. **172** curator-reviewable proposals (US-centric), written into `mappings/` as *locked* `RXNORM` rows that short-circuit the offline matcher (determinism preserved). `just resolve-drug-residue` |
| 16. Transformation-provenance model | DONE (cutover complete) | Standalone `src/medic/schema/provenance.yaml`: abstract `TransformationStep` + `ExtractionStep`/`TranslationStep`/`GroundingStep`/`NormalizationStep`, wrapped in a **`Resolution` container** (`Mention.resolution {input_value, output_value, confidence = product of step confidences, pipeline}`) with an enforced chaining invariant (`output[i]==input[i+1]`, ending at `resolved_id`); plus `Mention`, `TextSpan`, `ProvenanceAgent`, and the moved grounding enums. **Entity recognition and the claim are separate** (2026-08-08): `ExtractionStep` is NER only (`asserted_relationship` deleted — already named by `relationship_type`; `entailment_score` collapsed into `confidence`), while a new **`Assertion`** on `IndicationAssociation.assertion` holds the claim's quote/confidence/`negation_cue` and the relation-level `AssertionFlag` (`negated_inversion`/`over_extraction`/`wrong_section`/`wrong_pairing`); `ExtractionFlag` keeps only recognition failures. `Drug` carries `identity`/`approvals`/`reliability` + nested `atc` and a `features` list; `IndicationAssociation` carries `drug` DrugRef + inlined `disease` Mention + `assertion` + `reliability`. **All flat identity/approval fields removed**; reads go through `src/medic/product_view.py`. Reliability gates mirror the split (`_recognition_gate` / `_assertion_gate`). Products regenerated, 382 tests pass. **`validate-all` is no longer clean** (2026-08-13): `products/indication_list.yaml` fails with 2,620 `document_id` errors (#41), and the per-source `kb/drugs/*` layer has no working schema contract at all (#40). See `specs/2026-07-28-transformation-provenance-model-design.md` §2.1 (as-built), `review_model.md`, `docs/provenance-walkthrough.md`. **Open:** flat `atc_*`/`is_*` removal (#26), mention-id collisions (#17), w3id registration (#35). The disease-side `applied_rules` funnel is closed. |
| 15. Stage-0 translation (Babelon/DeepL) + MEDICNE mentions | DONE (drugs) | New `src/medic/translation/` translation stage: `MEDICNE:<uuid5>` minted per mention (`src/medic/mention.py`, I-9); DeepL via the `babelon` translator service; every translation a row in `mappings/drug_translation.babelon.tsv` (git-tracked deterministic cache). `Translation` schema class + `translation` record/product slot; `deepl_translation` `PreprocessingRuleEnum` value. **China & Russia rewired** to translate `zh`/`ru` → English before grounding (China's old per-name LLM translator removed); `translation` funneled through `drug_merge` to `products/drug_list.yaml`. Disease-side translation store + the MEDICNE-keyed term↔term grounding-store conversion are follow-ups (§11) |
| 17. Complete KGX export | DONE | `src/medic/export/kgx/` (package; was a 129-line module). **One edge per source assertion** (12,858 edges, was 8,737) so every edge is single-sourced with its own `primary_knowledge_source`, document, quoted span and confidence; pair aggregates repeat on each edge so the collapsed view is a `GROUP BY`. All 23,224 diseases + 4,323 drugs + 1,274 stub nodes for unmapped endpoints (28,821 nodes, was 6,554). Research + adverse-event edges wired. Two layers: Biolink-valid core + `medic_`-namespaced extensions. Fixed three correctness bugs — `biolink:contraindicated_for` does not exist in Biolink 4.x (now `contraindicated_in`), `agent_type: manual_agent` asserted on LLM-extracted claims (now derived from `assertion.method`), and list-valued `primary_knowledge_source`. Verbatim I-7 literals, quoted spans and character offsets ride on *standard* Biolink slots; `MEDICNE:` ids join edges back to the `mappings/` stores so I-8 replayability holds without JSON blobs. Conformance gate (`just validate-kgx`, biolink-model pinned `==4.3.7`) checks categories/predicates/slots, referential closure, single-valued discipline, determinism and I-1 source isolation; 66 tests. `koza` dropped (unused). See `specs/2026-08-13-kgx-export-design.md`. **2026-10-23:** every node and edge is now built through the `biolink-model` pydantic class for its category (strict: undeclared property, bad enum, list-for-scalar and missing required slot all refuse at construction); edges carry their association class as `category`; the source record (document CURIE, landing page, PDF) is a nested `sources[].source_record_urls` `RetrievalSource`; the `medic_*` layer cut from 62 edge / 30 node properties to 22 / 16, build-level agent provenance and the per-source regulatory explanations moved to `medic_kgx_metadata.yaml`. Pin moved `4.3.7` → `4.4.6` the same day: the newer model puts `clinical_approval_status` / `max_research_phase` on the `treats` class and `population_context_qualifier` on every class, so three more slots are constructor-validated and the off-class list is six; the new `subsets` node slot replaces `medic_subsets`. **2026-10-29 (#110):** `disease_context_qualifier` carries a MONDO term and nothing else — the slot's range is `disease`, but UMLS and HP are on that class's `id_prefixes` so the constructor took them and the `str` typing took free text, leaving only 10,314 of 20,342 populated values conformant (3,702 UMLS, 1,618 HP, 4,708 free text over 1,022 strings). The rule takes the first MONDO term among the grounded ids rather than the first id (+481 edges); every grounded id stays on `medic_disease_context_ids` and the label's wording on the new `medic_disease_context`, positionally aligned as the products write them, so nothing is lost; the conformance gate rejects a non-MONDO value. `population_context_qualifier` / `temporal_context_qualifier` have the same range problem and are open. Spec §12 |

**Tests:** `tests/test_regulatory_urls.py` (11, URL detection/upgrade/OB+PB lookup),
`tests/test_pmda_parse.py` (14, incl. legacy-parity gate), `tests/test_india_parse.py` (13, incl.
partial-download gates). All passing.

---

## 11. Open items and roadmap

Outstanding work lives on the tracker — <https://github.com/monarch-initiative/medic/issues>. The
local `issues/` directory is gitignored scratch for unfiled drafts only; every item below carries its
issue number, and anything without one is not yet filed.

Highest-value outstanding work:

- **Contraindications beyond FDA (#25).** EMA/PMDA source data lacks contraindications; needs EPAR
  SmPC / Japanese package-insert PDF parsing.
- **Non-Mondo disease IDs (#13).** Now partly handled by Stage-2 normalization, which maps grounded
  UMLS/HP IDs to Mondo via Mondo's own asserted `exactMatch`/`replaced_by` (§6). Remaining residue: IDs
  Mondo does not cross-reference. This is also the limiting gap for **cross-source overlap**
  (FDA↔EMA↔PMDA pair matching); the merge step should consume the `mappings/` normalization store.
- **Grounding coverage — India & US ingredient tail now handled; Russia & biologics remain.** The
  deterministic `formulation_strip` rule took **India 0% → 78%** and recovered US formulation strings;
  the **RxNorm resolver** adds 172 curator-reviewable proposals for the US ingredient tail (ledger
  13-14). Remaining: (a) **Russia 17%** — the Cyrillic path needs a fuller scheme or a Russian→INN
  dictionary; (b) **EMA/PMDA biologic/vaccine free-text** + the Russian residue that no vocab reaches
  (Phase 5 — accept unresolved or hand-curate via the SSSOM stores); (c) **fuzzy isotope false
  positives** (`13C`→`14C`) — drop digit-substitution edits. Filed as **#19** (Russia), **#18** (fuzzy
  isotope), **#20** (the SSSOM curation surface has zero manual rows against 12,252 unresolved
  literals) and **#33** (ChEMBL as a supplementary backbone for the tail).
- **Full I-8 traceability (schema-modelled; flat-field cutover complete).** The transformation-provenance
  model (`src/medic/schema/provenance.yaml`, spec `2026-07-28`) captures every step's in→out as a
  first-class `TransformationStep` on a `Mention.steps` trail, with every transform action + failure-mode
  `flag` a controlled enum. `Drug.mention` and the inlined `IndicationAssociation.disease` Mention
  carry the replayable trail; reliability is computed from it. **The flat fields have been removed** (task
  ledger #16) — the products carry the new shape only, and `src/medic/product_view.py` is the single
  read-side accessor every consumer goes through. The two step-trail completeness follow-ups are now
  closed: drug Mentions open with a `STRUCTURED_FIELD` ExtractionStep, and the GroundingStep carries
  per-Stage-1 `applied_rules` funneled from the git-tracked SSSOM grounding store at merge. Residual
  work is filed: **#26** (drop the flat `atc_*`/`is_*` fields), **#17** (MEDICNE mention ids collide
  across substances differing only inside brackets), **#22** (snippet entailment is lexical, demoting
  206 correct synonym extractions to LOW), **#24** (clinical qualifiers are dropped, so distinct claims
  collapse at merge) and **#35** (register the w3id redirect before publishing the schema).
  The verbatim-substring gap is **closed** (#64, landed in `7a07efd` / `6f808d8`). The extraction
  prompts ask the LLM to *canonicalise* disease names, so the first recorded step's "verbatim source
  string" used to be a value the document never wrote — 1,414 of 11,696 shipped INDICATIONs (12%)
  carried a disease label not locatable in its own source text. The extractors now return
  `Extracted(name, verbatim)`, the verbatim is stored on the evidence row as
  `original_disease_verbatim`, and re-extracting all four sources under the new contract takes the
  unlocatable rate to **347 of 13,342 (2.6%)**, with 2,213 rows locatable only via the quote. The LLM
  caches moved to a `_v2` namespace; the v1 files are retained so a rollback costs no re-extraction.
  **Still open on this axis:** the verbatim→canonical-name canonicalisation is not yet recorded as an
  I-8 `ExtractionStep` in→out pair, so the transformation happens but is not replayable from the
  record; and `char_start`/`char_end` are still computed by literal `find` in
  `provenance_build._extraction_step`, which should now anchor on the verbatim.
- **Translation stage follow-ups.** (a) The disease-side Babelon store
  (`mappings/disease_translation.babelon.tsv`) is not yet wired — no non-English disease sources exist
  today, but the plumbing (`DISEASE_TRANSLATION_STORE`, `entity_type="diseases"`) is ready. (b) The
  MEDICNE mention id currently anchors Stage-0; converting the Stage-1 grounding store from the SSSOM
  *literal* profile to a MEDICNE-keyed *term↔term* SSSOM (so grounding is a standard entity↔entity table
  like normalization) is a clean, mechanical follow-up now that mentions carry ids. (c) Re-run Russia to
  measure the DeepL uplift over the 17% Cyrillic-transliteration baseline.
- **Drug normalization index (#27).** `cache/normalization/drugs.db` is not built, so drug Stage-2 is
  identity-only (benign — drugs ground natively to ChEBI). Build it if CHEBI obsolete-replacement
  normalization is wanted.
- **Research integration (#32).** ClinicalTrials.gov ingest + systematic PubMed citations beyond deep
  research. **#38** covers the existing backlog: 0 of 35 `kb/research/` files carry `page_or_section`.
- **The research axis has no strength signal, and no real-world-use edges at all.** **#42**: not one
  evidence row in `products/research_list.yaml` sets `max_research_phase`, so all 164 research edges in
  `exports/medic_edges.jsonl` collapse onto `biolink:studied_to_treat` with zero
  `biolink:in_clinical_trials_for`. **#43**: the CURE-ID ingest has never been run against the current
  tree (`kb/research/cureid_associations.yaml` absent), and it is the only source of
  `max_research_phase: CASE_REPORT` — so the export carries zero `biolink:applied_to_treat` edges.
  Complementary: #43 adds edges, #42 reclassifies the existing ones.
- **Adverse events (#31).** PVLens/FAERS ingest is a stub; needs MedDRA→HP/Mondo mapping for the
  disease-centric view, and the MedDRA licence question decides whether any adverse-event product can
  ship at all.
- **Coverage and provenance audits.** **#14** (133 indicated drugs missing from the drug list), **#16**
  (only 22% of Orange Book drugs have DailyMed indication data), **#23** (233 drugs EveryCure calls
  FDA-approved have no MeDIC approval), **#15** (confidence priors ship uncalibrated), **#21**
  (extraction is the only stage with no hand-editable correction store), **#34** (harvest Open Targets'
  curated disease mappings to seed the SSSOM stores).
- **Regulatory document URLs.** **#30** — constructed URLs are never resolved and Orange Book links use
  only the first application number. **#39** — Russia has no per-product GRLS URL at all; the
  `background/grls.zip` rebuild put registration numbers on the records, but a registration number is
  not a `routingGuid`.
- **Release plumbing.** #37 is **done** — the workflow is repointed off the deleted `medi/` tree and
  the `conf/release_assets.yaml` manifest plus its staleness gate now decide what may ship. What
  remains is **#55**: `medic_version` is read from the installed distribution rather than the tag, so
  a tag-then-build without an intervening `uv sync` stamps the previous release into the artefacts.
  `just gh-release` now refuses when the stamp and the tag disagree. **The next release is `v2.0.0`**
  (decided 2026-08-16) — `v1.0.0`/`v1.0.1` are already published, so any `v0.x` would sort below the
  release it replaces, and a major bump is what a change of every product's schema warrants.
- **Ingest observability.** **#28** (count per-source ingest drops and extraction parse failures) and
  **#29** (row-count floors and source stamps are wired into only two of eight ingesters).
- **Schema contract drift.** **#41 is done** — all four products now validate clean against
  `medic.yaml` (verified 2026-08-15). **#40**: `DrugSource`
  declares 19 attributes against 56 distinct keys the eight ingesters actually emit — including the
  whole `grounding`/`normalization`/`translation` provenance block, which `drug_source.yaml` does not
  even import — so no `kb/drugs/*` file validates, and `validate-schema` now refuses that layer rather
  than failing on it. Both are §7 invariant breaches: the schema is supposed to describe what we emit.
- **v0.2 release review (2026-08-15), #44–#55.** Three independent reviews of PR #36 found five
  release blockers. Four are fixed: fabricated research citations (#44, axis cut to 9 verified rows),
  MedDRA term text still published via the un-regenerated normalization store (I-14 above),
  combination components exported as `skos:exactMatch` (`butalbital = paracetamol`), and the
  approval-date smear (I-15 above). The fifth is **not fixed and blocks the release**: `background/`
  and `sources/manual-sources.zip` — third-party conference decks, a paper PDF, a verbatim internal
  Slack transcript, and the GRLS/CDE archives — are reachable in this public repo's branch history at
  `6aa72b1` and `1f5346a`. Untracking them fixed the tree, not the history; squash-merge keeps them
  out of `main` but only GitHub Support can unpublish `refs/pull/36/head`. Filed from the same
  review: **#45** (`alternate_ids` published as `exactMatch`, incl. `ofatumumab` → dimethyl ether),
  **#46** (CI cannot catch a product regression; the invariant guard only warns), **#47** (the
  extraction/assertion reliability gates have no emit site), **#48** (three `mappings/` stores carry
  no licence), **#49** (UMLS is an undeclared build input), **#50** (the KGX export is not valid
  Biolink and `validate.py` cannot see it), **#51** (verbatim excerpt volume far exceeds the declared
  500-char cap), **#52** (`kb/drugs/{russia,china}` publish the full registers), **#53**
  (drug-interaction clauses extracted as contraindications), **#54** (`kb/adverse_events/` is not
  gitignored), **#55** (version stamping).
- **Snippet curation.** Stays in `scripts/curate_snippets.py`: it curates and verifies at extraction
  time, and we are not pushing the feature upstream into `linkml-reference-validator`.

### Requests filed before the redesign (#1–#12)

These predate the rebuild and were not written against the current architecture. Status as of
2026-08-13:

| Issue | Ask | Status |
|---|---|---|
| #1 | IVIG missing, and `ivig → dermatomyositis` absent | Open. A grounding/coverage instance — same class as #14 and #23; worth re-checking against the current build before treating it as a separate defect. |
| #2 | Per-edge source file pointers + raw document text alongside the LLM interpretation | **Largely delivered** by the transformation-provenance model (ledger 16): every claim carries a `Mention` step trail with `TextSpan`s, and `Assertion` holds the quote. Re-read against the current products before closing. |
| #3 | FDA approval dates on drug records | **Delivered** — `approval_date` is on the drug records (6,742 occurrences in `products/drug_list.yaml`). |
| #4 | RxNorm identifier mappings | **Partly delivered** — the RxNorm resolver (ledger 14) is opt-in and writes locked `RXNORM` rows into `mappings/`; RxNorm ids are not carried as first-class mappings on every drug. |
| #5 | Evaluate DrugOn for drug ontology alignment | Not started. |
| #6 | Japanese reimbursements | Not started. PMDA gives approvals only. |
| #7 | Chinese drug approvals | **Delivered** — ledger 7d; China is a drug list only (no indication text in the CDE scrape, §9). |
| #8 | Guidelines approvals | Not started. `GUIDELINE` exists as a `source_type` enum value; no ingester. |
| #9 | What `matrix_indication_list_hyperrelational.xlsx` is | **Answered.** `specs/2026-08-13-hyperrelational-context-integration-design.md` records the pipeline (a second Gemini pass emitting closed-vocabulary clinical context) — proposed, not implemented. Overlaps #24. |
| #10 | EveryCure indications list on HuggingFace | Not started. Only `everycure/drug-list` and `everycure/disease-list` are ingested. |
| #11 | NCC guidelines ingest, and what to do about missing Mondo diseases | Not started; blocked on the same question as #8, and the disease residue is #13. |
| #12 | Import the Matrix curated indications list | Not started. Related to #9. |

---

## 12. Running the pipeline (quick reference)

```bash
just setup                       # install deps (uv)
just build-drug-list [backend]   # ingest all drug sources + merge + enrich
just build-disease-list
just build-on-label-list         # DailyMed + EMA + PMDA -> indications + contraindications
just build-research              # compile kb/research/ into the product
just build-all                   # everything + exports + validation
just validate-all                # schema validation across products
just gh-release vX.Y.Z           # package products/ + exports/ into a draft GitHub release
```

Grounding backend argument: `lexical` (default — deterministic two-stage grounder, §6); legacy
`oak`/`ols`/`gilda` remain present but non-default, and `nameres` is removed. Build the offline index
with `just build-grounding-index` / `just build-normalization-index`; `just ground-report` for
per-predicate/quality/vocab/unresolved counts. Prereqs: Python 3.11+, `uv`, `just`. Key env vars:
`MEDIC_SKIP_EXPENSIVE_CALLS`, `ANTHROPIC_API_KEY`, `MEDIC_CACHE_DIR`.

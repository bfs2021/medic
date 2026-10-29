# MeDIC Technical Architecture

**Last updated:** 2026-08-19
**Branch:** `medic2` (v2.0 pipeline)

---

## 1. What is MeDIC

MeDIC (Medicines, Diseases, Indications, and Contraindications) is an open-source knowledge base for drug repurposing research (PMID:41385096). It builds highly curated datasets exclusively from government regulatory sources, mapping drugs to diseases via approved indications, contraindications, adverse events, and literature-derived research associations.

The system ingests data from 7+ regulatory agencies worldwide (FDA, EMA, PMDA, CDSCO, GRLS, CDE), grounds all entities to canonical ontology identifiers (ChEBI for drugs, Mondo for diseases, MedDRA/HPO for adverse events), merges records across jurisdictions, and produces machine-readable outputs for downstream analysis. Published at https://medic.renci.org.

All schemas are defined in LinkML (`src/medic/schema/`), with the master schema at `src/medic/schema/medic.yaml` importing sub-schemas for drugs, diseases, indication associations, adverse events, evidence, and research sources.

---

## 2. Products

MeDIC produces 6 core products plus several export formats:

| Product | Description | Schema class | Output path | Sources | Status |
|---------|-------------|-------------|-------------|---------|--------|
| **Drug List** | Unified list of approved drugs with chemical identifiers, ATC classifications, and property tags | `DrugList` (`drug.yaml`) | `products/drug_list.yaml` | FDA Orange Book, FDA Purple Book, EMA, PMDA, GRLS (Russia), CDSCO (India), CDE (China), EveryCure | Implemented |
| **Disease List** | Curated disease list with filter flags for rare diseases, hereditary conditions, cancer, etc. | `DiseaseList` (`disease.yaml`) | `products/disease_list.yaml` | Mondo Disease Ontology (via HuggingFace `everycure/disease-list`) | Implemented |
| **Indications List** | Approved drug-disease associations (on-label indications) | `IndicationList` (`indication.yaml`) | `products/indication_list.yaml` | FDA DailyMed, EMA, PMDA, CDSCO (India) | Implemented |
| **Contraindications List** | Drug-disease contraindications | `IndicationList` (`indication.yaml`) | `products/contraindication_list.yaml` | FDA DailyMed | Implemented |
| ~~Adverse Event List~~ | Drug-adverse event associations | `AdverseEventList` (`adverse_event.yaml`) | — | PVLens, FAERS | **Deferred, not a v2.0.0 product** |
| **Research List** | Literature-derived drug-disease associations with evidence snippets | `ResearchAssociationList` (`research_source.yaml`) | `products/research_list.yaml` | PubMed, CURE-ID | Implemented |

### Export formats

| Export | Description | Output path | Status |
|--------|-------------|-------------|--------|
| **Legacy CSV** | `drug_list_flexible.csv`, `drug_list_stringent.csv`, per-source XLSX/CSV files matching v1.0.0 format | `exports/` | Implemented |
| **KGX** | Biolink-compliant knowledge graph nodes and edges (JSONL) | `exports/medic_nodes.jsonl`, `exports/medic_edges.jsonl` | Implemented |
| **SSSOM** | Drug identifier cross-reference mappings in SSSOM format | `exports/medic_drug_mappings.sssom.tsv` | Implemented |
| **Reliability** | Products flattened to statement rows annotated with the reliability tier, plus the reliable subset | `exports/medic_statements.tsv`, `exports/medic_reliable.tsv` | Implemented |

Drug nodes in the KGX export carry the FDA **UNII** as an `xref` wherever an indication row
supplied one. ChEBI and DRON are fetched unpinned from PURLs and so mint identifiers newer than
the SRI Node Normalizer's snapshot; 717 drug ids carrying 3,448 assertions (28%) were unknown
to it, and they are the biologics. A UNII is stable, so a consumer can join whatever the
primary id turns out to be, which also makes the problem self-healing across ontology
refreshes. The identifier is read from the SPL's own `<activeMoiety><code>`, so it is
source-asserted rather than derived.

---

## 3. Pipeline Architecture

The full MeDIC pipeline builds all products from raw regulatory sources, merges them, enriches the results, and exports in multiple formats.

```
                                                              ┌──────────────────────┐
                    ┌── FDA Orange Book ──────────────────┐    │                      │
                    ├── FDA Purple Book ──────────────────┤    │   products/          │
                    ├── EMA ─────────────────────────────┤    │   drug_list.yaml     │
                    ├── PMDA ────────────────────────────┤────►    (DrugList)         │
                    ├── GRLS (Russia) ───────────────────┤    │                      │
                    ├── CDSCO (India) ───────────────────┤    │                      │
                    ├── CDE (China) ─────────────────────┤    │                      │
                    ├── EveryCure ───────────────────────┘    └──────────────────────┘
                    │
                    │                                         ┌──────────────────────┐
                    ├── EveryCure/disease-list (HuggingFace) ──►  kb/diseases/         │
                    │                                         │  disease_list.yaml   │
                    │                                         │  (DiseaseList)       │
                    │                                         └──────────────────────┘
                    │
Raw Sources ────────┤                                         ┌──────────────────────┐
                    │                                    ┌────►  indication_list.yaml │
                    ├── DailyMed (FDA SPL XML labels) ───┤    │  (indications)       │
                    ├── EMA (indications) ───────────────┤    ├──────────────────────┤
                    ├── PMDA (indications) ──────────────┘    │  contraindication_   │
                    │                                    └────►  list.yaml            │
                    │                                         └──────────────────────┘
                    │
                    │                                         ┌──────────────────────┐
                    ├── PVLens (label-mined AEs) ────────┐    │  adverse_event_      │
                    ├── FAERS (post-market reports) ─────┘────►  list.yaml            │
                    │                                         └──────────────────────┘
                    │
                    │                                         ┌──────────────────────┐
                    ├── PubMed (literature search) ─────────┐    │  research_list.yaml  │
                    └── CURE-ID (FDA/NCATS repurposing) ────┘────►  (ResearchAssociation │
                                                              │  List)               │
                                                              └──────────────────────┘

                                     ↓ All products ↓

                    ┌──────────────────────────────────────────────────────────────┐
                    │                     EXPORTS                                  │
                    │  Legacy CSV: drug_list_flexible.csv, drug_list_stringent.csv │
                    │  KGX:        medic_nodes.jsonl, medic_edges.jsonl            │
                    │  SSSOM:      medic_drug_mappings.sssom.tsv                   │
                    └──────────────────────────────────────────────────────────────┘

                    ┌──────────────────────────────────────────────────────────────┐
                    │                     VALIDATION                               │
                    │  linkml-validate (schema)                                    │
                    │  linkml-term-validator (ontology terms)                      │
                    │  linkml-reference-validator (evidence snippets)              │
                    └──────────────────────────────────────────────────────────────┘
```

Each pipeline follows the same pattern: **ingest** (parse, standardize, ground) -> **merge** (deduplicate, union metadata) -> **enrich** (add identifiers, tags) -> **export**. The pipeline is orchestrated by `just build-all` which runs all product builds, exports, and validation sequentially.

### Shared infrastructure

All pipelines share:

- **Entity grounding** (`src/medic/grounding/`) -- deterministic offline two-stage lexical matcher (Stage 1 string->id, Stage 2 id->canonical id). Other backends exist in `factory.py` but are not on the build path; `lexical` is the default and the only one used to build a release.
- **CURIE handling** (`src/medic/curie_utils.py`) -- all CURIE parsing uses the `curies` package with a bioregistry-backed converter (same pattern as sssom-py), never manual `str.split(":")`
- **Evidence model** (`src/medic/schema/evidence.yaml`) -- structured evidence items with source type, jurisdiction, references, snippets, and confidence
- **Validation stack** -- three-layer validation (schema, terms, references)
- **Persistent caching** -- all expensive operations cache results to `cache/` as JSON
- **HuggingFace integration** -- curated datasets (drug list, disease list) loaded via the `datasets` API from the `everycure` organization

---

## 4. Drug Pipeline (detailed)

The drug pipeline is the most mature and complex. It runs via `just build-drug-list`.

### 4.1 Data sources

MeDIC ingests drugs from 7 regulatory sources plus one curated dataset:

| Source | Jurisdiction | Module | Acquisition | Key Data |
|--------|-------------|--------|-------------|----------|
| **Orange Book** | USA (small molecules) | `medic.ingest.orangebook` | Download ZIP from FDA | Ingredient, approval date, marketing status (RX/OTC/DISCN) |
| **Purple Book** | USA (biologics) | `medic.ingest.purplebook` | Download CSV from FDA | Proper name, approval date, marketing status |
| **EMA** | Europe | `medic.ingest.ema` | Download XLSX from EMA | INN, approval date, ATC code, therapeutic indication |
| **PMDA** | Japan | `medic.ingest.pmda` | Consolidated approvals PDF fetched from pmda.go.jp (hard error if unavailable) | Active ingredient, approval date, indications |
| **Russia (GRLS)** | Russia | `medic.ingest.russia` | Local XLSX (manual export) | INN (Russian, LLM-translated), registration date |
| **India (CDSCO)** | India | `medic.ingest.india` | Year-by-year CDSCO PDFs (JSP-wrapped; hard error if none parse) | Drug name, approval date, indication |
| **China (CDE)** | China | `medic.ingest.china` | Local CSV (manual scrape, `background/cder_drugs_final_all.csv`) | Drug name (Chinese, LLM-translated to INN), approval date; drug-list-only (no indications) |
| **EveryCure** | Curated | `medic.ingest.everycure_drugs` | HuggingFace `everycure/drug-list` (1,810 drugs, Parquet) | Pre-grounded CURIEs, drug class, therapeutic area, ATC codes, boolean tags |

Source URLs for downloadable sources are configured in `conf/source_urls.yaml`, not hardcoded. For sources that require manual acquisition (PMDA, Russia, India, China), the ingest module raises a clear error if the local file is missing.

### Data acquisition notes

- **PMDA:** The approved products list is published as a PDF, parsed via `pdfplumber`. There is **no fallback** -- a missing or unparseable PDF raises `RuntimeError` (SPEC 3.1, no-legacy-fallback). A row-count floor also refuses a parse that silently yields too few records.
- **Russia:** The GRLS registry at `https://grls.rosminzdrav.ru/GRLS.aspx` can be exported to Excel via the small Excel icon in the top right, but this is a manual step with no API. Additionally, the GRLS website is unreachable from non-Russian IPs (connection refused as of April 2026), so the export requires a Russian IP or VPN. Drug names are in Russian and are translated to English via the Stage-0 Babelon/DeepL stage, with a deterministic Cyrillic-transliteration fallback. The ingest reads a manually-provided raw GRLS export at `background/grls.zip` (8 register xlsx) and fails loud if it is missing — the v1.0.0 `russia_norm.csv` path is gone. Russia records still carry no per-product GRLS URL ([#39](https://github.com/monarch-initiative/medic/issues/39)).
- **India:** CDSCO has removed their old JSP-based approval listing and now publishes approvals exclusively as year-by-year PDFs (`https://cdsco.gov.in/opencms/opencms/en/Approval_new/Approved-New-Drugs/`). The ingest parses those PDFs directly (`india/{fetch_primary,parse_pdf}.py`, 39 CDSCO year PDFs via JSP+iframe); the v1.0.0 pre-grounded path is gone. CDSCO publishes no per-drug record page, so the regulatory document URL resolves to the year batch, not the drug.
- **China:** A Selenium-based scraper (`background/cder_scraper.py`) extracts drug names from the paginated CDE registry. Drug names are in Chinese and require LLM translation.

### 4.2 Ingest pipeline (per source)

Each ingest module follows the same pattern:

```
Raw Source ──> Parse & Standardize ──> Ground (shared) ──> KB YAML (per-source)
(download      (source-specific)      (cascade)          kb/drugs/<source>/
 or local)
```

1. Parse raw file into a standardized list of drug names
2. LLM preprocessing: extract active moiety, translate non-English names
3. Ground each drug name via the cascade (see Section 9)
4. Normalize to canonical CURIEs via the Stage-2 mapping index (see 9.6)
5. Write `DrugSourceRecord` YAML entries to `kb/drugs/<source>/`
6. Write `grounding_report.yaml` with QC metrics

### 4.3 Merge

The merge pipeline (`src/medic/merge/drug_merge.py`) unifies per-source records into a single drug list:

1. **Load** all YAML files from `kb/drugs/<source>/` subdirectories
2. **Filter** out records with `grounding_status: unresolved`
3. **Group** remaining records by `normalized_id` (canonical CURIE)
4. **Merge** each group into a single drug record. The merge computes the same semantics as
   before, but projects them onto the transformation-provenance shape (§9.9): identity onto
   `mention` (`resolved_id`/`resolved_label`), per-jurisdiction approval onto `approvals[]`
   (`RegulatoryStatus`), and there are no longer any flat `curie`/`approved_*` fields.

| Merged output | Strategy (source) |
|-------|---------------|
| `mention.resolved_id` / `resolved_label` | Shared `normalized_id` / first non-empty `normalized_label` |
| `source_ingredients` | List of unique `source_name` values |
| `approvals[].authority` | One RegulatoryStatus per contributing authority (FDA←OB/PB, EMA, PMDA, CDSCO, MOH_RUSSIA, NMPA_CHINA) |
| `approvals[].marketing_status` (FDA) | Most permissive across USA sources: OTC > RX > DISCN > NONE |
| `approvals[].approval_date` | Earliest per authority (YYYYMMDD) |
| `alternate_ids` | Union of all alternate IDs, sorted |

   Consumers read these back through `src/medic/product_view.py` (e.g. `pv.drug_id(drug)`,
   `pv.approved_jurisdictions(drug)`, `pv.marketing_status_usa(drug)`).

5. **Enrichment** pipeline runs in-process on the merged list (see below)
6. **Write** output to `products/drug_list.yaml` as a `DrugList` wrapper

### 4.4 Enrichment

Enrichment runs post-merge on the unified drug list. Each enrichment step modifies drug records in-place.

#### ATC codes and SMILES

**Module:** `src/medic/enrichment/atc_smiles.py`

Multi-source cascade for chemical identifiers:

1. **Find ChEMBL ID.** Extract from `alternate_ids`, or map via UniChem.
2. **Query ChEMBL API.** Returns both ATC classifications and canonical SMILES.
3. **ATC fallbacks** (if ChEMBL returned no ATC): PubChem PUG View, DrugCentral API, RxNorm (RxNav API), ChEBI web service.
4. **SMILES fallback.** If ChEMBL returned no SMILES, query PubChem REST by CID.
5. **ATC decomposition.** The first ATC code is decomposed into hierarchical levels (`atc_main` through `atc_level5`).

Results are cached at `cache/enrichment/atc_smiles.json`.

**Note:** The v1.0.0 pipeline also scraped the WHO Collaborating Centre website (whocc.no) as a last-resort ATC lookup. This has been intentionally removed because it violates their terms of service (the website is free to browse but scraping is not permitted), is fragile (HTML parsing breaks on layout changes), and is unnecessary given the 5 other ATC sources, which currently cover 1,198 of 4,323 drugs (27.7%). If higher coverage is needed, the proper approach is to license the WHO ATC/DDD download and load it as a local lookup table.

#### Drug classification tags

**Module:** `src/medic/enrichment/drug_tags.py`

**Deterministic (ATC-derived) tags:** 9 of 11 boolean tags are determined by checking ATC code prefixes:

```python
ATC_CLASSIFICATION_MAP = {
    "is_steroid": ["H02", "D07", "R01AD", "R03BA", "S01BA", ...],
    "is_antimicrobial": ["J01", "J02", "J04", "J05", "P01", "P02", "D01"],
    "is_chemotherapy": ["L01"],
    "is_glucose_regulator": ["A10"],
    "is_vaccine_or_antigen": ["J07"],
    "is_allergen": ["V01"],
    "is_radioisotope_or_diagnostic_agent": ["V09", "V08", "V04"],
    "is_cancer_drug": ["L01", "L02"],
    "is_cardiovascular": ["C"],
}
```

**LLM fallback tags:** Two tags require LLM classification:
- `is_no_therapeutic_value` -- placebos, vehicles, diluents
- `is_metallic_salt` -- metal ion as active component

Results are cached at `cache/enrichment/drug_tags_llm.json`.

#### Combination therapy detection

**Module:** `src/medic/enrichment/combination.py`

LLM-based detection of coformulated or combination drugs. Sets `is_combination_therapy`, `combination_therapy_ingredients`, and `combination_therapy_ingredients_curies`. Cached at `cache/enrichment/combination.json`.

#### PHAROS cross-references

**Module:** `src/medic/enrichment/pharos.py`

Queries the PHAROS/TCRD GraphQL API to harvest additional cross-reference identifiers (PubChem CID, DrugCentral ID, Guide to Pharmacology ID, UNII, LyCHI hash). These are added to `alternate_ids`. Cached at `cache/enrichment/pharos.json`.

#### RxNorm Extension (planned)

**Module:** `src/medic/enrichment/rxnorm_extension.py`

Maps drugs to OMOP concept IDs via the RxNorm Extension vocabulary from OHDSI Athena. Currently a stub.

#### RxNorm substance-level resolver (grounding residue → CHEBI proposals)

**Module:** `src/medic/enrichment/rxnorm_resolve.py` — recipe `just resolve-drug-residue`.

Bridges the *unresolved* drug-string residue (product strings the deterministic lexical
grounder leaves as `sssom:NoTermFound` — biologics, foreign spellings, messy chemistry) to
CHEBI without any structure mapping. For each residue string it calls the free RxNav REST API
(`approximateTerm` → best RxCUI → its `IN` ingredient concepts → each ingredient's clean INN
name), then feeds those clean INN names **back through MeDIC's own lexical grounder** to reach
CHEBI/DRON. A false-positive guard keeps an ingredient only when its (stemmed) name is a
token/substring of the source string (RxNav's approximate matcher otherwise returns a
candidate for everything, e.g. `Sheep Pox Vaccine` → `menthol`).

**Determinism guard:** RxNav is a *network* call, so it does **not** live in the offline
matcher. It runs as a separate, cached enrichment (`cache/enrichment/rxnorm_resolve.json`,
resumable) that writes *proposed* rows into `mappings/drug_grounding.sssom.tsv` with
`mapping_justification: RXNORM`. Those rows are **locked** like manual curation
(`store.locked_rows`): an offline regrounding run reads them deterministically (short-circuits
the matcher) and never overwrites them, yet a curator can still tell an auto-proposal from a
hand-curated decision. Run after `build-drug-list`; a subsequent grounding run then resolves
the residue offline. RxNorm is US-centric, so it mainly recovers the India/Orange Book
small-molecule tail; non-US biologics/vaccines and the Cyrillic (Russia) residue remain a
genuine coverage gap (Phase 5).

---

## 5. Indications and Contraindications Pipeline

The indication pipeline extracts drug-disease associations from regulatory drug labels. It runs via `just build-on-label-list`.

### 5.1 Sources

| Source | Module | Data format | Content |
|--------|--------|-------------|---------|
| **DailyMed (FDA)** | `medic.ingest.dailymed` | SPL XML labels | Indications and contraindications sections |
| **EMA** | `medic.ingest.ema` | EPAR tables | Therapeutic indications |
| **PMDA** | `medic.ingest.pmda` | PDF/CSV | Indications (Japanese, translated) |

### 5.2 Ingest (DailyMed example)

The DailyMed ingest (`src/medic/ingest/dailymed/`) is the primary USA indication source. The pipeline:

1. **Acquire** — `acquire.py` fetches SPL XML for USA-approved drugs from the DailyMed v2 REST
   API (`spls.json?drug_name=<NAME>&doctype=<human Rx | human OTC>` → ranked setids →
   `spls/<setid>.xml`), driven by `products/drug_list.yaml`, into `data/raw/dailymed/<setid>.xml`.
   Resumable; a few hundred MB rather than the tens-of-GB bulk release. Run via
   `just ingest-dailymed-acquire`.

   Two separate faults were fixed here, and both fixes are needed (#98).

   **Which labels.** Hits are ranked — ingredient list matching the query's shape, Rx over OTC,
   systemic over inhaled over topical/ophthalmic/otic, plain over liposomal/extended-release,
   name match, then a *parsed* publication date — and only labels carrying an Indications section
   are kept. Querying per human doctype (`34391-3`, `34390-5`) keeps animal labels out of the pool
   entirely, at `pagesize=100` rather than 20. "Most recent of 20" used to pick ER hydromorphone
   (no renal colic), liposomal doxorubicin (no bone sarcoma), an ipratropium/albuterol combination
   for albuterol, veterinary ivermectin and ophthalmic flurbiprofen — and it compared
   `"Oct 14, 2026"` as a string, so September sorted above October.

   **How many labels, and under how many names.** Ranking a pool does not help when a drug's
   indications are *split* across labels rather than ranked among them, so it keeps up to
   `--labels-per-drug` (default 20) and queries **every** name the drug goes by, generic spelling
   first, pooling the hits. `lansoprazole` had two SPLs on disk and asserted exactly one
   indication — *Helicobacter pylori infection* — while GERD, duodenal ulcer and erosive
   esophagitis were in text that was never fetched; separately, 1,074 of 2,591 product-style
   names (41%) returned no SPL at all because DailyMed's `drug_name` index does not match
   "Lansoprazole Enteric-Coated Capsules". Cost, measured over 12 drugs and 221 SPLs: 118 distinct
   indication sections, i.e. ~9.8 extraction calls per drug rather than 1, because roughly half of
   a drug's SPLs repeat another's indication text verbatim and extraction is cached on the
   section-text hash.
2. **Mine** — `mine_spl_labels` parses each SPL XML, pulling the Indications & Usage section
   (LOINC 34067-9) and Contraindications section (LOINC 34070-3) text plus active-moiety names and the
   setid (from the `<setId root=...>` attribute). It also reads bulk-release `*.zip` archives if present.
   Per-element chunks are kept alongside the flattened text so the negation screen can scope cues
   to the element containing them. Homeopathic labels (marketing category `C73614`, or `HPUS` in
   the text) are skipped here. Active moieties are corrected for two SRS conventions: a bare
   counter-ion is dropped **when the label has another active moiety** (so lithium, gadolinium
   contrast agents and the radiopharmaceuticals, whose only moiety is an ion, survive), and an
   active metabolite is resolved back to its prodrug via the label's own `<genericMedicine>` name
   (`RAMIPRILAT` → `RAMIPRIL`). The FDA UNII on each `<activeMoiety><code>` is captured here too.
3. **Extract** — one LLM call per section returns *structured claims*, not bare disease names.
   See §5.8.
4. Grounds drug names to ChEBI CURIEs and disease names to Mondo CURIEs via the shared grounding cascade.
   A label none of whose moieties ground contributes nothing at all — the loop skips before
   extraction — so those are counted and reported as a warning at the end of the run rather
   than logged at debug level. 107 distinct moiety names currently never ground, and the
   common cause is one policy: the string is present in the index only as a `relatedSynonym`,
   often of several objects at once (`tenofovir` matches three, `thiamine` three). The ingest
   does **not** retry with a mangled string to get around that — a transform that earns a
   match belongs in `PreprocessingRuleEnum` and the SSSOM store, so the chain from the
   label's literal to the published id stays replayable (I-8).
5. Constructs `IndicationAssociation` records (USA-only evidence per source isolation) with normalized
   drug/disease identifiers, relationship type, raw label text, and setid-derived
   `reference`/`source_document_url` evidence (see §5.6).
6. Writes per-source YAML to `kb/indications/dailymed/`.

EMA, PMDA and India call the same two extractors, so they receive the structured-claim contract
of §5.8 — but they do **not** pass an ingredient list, so their claims carry no attribution.

The SPL-XML path is the single DailyMed acquisition path. An empty SPL directory is a hard error
(the ingester fails loudly, naming `just ingest-dailymed-acquire`) rather than silently degrading to
any legacy table — there is no legacy Excel fallback.

### 5.3 Schema

The indication schema (`src/medic/schema/indication.yaml`) models:

- **`IndicationAssociation`** -- a canonical drug-disease pair with relationship type, reliability, a noisy-OR confidence, and an `assertions` list of per-source-document `SourceAssertion`s (each with its own mentions, spans, evidence and regulatory status)
- **`Hyperrelation`** -- symptom-level specificity within an indication (e.g., "reduces tremor in Parkinson's"), with target symptom (HPO), relationship type (REDUCES, PREVENTS, TREATS, CONTRAINDICATES_IN, WORSENS), and supporting text
- **`IndicationList`** -- collection wrapper

Contraindication-specific fields include `is_allergen` and `is_diagnostic_agent`.

### 5.4 Merge

The indication merge (`src/medic/merge/on_label_merge.py`):

1. Reads all `kb/indications/<source>/*.yaml` files
2. Deduplicates by composite key: `(drug_id, disease_id, relationship_type, context)`, where
   *context* is the claim's disease/population/temporal qualifiers. Two claims about the same
   drug and disease that differ in context or relation are two claims, not duplicates — this is
   what lets formoterol be indicated for asthma *maintenance* and contraindicated in *acute*
   asthma without either side demoting the other.
3. Merges evidence and hyperrelations across sources (union)
4. Sets source flags: `fda`, `ema`, `pmda`
5. Splits output into:
   - `products/indication_list.yaml` -- indications
   - `products/contraindication_list.yaml` -- contraindications

### 5.5 Source isolation invariant

Each ingester emits evidence rows only for the jurisdiction it itself originates. DailyMed → USA only, EMA → EU only, PMDA → JAPAN only, etc. Cross-jurisdiction *merging* happens here at merge time and is allowed; cross-jurisdiction *emission* at ingest is forbidden, even when an upstream raw file carries cross-jurisdictional flag columns. See `docs/source-isolation.md` for the full rule, the historical bleeding bug it fixed, and the enforcement guidance.

### 5.6 Regulatory document URLs — design choices

Each regulatory evidence row carries two URL fields:

- **`reference`** / **`regulatory_document_url`** — the canonical landing URL for the source's record (e.g. Drugs@FDA detail page, Purple Book search page, EPAR product landing).
- **`source_document_url`** (optional) — a direct link to the underlying PDF when one can be constructed deterministically from source metadata.

Per-source policy:

| Source | `regulatory_document_url` | `source_document_url` |
| --- | --- | --- |
| DailyMed | `dailymed.nlm.nih.gov/dailymed/lookup.cfm?setid=<setid>` | `dailymed.nlm.nih.gov/dailymed/downloadpdffile.cfm?setid=<setid>` |
| Drugs@FDA (Orange Book) | `accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&ApplNo=<NDA>` | **deliberately not emitted** — see below |
| Purple Book | `purplebooksearch.fda.gov/?query=<BLA>` | **deliberately not emitted** — see below |
| EMA EPAR | `ema.europa.eu/en/medicines/human/EPAR/<slug>` | `ema.europa.eu/en/documents/product-information/<slug>-epar-product-information_en.pdf` |
| PMDA | review-report PDF when known, else `PmdaSearch/iyakuSearch/` | review-report PDF when known |
| CDSCO | `cdsco.gov.in/.../Approved-New-Drugs/` index | not emitted (source publishes only year-batch PDFs) |
| MOH_RUSSIA (GRLS) | `grls.rosminzdrav.ru/Default.aspx` index | not emitted (source publishes per-product pages, but routingGuid scraping is auth-gated) |

**Decision: Drugs@FDA and Purple Book document URLs are intentionally limited to the website detail page, not the PDF.** FDA does not publish a deterministic per-NDA / per-BLA label PDF URL. Constructing one would require per-record HTML scraping of `accessdata.fda.gov` (the year and supplement-number segments aren't in the Orange Book bulk download) and similarly for Purple Book (per-BLA detail pages are JavaScript-rendered modals). The maintenance cost of such a scraper outweighs the marginal benefit — reviewers can already reach the PDF in two clicks from the detail page. Revisit only if FDA publishes a stable JSON manifest of label URLs.

For sources where the document URL *is* deterministic (DailyMed via setid, EMA via EPAR slug, PMDA review-report PDFs from `review_lookup`), emit `source_document_url` directly at ingest.

### 5.7 Mondo downfilling (removed)

The v1.0.0 pipeline propagated indications down the Mondo disease hierarchy (e.g., a drug approved for "breast cancer" would generate indication records for all subtypes like "breast diffuse large B-cell lymphoma"). This has been intentionally removed.

**Rationale:** In ontology reasoning, annotations travel **up** the hierarchy, not down. If a drug treats a specific subtype, you can infer it treats the parent class — but a drug approved for a broad category is not necessarily indicated for every subtype. Downfilling inflated the indication list with unvalidated associations. Consumers who need hierarchical inference should apply it at query time using the Mondo graph, not bake it into the data.

### 5.8 The structured claim contract

Extraction returns one **claim** per therapeutic statement rather than a flat list of disease
names. A claim is a `Claim` NamedTuple carrying:

| field | what it holds |
|---|---|
| `target` + `target_verbatim` | the condition treated, and the label's own wording for it, copied character-for-character |
| `predicate` | `treats` \| `prevents` \| `reduces_risk_of` \| `maintenance` \| `diagnoses` |
| `attributed_to` + `basis` | the ingredient the text credits, or `product`; and how that was decided |
| `disease_context` | a qualifying condition the patient must already have |
| `population` | age group, sex, pregnancy, transplant recipient … |
| `temporal_context` | post-MI, following hip replacement … |
| `co_administered_with` | drugs the claim is conditioned on |

**Only `target` becomes the edge object.** The rest become Biolink qualifiers on the
association (`disease_context_qualifier`, `population_context_qualifier`,
`temporal_context_qualifier`, `sex_qualifier`) or, for co-administration, which Biolink 4.4.6
does not model on this association, a namespaced `medic_co_administered_with`.

This replaced four separate defects with one prompt change. A flat list of names promoted the
population or setting to the object, dropped the restricting context entirely, credited every
ingredient of a combination with the whole product's indications, and recorded prevention as
treatment. The qualifier half is invisible to a precision audit — a dropped qualifier scores as
*correct* — so the improvement is only measurable once the rubric counts it.

`attributed_to` defaults to `product` with `basis: product_level`, because labels almost always
make the product the grammatical subject. An ingredient is named only for the recurring explicit
patterns: `component_phrase` ("the naproxen component …"), `ingredient_subject`, `because_of`,
`when_used_as_component`, `subheading`.

Two guards exist because the format itself can induce errors:

- **A clause that names no condition produces no claim.** Pressed to always supply a `target`,
  a model will invent one — "contraindication in pediatric population" for a pure age
  restriction. This does not fail loudly: the disease index holds 471 strings matching
  "contraindicat" (SNOMED procedure concepts), so a fabricated target grounds cleanly and
  ships looking plausible. There is a prompt rule *and* a deterministic reject in
  `looks_like_disease_name`.
- **Token budget is sized for the format.** A structured record is five labelled lines where a
  pipe-list entry was a few words; at the old limit, long sections truncated mid-record and the
  parser tolerates a short final record, so claims after the cut vanished silently.

The raw model answer is what gets cached (keyed on the section-text hash), not the parse, so the
parser can improve without re-asking. Note that the key does **not** include the prompt, so a
prompt change needs a deliberate cache-namespace bump — `v3` is the current generation.

### 5.9 Reliability gates

`src/medic/reliability.py` scores each statement as the worst of the applicable gates *within*
an assertion, then takes the **strongest** across the assertions backing a pair, so corroboration
by a second regulator raises the tier rather than lowering it. Two gates come from the external
audit:

- **`_attribution_gate`** caps at LOW any assertion from a multi-ingredient label, using the
  openFDA ingredient count. Pairs supported *only* by combination labels were measured 65%
  wrong. Interim by construction: it says "this could be a fan-out", not "this is wrong".
- **`_context_gate`** caps at LOW when the object was judged population-or-setting at ingest,
  where the sibling diseases from the same section are in hand. LOW rather than excluded,
  because a preposition is weaker evidence than a deletion needs.

Nothing reaches HIGH by absence of signal: the provenance gate runs on every statement, and an
empty gate list scores LOW rather than HIGH.

### 5.10 Scoring against the external audit

`just audit-regression` scores the built export against a 300-edge blind label audit, reporting
**both** directions — errors removed *and* correct edges kept. Precision alone can be improved
by dropping everything, which is exactly what a one-sided metric would call success.

Matching is by id *and* by normalised label, because an id-only join counts a drug whose primary
id moved (e.g. `UNII:` → `CHEBI:`) as a lost edge. The audit file is licensed with
drug-disease-compare and is fetched rather than vendored; the script exits 2 saying so rather
than appearing to pass without it.

---

## 6. Disease List Pipeline

The disease list pipeline runs via `just build-disease-list`.

### 6.1 Source

The disease list is ingested from the HuggingFace dataset `everycure/disease-list` (23,148 diseases, Parquet format, CC-BY-4.0). This dataset is derived from the Mondo Disease Ontology with EveryCure curation. HuggingFace is the **single** source -- there is no local-file fallback; an unavailable dataset raises `RuntimeError` (SPEC 9, no-legacy-fallback). The ingest module is at `src/medic/ingest/disease_list/__main__.py`.

### 6.2 Ingest process

1. Load dataset via `datasets.load_dataset("everycure/disease-list", split="train")`
2. Convert to pandas DataFrame
3. For each row, construct a `Disease` record with:
   - `category_class`: from `id` column — Mondo CURIE (e.g., `MONDO:0017545`)
   - `label`: from `name` column
   - `definition`: text definition
   - `synonyms`: alternative names (semicolon-separated string, split to list)
   - `subsets`: Mondo subset tags (semicolon-separated, split to list)
   - `crossreferences`: external database links (MEDGEN, UMLS, GARD, Orphanet, ICD)
4. Map boolean filter flags directly from dataset columns (70+ flags including speciality classifications)
5. Write to `kb/diseases/disease_list.yaml`
6. Run schema and term validation

### 6.3 Filter flags

The disease schema defines 26 boolean filter flags used for matrix construction:

| Flag category | Example flags | Purpose |
|---------------|--------------|---------|
| **Rarity** | `f_gard_rare`, `f_nord_rare`, `f_is_rare` | Mark rare diseases from GARD, NORD, Orphanet |
| **Heredity** | `f_mondo_top_grouping_hereditary_disease`, `f_mondo_top_grouping_chromosomal_disorder` | Classify hereditary and chromosomal conditions |
| **Disease type** | `f_is_cancer`, `f_is_infectious`, `f_is_metabolic`, `f_is_autoimmune`, `f_is_neurological`, `f_is_cardiovascular` | Broad disease category flags |
| **Ontology structure** | `f_leaf`, `f_grouping_subset`, `f_orphanet_subtype`, `f_omimps` | Ontology hierarchy position |
| **Evidence** | `f_has_phenopackets`, `f_has_phenotype_annotations`, `f_has_gene_associations`, `f_has_treatment` | Available evidence and annotations |
| **Curation** | `f_matrix_manually_excluded`, `f_matrix_manually_included`, `f_clingen` | Manual curation overrides |
| **Scope** | `f_in_scope` | Computed final filter: whether this disease is in scope for analysis |

---

## 7. Adverse Events Pipeline

The adverse events pipeline runs via `just build-adverse-event-list`. Currently a stub awaiting data source integration.

### 7.1 Sources

| Source | Module | Description |
|--------|--------|-------------|
| **PVLens** | `medic.ingest.pvlens` | Label-mined adverse events from FDA drug labels. Extracts adverse reactions from structured product labeling sections (ADVERSE_REACTIONS, BLACK_BOX_WARNING, WARNINGS_AND_PRECAUTIONS, POST_MARKET). |
| **FAERS** | `medic.ingest.faers` | Post-market adverse event reports from the FDA Adverse Event Reporting System. Quarterly data files with case-level reporting. |

### 7.2 Schema

The adverse event schema (`src/medic/schema/adverse_event.yaml`) models:

- **`AdverseEventAssociation`** -- links a drug (ChEBI CURIE) to an adverse event (MedDRA term, with optional HPO mapping)
- Key fields: `label_section` (where in the label the AE was found), `frequency`, `severity` (MILD through FATAL), `sources`, `evidence`
- **`AdverseEventList`** -- collection wrapper

### 7.3 Planned ingest pipeline

1. Parse PVLens/FAERS output for drug-adverse event pairs
2. Ground drugs to ChEBI via the shared grounding cascade
3. Map MedDRA adverse event terms to HPO where possible (using UMLS mappings or OAK)
4. Write per-source YAML to `kb/adverse_events/<source>/`

### 7.4 Merge

The adverse event merge (`src/medic/merge/adverse_event_merge.py`):

1. Reads all `kb/adverse_events/<source>/*.yaml` files
2. Deduplicates by composite key: `(drug_id, adverse_event_term)`
3. Unions source lists and evidence across sources
4. Writes `products/adverse_event_list.yaml`

---

## 8. Research Pipeline

The research pipeline discovers drug-disease associations from published literature. It runs via `just build-research` for compilation, and `just research-curate` or `just research-batch` for curation.

### 8.1 Pipeline overview

```
Disease queue (from priority list)
       |
       v
  PubMed search (drug + disease terms)
       |
       v
  LLM extraction (drug-disease pairs with evidence)
       |
       v
  Snippet validation (against PubMed abstracts)
       |
       v
  ResearchAssociation YAML (cached per disease)
       |
       v
  Compilation (just build-research)
       |
       v
  products/research_list.yaml
```

### 8.2 Schema

The research source schema (`src/medic/schema/research_source.yaml`) defines:

- **`ResearchAssociation`** -- a drug-disease pair from literature, with:
  - Normalized drug and disease identifiers
  - `curation_status`: DRAFT, REVIEWED, VALIDATED, or REJECTED
  - `search_query`: the PubMed query that found the literature
  - `evidence`: list of `EvidenceItem` records with PMID references, snippets, confidence levels, and research phase
  - `deep_research_used`: whether deep research tools were employed
  - `curator` and `curation_date`: provenance tracking

### 8.3 Interactive curation workflow

The research pipeline supports interactive curation via `just research-curate`:

1. Load next uncurated disease from the priority queue
2. Show disease context (name, categories, phenotypes)
3. Run PubMed search, present candidate drug-disease pairs
4. For each pair: LLM drafts evidence, user reviews/edits
5. Write validated `ResearchAssociation` YAML to `kb/research/`
6. Update progress tracker

### 8.4 Deep research

For high-priority disease-drug pairs, the pipeline supports deep research via external providers (Perplexity, Falcon, Cyberian):

```bash
just research-disease perplexity "Marfan syndrome" MONDO:0007947
just research-disease-batch perplexity count=5
```

Deep research produces richer evidence with more references and is tracked via the `deep_research_used` flag.

### 8.5 CURE-ID ingest

CURE-ID (https://cure.ncats.io/) is a collaboration between FDA and NCATS that collects real-world drug repurposing evidence from case reports. The ingest module (`src/medic/ingest/cureid/__main__.py`) downloads the pre-mapped TSV from the NCATS open data portal, filters to drug→disease treatment edges (`biolink:applied_to_treat`), and aggregates by (drug, disease) pair into `ResearchAssociation` records.

Key details:
- **Source**: `https://opendata.ncats.nih.gov/public/cureid/cureid_data.tsv` (~240 rows, case-centered KG)
- **Filtering**: Only `subject_type=Drug` + `predicate=applied_to_treat` edges are kept. Gene, variant, and adverse event edges are excluded.
- **Aggregation**: Multiple case reports for the same drug-disease pair are merged into one association. Drug→PhenotypicFeature edges that share a `report_id` with a disease edge are folded into the disease association's `notes` field; orphan phenotype edges become standalone associations.
- **Evidence**: Each association carries a `DATABASE` evidence item (CURE-ID source attribution with report_id and outcome) and `LITERATURE` evidence items for any associated PMIDs.
- **Qualification**: All associations are marked `approval_status: OFF_LABEL`, `max_research_phase: CASE_REPORT`, `evidence_source: HUMAN_CLINICAL`. Confidence is `MEDIUM` for positive outcomes (improved/recovered) and `LOW` otherwise.
- **Output**: `kb/research/cureid_associations.yaml`
- **Justfile**: `just ingest-cureid`

Drug CURIEs are mostly pre-grounded to ChEBI; one UNII CURIE (Dupilumab) is present. Disease CURIEs are mostly MONDO with some ORPHA and UMLS identifiers.

### 8.6 Compilation

The research compiler (`src/medic/research/compile.py`) reads all `kb/research/*.yaml` files and merges them into `products/research_list.yaml`.

---

## 9. Entity Grounding Layer

Entity grounding maps free-text names (drug names, disease names) to canonical ontology identifiers. This is a shared subsystem used across all pipelines, implemented in `src/medic/grounding/`.

### 9.1 Why a cascade

Different grounding services have different coverage, accuracy, and speed characteristics. No single service resolves all names correctly. The cascade tries multiple backends in order and accepts the first result that exceeds a confidence threshold.

### 9.2 The cascade

The `CascadeGrounding` class (`src/medic/grounding/cascade.py`) tries backends in this order:

| Order | Backend | Type | Confidence scoring | Strengths |
|-------|---------|------|-------------------|-----------|
| 1 | **OAK** | Offline (ChEBI SQLite) | 1.0 exact label, 0.9 exact synonym | Fast, precise, no network |
| 2 | **Gilda** | Offline (term lists) | Native scored fuzzy match | Good fuzzy matching, handles synonyms |
| 3 | **NameRes** | SRI API | Jaro-Winkler similarity | Broad coverage across Biolink |
| 4 | **OLS** | EBI API | Jaro-Winkler similarity | Fallback for rare terms |

The cascade accepts the first result with confidence >= 0.80. If no backend exceeds this threshold, all candidates from all backends are collected and sorted by score for the reranking step.

### 9.3 LLM preprocessor

Before grounding, an LLM normalizes the entity name (`src/medic/grounding/preprocessor.py`):

```
Input:  "FENTANYL CITRATE INJECTION 200MCG/ML"
Output: "fentanyl"  (active moiety extracted)
```

The preprocessor:
- Extracts the active moiety (strips salt forms, dosages, formulations)
- Translates non-English names (Japanese, Russian, Chinese) to English INN
- Detects multi-ingredient combinations

The LLM returns structured JSON with `active_moiety`, `is_combination`, `components`, and `confidence` fields. Results are cached at `cache/grounding/preprocessor.json`.

### 9.4 Confidence tiers

Confidence scores are computed using Jaro-Winkler string similarity (`src/medic/grounding/confidence.py`) and classified into action tiers:

| Score range | Tier | Action |
|------------|------|--------|
| >= 0.95 | `AUTO_ACCEPT` | Accepted without review |
| 0.80 -- 0.94 | `REVIEW_RECOMMENDED` | Accepted, flagged for periodic audit |
| 0.50 -- 0.79 | `LLM_RERANK` | LLM selects best candidate from all backends |
| < 0.50 | `UNRESOLVED` | Persisted in grounding report, not silently dropped |

### 9.5 LLM reranker

When confidence falls in the 0.50--0.79 range, the LLM reranker (`src/medic/grounding/reranker.py`) receives the entity name and up to 30 candidates from all backends. It returns a structured selection with reasoning. The selected candidate's score is boosted to at least 0.85. Results are cached at `cache/grounding/reranker.json`.

### 9.6 Stage-2 canonicalization

After grounding, the Stage-1 id is normalized to a canonical id (Mondo for diseases, ChEBI for
drugs) by `src/medic/normalization/`, using an offline index built from the mappings the target
vocabulary itself asserts (`just build-normalization-index`). Every decision -- including an
identity normalization -- is written to `mappings/{disease,drug}_normalization.sssom.tsv` and
emitted as a `NormalizationStep` (I-8, I-11).

This replaced a network call to NodeNorm (SRI Node Normalization Service). NodeNorm is no longer
used anywhere in `src/`: it made the build non-reproducible and network-dependent, and it could
not record *why* a mapping was chosen, which I-4 requires.

### 9.7 Persistent disk cache

All grounding results are cached to disk at `cache/grounding/<source_name>.json`. The cache is keyed by cleaned name and checked before any API call. This eliminates redundant API calls across pipeline runs and across sources that share entity names.

### 9.8 Grounding report

Each ingest run produces a grounding report at `kb/drugs/<source>/grounding_report.yaml` with counts of auto-accepted, review-recommended, LLM-reranked, and unresolved entities, plus a list of all unresolved names. This provides QC visibility into grounding quality per source.

### 9.9 Transformation-provenance model (the `Mention` trail)

Every step from a verbatim source string to a canonical id — extraction, translation, grounding, normalization — is captured as a first-class step so the whole chain is replayable from one object (invariant I-8). The model is a **standalone, reusable LinkML schema** (`src/medic/schema/provenance.yaml`, namespace `w3id.org/monarch-initiative/transformation-provenance`), which MeDIC imports:

- **`TransformationStep`** (abstract) records each step's `input_value` and `output_value` (the I-8 in→out pair), plus `method`, `agent`, `confidence`, `status`, and controlled-enum `applied_rules`/`quality`/`flags`. Four concrete subclasses narrow those enums: **`ExtractionStep`** (source text or cell → mention string — *entity recognition only*; `ExtractionFlag`), **`TranslationStep`** (foreign → English; `TranslationFlag`), **`GroundingStep`** (string → id; `PreprocessingRuleEnum`/`GroundingQualityEnum`/`GroundingFlag`), **`NormalizationStep`** (id → canonical id).
- **Every step records what ran it** — `tool` + `tool_version`, and for non-deterministic steps `agent` + `agent_version`. For an LLM the agent version is the **dated model id** (`claude-haiku-4-5-20251001`), the pin that makes a record comparable to a re-run (FAILURE_MODES §13.1). Resolved centrally in `src/medic/versions.py`: MeDIC components carry hand-bumped component versions (`medic-lexical-grounder/1`), third-party tools their distribution version (`babelon/0.3.6`), per-source ingest parsers the released MeDIC version. The full `1.0.0.postN.devN+<commit>` package version is deliberately *not* stamped — it would rewrite every product record on every commit.
- **`Assertion`** is the deliberate counterpart: a Mention answers *"what entity is this string?"*, an Assertion answers *"what is the source claiming about it?"* (supporting quote, `confidence`, `negation_cue`, and the relation-level `AssertionFlag` — `negated_inversion`/`over_extraction`/`polarity_unverified`/`wrong_section`/`wrong_pairing`). The relation itself is named by the owning record's `relationship_type`, never repeated. This split matters because an entity can be recognised perfectly while the asserted relation is wrong (VITAMIN A → hyperthyroidism, listed as a *depleting condition*): that is an `over_extraction` on the **assertion**, not an extraction failure on the mention.
- **`Mention`** holds its `resolution` container — `resolution.pipeline` (the ordered steps) plus an aggregate `input_value`/`output_value` and a product `confidence` — the verbatim `original_literal` (its `MEDICNE` `id`), the `source_spans` (`TextSpan` list — the section text quoted by the extraction), and the `resolved_id`/`resolved_label`. Chaining (`output[i]==input[i+1]`, ending at `resolved_id`) is enforced at assembly.
- Every transform *action* and failure-mode `flag` is an enum value (I-8, enum-first): the `PreprocessingRuleEnum` + `GroundingQualityEnum`/`NormalizationQualityEnum` moved here from `grounding.yaml`, joined by the new `ExtractionFlag`/`TranslationFlag`/`GroundingFlag`/`NormalizationFlag` (cross-referenced to `FAILURE_MODES.md`).

**Adoption.** `Drug.identity` carries the drug's identity trail — a `STRUCTURED_FIELD` `ExtractionStep` on the verbatim source cell, then (translation for zh/ru →) grounding (with the Stage-1 `applied_rules` funneled from the SSSOM store) → normalization — and `Drug.approvals` is a `RegulatoryStatus` list.

`IndicationAssociation` is **two-level** (spec `2026-08-09-source-scoped-association-provenance-design.md`). The top level is one canonical `(drug_id, disease_id, relationship_type)` pair carrying only cross-source judgements: `reliability` and a noisy-OR `confidence`. **All provenance lives on `assertions`, one `SourceAssertion` per source *document*** — a DailyMed setid, an EMA EPAR, a PMDA PDF, a CDSCO entry. Each assertion inlines its own `drug` and `disease` `Mention`s built from *that document's* literals, its own typed `spans`, its own singular `evidence` and `regulatory_status`, and the `assertion` holding claim-level provenance.

This replaced a model in which one record mixed three provenance scopes — a merge-elected drug trail, a first-source-wins disease trail, and an all-sources evidence list — which produced associations whose drug trail came from a different jurisdiction than the indication text, left 462 associations with no drug trail at all, and silently dropped ~3,000 document-level attestations. Invariant I-10 now makes single-sourcedness checkable, and the rebuild recovers those attestations: **8,737 pairs carrying 11,915 assertions**.

**Construction.** `src/medic/provenance_build.build_mention()` assembles a `Mention` (its `resolution.pipeline` of ordered steps) from the stage objects the merge carries. `merge/drug_merge.py` builds the drug `identity`; `merge/on_label_merge.py` builds **both** mentions per source document — `_build_drug_mention` and `_build_disease_provenance` are mirror images, each recovering its trail from the corresponding `mappings/*_grounding.sssom.tsv` decision store — then `_append_assertion` / `_finalize_pair` assemble the pair and stamp `reliability`. `src/medic/spans.py` turns each source's raw text into typed `TextSpan`s so an extraction can name which span it read (I-8b) and negation is scoped to that span rather than to the whole flattened section. `readable_span_indices` is the single definition of which spans a claim may be read from (everything but `SECTION_HEADER`/`SUBSECTION_HEADER`/`LIMITATION_STATEMENT`), read by **both** the reporting half here and the destructive ingest gate in `medic.validation.extraction_fidelity.screen_indications`; they previously held separate definitions and only this one was ever fixed, which let a `Limitations of Use` cue drop genuine approvals at ingest (#59). When no anchor can locate a disease in its source text at all, the claim is stamped `polarity_unverified` — the negation check did not run, which is not the same as its having passed. Read-side access is centralised in `src/medic/product_view.py` — `assoc_evidence`, `assoc_authorities`, `assoc_jurisdictions`, `assoc_mentions`, `assoc_claims` — which every consumer (KGX/legacy/SSSOM exports, `coverage.py`, `reliability.py`, `reliability_export.py`) goes through, so the two-level shape is not smeared across modules.

**How recording, caching, and assembly fit together.** The `resolution` block is a *materialized view* of the hand-editable `mappings/` decision stores (SSSOM grounding/normalization + Babelon translation, §9.8/§6), which are at once the authoritative record (I-4) and the deterministic offline cache — a filled row is never recomputed (no DeepL, no re-ground). Recording happens once at ingest; assembly (`build_mention`) is a pure offline read at merge that re-expresses the recorded decisions and funnels the Stage-1 `applied_rules` back off the SSSOM row; a curator edits a store TSV and rebuilds. Two full end-to-end traces — a drug and a disease, with the real backing store rows — are in [`docs/provenance-walkthrough.md`](provenance-walkthrough.md).

---

## 10. Validation Stack

MeDIC uses a three-layer validation stack, modeled after the dismech project. All validators are invoked from the justfile.

### 10.1 Schema validation

```bash
just validate-schema <file> [target_class]
```

Runs `linkml-validate` against the master schema (`src/medic/schema/medic.yaml`). The target class is auto-detected from the file path (e.g., files containing "drug" validate as `DrugList`). Validates structural correctness: required fields, value ranges, enum constraints.

### 10.2 Term validation

```bash
just validate-terms <file> [target_class]
```

Runs `linkml-term-validator` to verify that all ontology term IDs exist and labels match canonical labels. OAK adapters are configured in `conf/oak_config.yaml`:

```yaml
ontology_adapters:
  MONDO: sqlite:obo:mondo
  HP: sqlite:obo:hp
  CHEBI: sqlite:obo:chebi
  MAXO: sqlite:obo:maxo
```

### 10.3 Reference validation

```bash
just validate-references <file> [target_class]
```

Runs `linkml-reference-validator` to validate evidence snippets against source documents. For literature evidence, this checks that quoted snippets actually appear in the PubMed abstract (anti-hallucination). Configuration is in `conf/reference_validator_config.yaml`.

### 10.4 Combined validation

```bash
just validate <file>        # schema + terms + references for one file
just validate-all           # schema validation across all product files
```

The `validate-all` target checks:
- `products/drug_list.yaml` (DrugList)
- `products/indication_list.yaml` (IndicationList)
- `products/contraindication_list.yaml` (IndicationList)
- `products/adverse_event_list.yaml` (AdverseEventList)
- `products/disease_list.yaml` (DiseaseList)

---

## 11. Export Formats

### 11.1 Legacy CSV exports

**Module:** `src/medic/export/legacy.py`
**Target:** `just export-legacy`

Generates files matching the v1.0.0 release format:

| File | Format | Description |
|------|--------|-------------|
| `exports/drug_list_flexible.csv` | CSV (31 columns) | All drugs, column-compatible with v1.0.0 release |
| `exports/drug_list_stringent.csv` | CSV | Filtered to drugs with high-confidence grounding |
| `exports/orangebook.xlsx` | XLSX | Orange Book source drugs |
| `exports/purplebook.xlsx` | XLSX | Purple Book source drugs |
| `exports/ema.xlsx` | XLSX | EMA source drugs |
| `exports/pmda.xlsx` | XLSX | PMDA source drugs |
| `exports/russia.csv` | CSV | Russia source drugs |
| `exports/india.csv` | CSV | India source drugs |

### 11.2 KGX export (Biolink knowledge graph)

**Module:** `src/medic/export/kgx/` · **Targets:** `just export-kgx`, `just validate-kgx`
**Biolink Model:** pinned at `4.4.6` · **Design:** [`specs/2026-08-13-kgx-export-design.md`](../specs/2026-08-13-kgx-export-design.md)

The KGX export is the complete graph view of MeDIC — every product, not just drugs and
indications. It is built in two layers: a strictly Biolink-valid core that a Translator
ingest consumes unchanged, plus a `medic_`-namespaced extension layer carrying what Biolink
has no slot for and a consumer needs on the record itself. A strict consumer drops the
extension layer with one rule and still has a valid graph.

**Every record is an instance of a Biolink class.** The core of each node and edge is built
by instantiating the matching class of the `biolink-model` pydantic model (generated from the
same 4.4.6 schema) and dumping it. The class fixes `category`, rejects any property it does
not declare (`extra="forbid"`), rejects enum values outside the permissible set and a list
where it wants a scalar, and requires what is required. A term Biolink does not define
therefore cannot reach the file. Six Biolink slots the model defines but does not attach to
every class here (`clinical_approval_status` and `max_research_phase`, which 4.4.6 put on
the `treats` class but not the contraindication class; `temporal_context_qualifier` and
`sex_qualifier`; the two `*_location_in_text` offsets) are written beside the dumped core
where the class lacks them; they are listed once in `biolink.py` with the reason each is
there, and a test pins the list.

**One edge per source assertion, not per pair.** Each edge is single-sourced by construction
(the same property invariant I-10 enforces on `SourceAssertion`), so it carries exactly one
`primary_knowledge_source`, one document, one quoted span and one confidence. Pair-level
aggregates ride along on every edge as `medic_pair_*`, so the collapsed drug–disease view is
a `GROUP BY (subject, predicate, object)` rather than a re-derivation.

**Nodes** — one category each, the class's own:

| Type | Category | Content |
|---|---|---|
| Drug | `biolink:Drug` | name, synonyms, xrefs (incl. the SPL-asserted UNII), `available_from` (RX/OTC), ATC codes, SMILES, features, per-jurisdiction approval summary, reliability, `MEDICNE:` mention id |
| Disease | `biolink:Disease` | all 23,224 from the disease list — definition, synonyms, crossrefs, MONDO `subsets` (a Biolink slot since 4.4.6), and the `f_*` filter flags that are true |
| Stub | inferred from id prefix | edge endpoints no product describes (unmapped `UNII:`/`ORPHA:`/`UMLS:` ids), marked `medic_stub` so the graph stays referentially closed |

**Edges** — predicate chosen from recorded data, never from a source's name, and the
association class chosen by what it constrains:

| Statement | Predicate | Association class (`category`) | knowledge_level |
|---|---|---|---|
| Approved regulatory indication | `biolink:treats` | `ChemicalOrDrugOrTreatmentToDiseaseOrPhenotypicFeatureAssociation` | `knowledge_assertion` |
| Vaccine / risk-reduction indication | `biolink:preventative_for_condition` | same | `knowledge_assertion` |
| Diagnostic-agent indication | `biolink:diagnoses` | `ChemicalEntityToDiseaseOrPhenotypicFeatureAssociation` | `knowledge_assertion` |
| Indication not recorded as approved | `biolink:treats_or_applied_or_studied_to_treat` | `ChemicalOrDrugOrTreatmentToDiseaseOrPhenotypicFeatureAssociation` | `not_provided` |
| Contraindication | `biolink:contraindicated_in` | `ChemicalEntityToDiseaseOrPhenotypicFeatureAssociation` | `knowledge_assertion` |
| Research — `max_research_phase: CASE_REPORT` | `biolink:applied_to_treat` | `ChemicalOrDrugOrTreatmentToDiseaseOrPhenotypicFeatureAssociation` | `observation` |
| Research — numbered trial phase | `biolink:in_clinical_trials_for` | same | `observation` |
| Research — other literature | `biolink:studied_to_treat` | same | `observation` |
| Adverse event — PVLens (label-listed) | `biolink:has_side_effect` | `ChemicalOrDrugOrTreatmentSideEffectAssociation` | `knowledge_assertion` |
| Adverse event — FAERS (reported) | `biolink:has_adverse_event` | `ChemicalOrDrugOrTreatmentAdverseEventAssociation` | `observation` |

The treats-family class restricts its predicate to `treats or applied or studied to treat`,
which every predicate in those rows specialises (`preventative_for_condition` through its
`treats` mixin). `contraindicated_in` and `diagnoses` specialise nothing any class restricts
to, so they take the unrestricted chemical-to-disease class rather than the bare
`Association`. The gate checks the constraint, since the pydantic class types `predicate` as
a plain string.

`agent_type` is **derived** from `assertion.method` / `assertion.agent.agent_type` — an
LLM-extracted DailyMed indication is a `text_mining_agent`, a structured field is a
`data_analysis_pipeline`. It is never asserted as `manual_agent` wholesale. *Which* LLM and
extractor version produced the build is one fact per build and lives in
`medic_kgx_metadata.yaml` (`extraction_agents`), not on every edge.

**Where the source record lives.** The document a claim was read from — its CURIE
(`DailyMed:<setid>`), the landing page and the PDF — is Biolink's `source_record_urls`, a
property of `RetrievalSource`. Every edge therefore carries one nested `sources` entry for its
primary knowledge source holding those references. That is the one nested payload Biolink
defines for an association's provenance; it replaces six flat `medic_*_url`/`medic_document`
properties. The aggregator is not repeated inside `sources`, since
`aggregator_knowledge_source` already states it.

MeDIC's verbatim source strings map onto standard Biolink slots: `original_subject` /
`original_object` (the I-7 literals), `supporting_text` (the quoted label span, a
one-element list — the slot is multivalued), `subject_location_in_text` /
`object_location_in_text` (character offsets), and on research edges `description` (the
per-claim explanation; a regulatory source's one-sentence explanation is build-level and is
recorded once in the metadata as `source_explanations`).
The label's context ships as `disease_context_qualifier` (**a MONDO term only** — the
slot's range is `disease`, and a UMLS or HP id passes the pydantic constructor while free
text passes the `str` typing, so neither guard catches a value that is not one; every
grounded id whatever its prefix stays on `medic_disease_context_ids` and the label's own
wording on `medic_disease_context`, so narrowing the slot loses nothing),
`population_context_qualifier`, `temporal_context_qualifier` and `sex_qualifier` (a PATO
term, the slot's range being `biological sex`). The full transformation trail is not
embedded — edges carry the `MEDICNE:` mention ids, which join to the shipped
`mappings/*.sssom.tsv` and Babelon stores, so I-8 replayability holds at the graph level
without JSON blobs in edge properties.

**The extension layer is the residue, not the inventory.** It is kept to: the source /
jurisdiction pair the I-1 gate checks; the raw approval status and date; the assertion
quality flags and the truncation marker; the two mention-id join keys; the three pair
aggregates a `GROUP BY` cannot rebuild (noisy-OR confidence, assertion count, tier); claim
context Biolink has no qualifier for (further disease-context ids, co-administered drugs, the
label's finer relation); and the allergen / diagnostic-agent markers on contraindications.
Everything else that used to be namespaced was derivable from a Biolink slot on the same
record, constant across the build, or replayable from the products through the join keys.

**Validation.** `just export-kgx` checks the graph it just built against the *installed*
Biolink model and exits non-zero on any error. Every record is re-validated through the
pydantic class its `category` names — the same class that built it — so an edited or
older file is held to the same rules: undeclared property, impermissible enum value, list
where the class wants a scalar, missing required slot. On top of that: a predicate the
association class does not admit, a `medic_*` name shadowing a real slot, a dangling edge
endpoint, an off-class enum value, and a source-isolation violation (I-1, echoed at the
export boundary).
Unusual-but-legitimate findings (a `DRON:` prefix Biolink does not list, an edge that could
not be attributed to a primary source) are warnings with counts.

Output: `exports/medic_nodes.jsonl`, `exports/medic_edges.jsonl`,
`exports/medic_kgx_metadata.yaml` (counts by category/predicate/source/reliability, the
extraction agents),
`exports/infores_medic.yaml` (proposed Translator registry entry).

### 11.3 SSSOM export (drug mappings)

**Module:** `src/medic/export/sssom.py`
**Target:** `just export-sssom`

Generates drug identifier cross-reference mappings in SSSOM (Simple Standard for Sharing Ontological Mappings) format:

- One row per cross-reference: primary CURIE to each alternate ID
- Predicate: `skos:exactMatch`
- Mapping justification: `semapv:LexicalMatching`
- Includes DrugBank mappings where available

Output: `exports/medic_drug_mappings.sssom.tsv`

---

## 12. What Changed from v1.0.0

### Architecture

| Aspect | v1.0.0 | v2.0 (redesign) |
|--------|--------|-----------------|
| **Framework** | Kedro pipeline with pre-processed intermediate files | Self-contained ETL modules, no framework dependency |
| **Source data** | Read from `/medi/` intermediate directory | Each module downloads or reads raw source directly |
| **Grounding** | NameRes only, synthetic rank-based confidence | Cascade of 4 backends (OAK, Gilda, NameRes, OLS) with real Jaro-Winkler confidence |
| **Unresolved drugs** | Silently dropped | Persisted with `grounding_status: unresolved`, tracked in grounding reports |
| **Drug classification** | All tags via LLM (10 LLM calls per drug) | ATC-derived deterministic tags + LLM only for 2 tags (0-1 calls per drug) |
| **Caching** | None | Persistent disk-based JSON caches for all expensive operations |
| **Reproducibility** | Results varied with LLM responses | Deterministic ATC tags + caches = reproducible runs |
| **Cost** | ~$50-100 per full run (heavy LLM use) | ~$5-10 per full run (LLM only for preprocessing, reranking, 2 tags) |
| **Scope** | Drug list only | Full knowledge base: drugs, diseases, indications, contraindications, adverse events, research |

### Compatibility with v1.0.0

Based on the comparison report (`docs/v1_comparison_report.md`):

| Metric | Value |
|--------|-------|
| v1.0.0 total drugs | 3,883 |
| v2.0 total drugs | 5,089 (+1,206) |
| Shared (same CURIE) | 3,220 (82.9% overlap) |
| Only in v1.0.0 | 663 |
| Only in v2.0 | 1,869 |
| Column schema match | 31/31 columns identical |

**High-match fields (>= 90%):** `is_allergen`, `is_vaccine_or_antigen`, `is_no_therapeutic_value`, `approved_india`, `approved_japan`, `approved_russia`, `is_radioisotope_or_diagnostic_agent`, `curie_label`, `is_glucose_regulator`, `is_steroid`, `is_metallic_salt`, `is_chemotherapy`, `approved_europe`, `is_combination_therapy`, `is_cancer_drug`.

**Known differences:**
- `alternate_ids` format changed from Python list repr (`['CHEBI:123']`) to pipe-separated (`CHEBI:123| DRUGBANK:456`)
- `marketing_status_usa` now picks the most permissive status instead of storing all pipe-separated
- ATC code coverage differs due to using ChEMBL as primary source (vs. the old pipeline's sources)
- SMILES coverage differs (27.4% match) due to different source databases

**Drugs lost from v1.0.0:** Primarily drugs where the old pipeline grounded salt forms (e.g., "THIOPENTAL SODIUM") to different CURIEs than the new pipeline's active-moiety extraction.

**Drugs gained in v2.0:** New drugs found from broader grounding coverage and the addition of the China source.

### Key improvements

- **Reproducibility:** Persistent caches mean re-running the pipeline produces identical results (absent upstream API changes)
- **Transparency:** Grounding reports per source show exactly which drugs were auto-accepted, flagged for review, or unresolved
- **Extensibility:** Adding a new grounding backend requires implementing the `GroundingService` interface and registering it in the factory
- **Cost control:** `MEDIC_SKIP_EXPENSIVE_CALLS=1` bypasses all LLM and rate-limited API calls for fast iteration

---

## 13. Running the Pipeline

### Prerequisites

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) (Python package manager)
- [just](https://github.com/casey/just) (command runner)

### Quick start

```bash
# Install dependencies
just setup

# Build the full drug list (all sources + merge + enrichment)
just build-drug-list

# Build the disease list
just build-disease-list

# Build indication/contraindication lists
just build-on-label-list

# Build adverse event list
just build-adverse-event-list

# Compile research associations
just build-research

# Build everything (all products + exports + validation)
just build-all

# Export to CSV/XLSX/KGX/SSSOM
just export-legacy
just export-kgx
just export-sssom

# Run all validations
just validate-all
```

### Grounding backend selection

Each ingest accepts a grounding backend parameter (default: `nameres`):

```bash
just ingest-orangebook cascade    # Full cascade (OAK -> Gilda -> NameRes -> OLS)
just ingest-orangebook oak        # OAK only (offline, fast)
just ingest-orangebook nameres    # NameRes only (default, matches v1.0.0 behavior)
```

### Environment variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `MEDIC_SKIP_EXPENSIVE_CALLS` | Set to `1` to bypass LLM and rate-limited API calls | unset (all calls enabled) |
| `ANTHROPIC_API_KEY` | Required for LLM preprocessing, reranking, and drug tagging | -- |
| `MEDIC_CACHE_DIR` | Override cache directory | `cache/` |

### Build targets reference

| Target | Description |
|--------|-------------|
| `build-drug-list` | Run all drug ingests + merge + enrichment |
| `build-disease-list` | Ingest + validate disease list |
| `build-on-label-list` | Ingest DailyMed + EMA + PMDA, then merge indication associations |
| `build-adverse-event-list` | Ingest PVLens + FAERS, then merge adverse events |
| `build-research` | Compile research pipeline output |
| `build-all` | Build all products + export + validate |
| `export-legacy` | Generate CSV/XLSX files matching v1.0.0 format |
| `export-kgx` | Generate Biolink KGX export |
| `export-sssom` | Generate SSSOM mappings |
| `validate-all` | Run schema validation on all product files |
| `research-curate` | Interactive research curation for a disease |
| `research-batch` | Batch research for multiple diseases |
| `research-disease` | Deep research on a disease via external provider |

### Cost control

Setting `MEDIC_SKIP_EXPENSIVE_CALLS=1` bypasses all operations that incur API costs or rate limits:

| Operation | Normal behavior | Skip mode behavior |
|-----------|----------------|-------------------|
| LLM drug name preprocessing | Extract active moiety, translate | Pass raw name through |
| LLM candidate reranking | Pick best from ambiguous candidates | Accept highest-scored cascade result |
| LLM drug classification | Classify `is_no_therapeutic_value`, `is_metallic_salt` | Set both to `False` |
| LLM combination detection | Detect coformulated drugs | Set `is_combination_therapy` to `False` |
| ATC + SMILES lookup | Query ChEMBL, PubChem, etc. | Leave fields empty |
| PHAROS cross-references | Query GraphQL API | Skip entirely |

Skip mode is intended for fast iteration and testing. Non-English sources (PMDA, Russia, China) will have poor grounding quality in skip mode because the LLM translation step is bypassed.

### Caching strategy

All expensive operations cache their results to disk as JSON files under `cache/`. Caches are keyed by input (drug name, CURIE, etc.) and persist across pipeline runs:

- **First run:** All API calls are made; results are cached
- **Subsequent runs:** Cached results are used; only new/changed inputs trigger API calls
- **Cache invalidation:** Delete the relevant JSON file to force re-computation

---

## Appendix A: Evidence Model

The shared evidence model (`src/medic/schema/evidence.yaml`) is used across indication, adverse event, and research associations. Each `EvidenceItem` captures:

| Field | Description |
|-------|-------------|
| `source_type` | REGULATORY, LITERATURE, GUIDELINE, DATABASE, or POST_MARKET |
| `jurisdiction` | USA, EU, JAPAN, INDIA, RUSSIA, CHINA, or INTERNATIONAL |
| `reference` | Identifier: `PMID:12345`, `DailyMed:xxx`, `NCT:xxx`, `FAERS:xxx` |
| `reference_title` | Title of the referenced document |
| `snippet` | Exact quote, validated against PubMed abstract for literature sources |
| `explanation` | How the evidence supports or refutes the drug-disease association |
| `curator` | Agent object tracking who/what produced this evidence (see Agent class) |
| `support` | SUPPORT, REFUTE, PARTIAL, or NO_EVIDENCE |
| `confidence` | HIGH, MEDIUM, or LOW |
| `evidence_source` | HUMAN_CLINICAL, MODEL_ORGANISM, IN_VITRO, COMPUTATIONAL, or OTHER |
| `approval_status` | APPROVED, INVESTIGATIONAL, WITHDRAWN, DISCONTINUED, or OFF_LABEL |
| `max_research_phase` | PRE_CLINICAL through PHASE_IV, CASE_REPORT, IN_VITRO, or COMPUTATIONAL |
| `study_status` | Clinical trial status: COMPLETED, ACTIVE_NOT_RECRUITING, RECRUITING, NOT_YET_RECRUITING, TERMINATED, WITHDRAWN, SUSPENDED, or UNKNOWN (`StudyStatusEnum`) |

### Custom types and LinkML annotations

- **`PMID`** custom type -- used as the range for the `reference` field when the source is literature
- **`reference`** field has `implements: linkml:authoritative_reference`
- **`snippet`** field has `implements: linkml:excerpt`

### Agent class (curator provenance)

Each `EvidenceItem` carries an inlined `curator` field (range: `Agent`) tracking who or what produced the evidence:

| Field | Description |
|-------|-------------|
| `curator_id` | GitHub commit URL identifying the pipeline run or human action |
| `curator_type` | `CuratorTypeEnum`: AI_AGENT, HUMAN, or PIPELINE |
| `name` | Human-readable description of the curator |

### StudyStatusEnum

Tracks clinical trial status for evidence items sourced from clinical trials:

COMPLETED, ACTIVE_NOT_RECRUITING, RECRUITING, NOT_YET_RECRUITING, TERMINATED, WITHDRAWN, SUSPENDED, UNKNOWN

### IndicationSourceNameEnum

Enumerates data sources for indication associations. Includes CLINICAL_TRIAL and CUREID values in addition to the standard regulatory sources (DAILYMED, EMA, PMDA, etc.).


---

## Appendix B: Module Map

```
src/medic/
├── grounding/                     # Entity grounding subsystem
│   ├── base.py                    # GroundingService ABC, GroundingResult dataclass
│   ├── factory.py                 # get_grounding_service(name) factory
│   ├── cascade.py                 # CascadeGrounding orchestrator
│   ├── confidence.py              # Jaro-Winkler scoring, ConfidenceTier enum
│   ├── preprocessor.py            # LLM drug name preprocessor
│   ├── reranker.py                # LLM candidate reranker
│   ├── cache.py                   # Persistent disk-based grounding cache
│   ├── oak_backend.py             # OAK (ChEBI SQLite) backend
│   ├── gilda_backend.py           # Gilda fuzzy matching backend
│   ├── nameres_backend.py         # SRI NameRes backend
│   └── ols_backend.py             # EBI OLS backend
├── ingest/                        # Per-source ingest modules
│   ├── common.py                  # Shared utilities (download, date, YAML I/O)
│   ├── grounding.py               # Shared grounding pipeline (all sources use this)
│   ├── orangebook/__main__.py     # FDA Orange Book
│   ├── purplebook/__main__.py     # FDA Purple Book
│   ├── ema/__main__.py            # EMA
│   ├── pmda/__main__.py           # PMDA (Japan)
│   ├── russia/__main__.py         # GRLS (Russia)
│   ├── india/__main__.py          # CDSCO (India)
│   ├── china/__main__.py          # CDE (China)
│   ├── everycure_drugs/__main__.py  # EveryCure curated drug list
│   ├── dailymed/__main__.py       # DailyMed indication ingest
│   ├── on_label_ingest.py         # Indication ingest from existing intermediates
│   ├── disease_list/__main__.py   # Disease list ingest
│   ├── pvlens/__main__.py         # PVLens adverse events
│   └── faers/__main__.py          # FAERS adverse events
├── merge/
│   ├── drug_merge.py              # Drug merge + enrichment orchestration
│   ├── on_label_merge.py          # Indication merge (indications + contraindications)
│   └── adverse_event_merge.py     # Adverse event merge
├── enrichment/                    # Post-merge enrichment steps
│   ├── atc_smiles.py              # ATC codes + SMILES via ChEMBL
│   ├── drug_tags.py               # ATC-derived + LLM classification tags
│   ├── combination.py             # Combination therapy detection
│   ├── pharos.py                  # PHAROS cross-references
│   ├── rxnorm_extension.py        # RxNorm Extension mapping (planned)
│   └── cache.py                   # Shared enrichment cache utility
├── research/                      # Research pipeline
│   ├── compile.py                 # Compile research YAML into product
│   ├── curate.py                  # Interactive curation workflow
│   └── batch.py                   # Batch research processing
├── export/                        # Output format generators
│   ├── legacy.py                  # CSV/XLSX matching v1.0.0 format
│   ├── kgx.py                     # Biolink KGX export
│   └── sssom.py                   # SSSOM drug mappings
├── schema/                        # LinkML schema definitions
│   ├── medic.yaml                 # Master schema (imports all sub-schemas)
│   ├── drug.yaml                  # Drug and DrugList classes
│   ├── drug_source.yaml           # Per-source drug records
│   ├── disease.yaml               # Disease and DiseaseList classes
│   ├── indication.yaml             # IndicationAssociation and IndicationList
│   ├── indication_source.yaml     # Per-source indication records
│   ├── adverse_event.yaml         # AdverseEventAssociation and AdverseEventList
│   ├── adverse_event_source.yaml  # Per-source adverse event records
│   ├── evidence.yaml              # Shared EvidenceItem model
│   └── research_source.yaml       # ResearchAssociation schema
├── validate/                      # Schema and term validation
└── pipeline.py                    # Pipeline orchestration utilities
```

### Intermediate and output directory structure

```
kb/                                # Intermediate knowledge base files
├── drugs/
│   ├── orangebook/                # Per-source drug records + grounding reports
│   ├── purplebook/
│   ├── ema/
│   ├── pmda/
│   ├── russia/
│   ├── india/
│   ├── china/
│   └── everycure/
├── diseases/
│   └── disease_list.yaml          # Disease list product
├── indications/
│   ├── dailymed/                  # Per-source indication records
│   ├── ema/
│   └── pmda/
├── adverse_events/
│   ├── pvlens/
│   └── faers/
└── research/                      # Per-disease research YAML files

products/                          # Final merged products
├── drug_list.yaml
├── indication_list.yaml
├── contraindication_list.yaml
├── adverse_event_list.yaml
├── research_list.yaml

exports/                           # Export format outputs
├── drug_list_flexible.csv
├── drug_list_stringent.csv
├── orangebook.xlsx
├── purplebook.xlsx
├── ema.xlsx
├── pmda.xlsx
├── russia.csv
├── india.csv
├── medic_nodes.jsonl
├── medic_edges.jsonl
└── medic_drug_mappings.sssom.tsv

cache/                             # Persistent caches
├── grounding/
│   ├── orangebook.json
│   ├── purplebook.json
│   ├── preprocessor.json
│   └── reranker.json
├── enrichment/
│   ├── atc_smiles.json
│   ├── drug_tags_llm.json
│   ├── combination.json
│   └── pharos.json
└── research/                      # Per-disease research caches
```

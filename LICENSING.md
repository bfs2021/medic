# Licensing

MeDIC is not one work under one licence. It is code that builds a derived database out of a dozen
upstream sources whose terms differ, and several of those terms are incompatible with each other.
Applying a single permissive licence to the whole repository would misrepresent what a downstream
user is actually allowed to do — most importantly, it would tell them attribution is optional when
for EMA and PMDA it is mandatory.

So the repository is licensed in layers.

## What MeDIC licenses

| Layer | Covers | Licence |
|---|---|---|
| **Software** | `src/`, `tests/`, `scripts/`, `justfile`, `project.justfile`, LinkML schemas in `src/medic/schema/` | [BSD-3-Clause](LICENSE) |
| **Documentation** | `docs/`, `README.md`, `SPEC.md`, this file | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) |
| **MeDIC curation** | The mapping *assertions* in `mappings/*.sssom.tsv`, and hand-curated content in `kb/` | Offered as [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/); the **files** are declared [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) — see below |
| **Derived data products** | `products/`, `exports/`, GitHub release assets | **No MeDIC licence grant** — see below |

Two caveats on the CC0 layer:

- CC0 covers MeDIC's *contribution* — the decision that string X grounds to CURIE Y, at a given
  confidence, by a given method. It does not cover the `subject_label` and `match_string` columns,
  which reproduce verbatim strings from the sources and remain under the source terms in the table
  below.
- A waiver only works on rights the waiving party holds. MeDIC cannot and does not purport to place
  upstream source content in the public domain.

**Why the SSSOM files declare CC BY 4.0 and not CC0.** A file mixes both layers: MeDIC's assertions
*and* the verbatim source strings in `subject_label` / `match_string`. `license` is the one field a
machine reads, and CC0 in it says "attribution optional" — which is false for a file containing EMA
and PMDA strings, and is exactly the misreading this document warns against at the end. So the set
is declared CC BY 4.0, and the `#comment:` line records that MeDIC's own mapping decisions are still
offered as CC0. The change is about stating the obligation that genuinely attaches to the file, not
about MeDIC wanting credit for the mappings.

## What MeDIC cannot license

The derived data products are built from regulatory sources MeDIC does not own. MeDIC grants no
rights over them and passes the upstream terms through unchanged. If you redistribute a MeDIC data
product, you take on the obligations of every source that contributed to it.

Stated as a passthrough, which is the sentence that travels with every release asset:

> MeDIC grants no rights over upstream source content. The original licence of each contributing
> source remains in force and must be respected; parsing, translation, normalisation and mapping to
> ontology identifiers do not alter it. Where terms conflict, the stricter governs.

In practice this means **attribution is required**, because EMA and PMDA both require it and both
feed the merged on-label product. Ship this notice with any redistribution:

> Contains data from the European Medicines Agency (© EMA), the Pharmaceuticals and Medical Devices
> Agency of Japan (PMDA, <https://www.pmda.go.jp>), the U.S. Food and Drug Administration, and the
> U.S. National Library of Medicine. Data has been edited: source records were parsed, translated,
> normalised, and mapped to ontology identifiers by the MeDIC pipeline. Neither EMA, PMDA, FDA nor
> NLM endorses this derived work.

The "data has been edited" clause is not boilerplate — Japan's Public Data License 1.0 specifically
requires that modified content say so and not be presented as originating from the public body.

## Per-source terms

| Source | Jurisdiction | Upstream terms | Attribution | MeDIC redistributes? |
|---|---|---|---|---|
| [DailyMed](docs/sources/dailymed.md) (NIH/NLM SPLs) | USA | Public domain in practice | Courtesy only | Yes — derived only |
| [Orange Book](docs/sources/orangebook.md) | USA | US Government work, public domain (17 U.S.C. §105) | Courtesy only | Yes |
| [Purple Book](docs/sources/purplebook.md) | USA | US Government work, public domain | Courtesy only | Yes |
| [FAERS](docs/sources/faers.md) | USA | US Government work, public domain — **but carries MedDRA terms** | Courtesy only | Not by default (see MedDRA) |
| [CURE-ID](docs/sources/cureid.md) (FDA/NCATS) | USA | NIH open data, public domain | Courtesy only | Yes |
| [EMA](docs/sources/ema.md) | EU | © EMA. Reproduction permitted, commercial and non-commercial, **provided EMA is acknowledged as the source in each copy**. EU database right also applies. | **Required** | Yes, with attribution |
| [PMDA](docs/sources/pmda.md) | Japan | Japan [Public Data License 1.0](https://www.pmda.go.jp/english/0013.html). Source citation required; edited content must be marked as edited. | **Required** | Yes, with attribution + edit notice |
| [India CDSCO](docs/sources/india.md) | India | No licence declared on cdsco.gov.in. Indian government open data is normally published under GODL-India, which requires attribution. | **Treated as required** | Derived records, with attribution |
| [Russia GRLS](docs/sources/russia.md) | Russia | State register of medicinal products. No licence declared. | Courtesy | Derived records only — **never the source archive**. A derived record carries the drug name, its English translation, the registration number and date, and MeDIC's own grounding. |
| [China CDE/NMPA](docs/sources/china.md) | China | Register of approved products. No licence declared. | Courtesy | Derived records only — **never the source archive**. A derived record carries the drug name, its English translation, the approval date, and MeDIC's own grounding. |
| [EveryCure drug-list](docs/sources/everycure.md) | — | CC BY 4.0 (HuggingFace `everycure/drug-list`) | **Required** | Yes, with attribution |
| [EveryCure disease-list](docs/sources/disease_list.md) | — | CC BY 4.0 (HuggingFace `everycure/disease-list`) | **Required** | Yes, with attribution |
| [PVLens](docs/sources/pvlens.md) | USA | Software is GPL-3.0; MeDIC consumes its CSV *output*, not its code, so no copyleft attaches. Output carries MedDRA terms. | — | Not by default (see MedDRA) |

## Reference vocabularies used for grounding

| Vocabulary | Licence | Consequence for MeDIC |
|---|---|---|
| Mondo | CC BY 4.0 | Attribution; no restriction on use of identifiers |
| ChEBI | CC BY 4.0 | Attribution; no restriction on use of identifiers |
| PubChem | Public domain | None |
| ChEMBL | CC BY-SA 3.0 | Used in `enrichment/atc_smiles.py`. Identifiers and ATC codes are facts; a bulk redistribution of ChEMBL-derived *fields* could attract share-alike. Keep ChEMBL-derived enrichment separable. |
| DrugCentral | CC BY-SA 4.0 (verify) | Same caution as ChEMBL |
| RxNorm | UMLS Metathesaurus Licence. RxNorm-original content (RXCUIs and RxNorm normalized names) is unrestricted — UMLS Appendix 1 **Category 0**; some vocabularies that *contribute to* RxNorm are not. | **Build input and published label source.** 18,279 ingredient concepts are **committed** as `data/vocab/rxnorm_ingredients.tsv` (OMOP concept id, RXCUI, label) and read into the drug grounding index (`conf/grounding_sources.yaml`, loader `grounding/lexical/loaders/omop_rxnorm.py`); RxNorm ingredient names may supply a published `object_label`. Committing the table rather than reading a dump is deliberate — the index must be reproducible from a checkout. `scripts/build_rxnorm_table.py` regenerates it from an OMOP/ATHENA download and filters on `vocabulary_id == "RxNorm"` at extraction time, so the bundle's SNOMED, MedDRA and other licence-gated content never enters the committed artifact — the same fail-closed reasoning as the UMLS label allowlist (I-14). Only `Ingredient` and `Precise Ingredient` classes are included; clinical-drug and pack concepts denote dose forms, not substances. The ATHENA download itself is not committed and is needed only to refresh the table. |
| **MedDRA** | ICH/MSSO **subscription licence**. Redistribution of dictionary term text is prohibited. | **Never redistributed.** MedDRA term text is not published in any MeDIC artefact. This is not a question of volume or degree: a source whose terms prohibit redistribution is matched against internally or not used at all. Enforced in two places. (1) The disease grounding index reaches MedDRA through UMLS, which bundles 113,364 `MDR` atoms; every `MDR*` variant is excluded from `LABEL_SAB_DECISIONS` in `grounding/lexical/loaders/umls.py`, so MedDRA can be matched against but can never supply a published label, and a concept known *only* to MedDRA ships **unnamed** rather than borrowing a name it may not carry (invariant I-14). (2) There is no adverse-event product. MeDIC ships 1,443 MedDRA *identifiers* inherited as Mondo crossreferences — bare identifiers, no term text — and nothing else. |
| **UMLS Metathesaurus** | UMLS Metathesaurus Licence. The Metathesaurus **bundles MedDRA (`MDR`) and SNOMED CT (`SNOMEDCT_US`)**; individual source vocabularies keep their own terms. | Used to build the disease grounding index (`conf/grounding_sources.yaml` → `background/umls-2021AA-mrconso.zip`). A licence-gated **build input**: not committed, not redistributable, and required to reproduce the index. Matching against it is internal lookup; publishing a restricted vocabulary's term text is not (I-14). |
| **SNOMED CT** | **Global Patient Set** (<https://www.snomed.org/gps>), reached through UMLS. | Cleared for publication under the GPS; SNOMED term text may supply a published label. |
| **WHO ICD-10** (`ICD10`) | WHO copyright, reached through UMLS. **UMLS Appendix 1 Category 3 — publication expressly excluded.** | **Never redistributed.** Matched against internally; excluded from `LABEL_SAB_DECISIONS`, so it cannot supply a published label. A different vocabulary from `ICD10CM` below — the SAB names differ by three characters, the terms differ entirely. |
| ICD-10-CM (`ICD10CM`) | US federal work (CDC/NCHS), published free of charge — **public domain at source**. | Published as a label on the basis of the vocabulary's own federal public-domain status. Supplies ~37 published labels. |
| OMIM | UMLS Appendix 1 **Category 0** — no additional restrictions. | Cleared for publication; supplies ~33 published labels. |
| **WHO ATC/DDD** | WHOCC copyright; the bulk index requires a paid licence. | Scraping whocc.no was deliberately removed (`docs/architecture.md`). ATC codes now come from ChEMBL/PubChem/DrugCentral/RxNorm/ChEBI, where they appear as facts. Do not reintroduce a whocc.no fetch. |

## What is published, and how that is decided

Release assets are enumerated in [`conf/release_assets.yaml`](conf/release_assets.yaml), one entry
per file, each declaring the sources it draws on and whether it may ship. A file in `products/` or
`exports/` with **no entry is refused**, so a new build output cannot become a release asset by
default — `just gh-release` fails until someone decides. Inspect the current plan with
`just release-assets`.

This replaced a glob over `exports/*.{csv,xlsx,jsonl,tsv}`. The glob is how the KGX export silently
became 57 MB of new release assets — including 4.6 MB of verbatim EMA/PMDA/DailyMed label text —
with no licensing decision taken.

The attribution notice is **generated from the manifest**, naming exactly the attribution-required
sources whose data is actually in the release, and is embedded in the outputs rather than only
beside them:

| Output | Where the notice appears |
|---|---|
| **Every release asset** | **`exports/NOTICE.md`** — attribution, the passthrough, and a per-asset table of which sources each file contains |
| KGX | `medic_kgx_metadata.yaml` → `license.attribution_notice` |
| SSSOM (export and `mappings/` stores) | `#comment:` in the mapping-set header, alongside the CC0 carve-out |
| Release | The release body, plus `LICENSING.md` and `NOTICE.md` as assets |

`exports/NOTICE.md` is the general instrument: it is generated from the manifest, covers the whole
release, and is regenerated by `just gh-release` before the assets are resolved, so it always
describes the release it ships in. The per-format notices are convenience copies for the two formats
that can carry one — most of what MeDIC ships is CSV, XLSX and JSONL, none of which can.

The two reliability TSVs carry **no in-band header** on purpose: `docs/reliability.md` documents
reading them with `csv.DictReader`, which a leading `#` comment block would break. `NOTICE.md`
covers them.

### How much verbatim source text actually ships

Measured on the current build, across `products/indication_list.yaml` and
`products/contraindication_list.yaml`.

| Source | Quoted spans | Longest span | Verbatim text | Attribution |
|---|---:|---:|---:|---|
| DailyMed | 14,278 | 14,474 | 13.5 MB | courtesy |
| EMA | 3,054 | 8,249 | 3.1 MB | **required** |
| PMDA | 2,620 | 988 | 0.6 MB | **required** |
| CDSCO | 132 | 783 | 0.03 MB | **treated as required** |

Three fields carry source text, and they are capped differently — a single figure would misdescribe
the file:

- `evidence.snippet` is capped at 500 characters (PMDA 988).
- `span.text` and each resolution step's `input_value` are **not** capped. They carry the section the
  claim was read from, which is what makes the extraction auditable against the source.
- In the KGX export, `supporting_text` is capped at 2,000 characters, and any edge that hit the cap
  carries `medic_supporting_text_truncated` so a consumer can tell.

The per-asset breakdown, including which sources contribute to which file, is generated into
`exports/NOTICE.md` for every release.

## Sources that prohibit redistribution

Some upstream terms prohibit redistribution of the content outright. For those, MeDIC's position is
categorical rather than proportionate: **the content is matched against internally, or it is not used
at all. It is never published, in any artefact, at any volume.**

This is enforced in code, not by convention. `LABEL_SAB_DECISIONS` in
`src/medic/grounding/lexical/loaders/umls.py` is an **allowlist**: a vocabulary may supply a published
label only if a decision permitting it is recorded beside it, so a vocabulary nobody has assessed
fails closed. `LABEL_SAB_REFUSED` records the refusals and why. A concept known only to a refused
vocabulary ships **unnamed** — an empty label is honest, the identifier still resolves, and the
mapping still works. `tests/test_restricted_vocabulary_labels.py` asserts the boundary against the
committed artefacts, globbing `mappings/` so a new store cannot be added without a licence decision.

Currently refused: **MedDRA** (all 23 `MDR*` language variants) and **WHO ICD-10**. There is no
adverse-event product, and `kb/adverse_events/` is untracked and gitignored so an ingest cannot
create one by accident.

## Other recorded positions

- **`exports/russia.csv` and `exports/india.csv` ship** as explicit manifest entries rather than as a
  side effect of a glob. `russia.csv` holds 5,885 rows of DeepL-translated English drug name, ChEBI
  id and approval date. `india.csv` carries the verbatim formulation string plus a short indication
  phrase on 101 of its 112 rows.
- **Russia and China derived records are published; the source archives are not.** A derived record
  carries the drug name and its English translation, the registration or approval identifier and
  date, and MeDIC's own grounding. The source archives are never redistributed —
  `sources/manual-sources.zip` is not in the repository and its download URL is deliberately not
  committed. The verbatim source string is retained on the record because invariants I-7 and I-8
  make it the anchor of the transformation chain: every published identifier walks back to the
  string a regulator published, and that is the claim this release is built on.
- **All five files in `mappings/` declare their terms.** The four SSSOM stores carry a `# license:`
  header emitted by their own writer. The Babelon translation table carries a sidecar,
  `mappings/drug_translation.babelon.meta.yaml`, because `babelon.utils.parse_babelon` is a bare
  `pd.read_csv(sep="\t")` with no comment handling and a `#` front-matter line makes the table
  unparseable. The sidecar records what only that file has to record: `source_value` is verbatim
  source-language register content, `translation_value` is MeDIC's contribution, and the CC BY 4.0
  over `mappings/` is a claim about the latter.
- **DrugCentral's licence is recorded from secondary sources** and has not been checked against the
  DrugCentral site.
- **`.github/workflows/release.yml`** creates the draft release and ships this file. Data assets are
  attached from a local build with `just gh-release`, because `products/` and `exports/` are
  gitignored build outputs that a runner cannot reproduce — the manual source archives and API keys
  are not available to it.

## Why not CC0 for everything

Because it would be both ineffective and misleading. Ineffective, because a rights waiver cannot
reach rights MeDIC never held — the EMA and PMDA material would stay exactly as encumbered as it
was. Misleading, because a downstream user would reasonably read CC0 as "no attribution needed",
redistribute the merged product without acknowledging EMA, and breach EMA's terms on MeDIC's word.

A blanket CC0 would also flatten a real distinction. The individual facts — that a drug was approved
for an indication on a date — are treated differently across jurisdictions from the databases that
collect them, and a single waiver cannot speak for both. Declaring the layers separately is what lets
a downstream user see which obligations actually travel with the part they are using.

# Worklog — Pareto push against v1.0.0

Chronological record of this stretch of work. `deferred.md` is the thematic ledger (what is
owed, what was declined); this is what happened, in order, with the numbers.

Goal, as stated: **be at least near-Pareto against the old MeDIC** — better on quality
*without* being worse on coverage.

---

## Where we started (2026-10-17)

The audit of 300 random indication edges, judged blind against independently fetched
FDA/EMA text, scored v1 and the then-current build almost identically overall but with
*different* error profiles:

| | v1 (medic-ingest) | v2 @47dd0b7 |
|---|---:|---:|
| error rate | 25.8% | 25.6% |
| `FP1_setting` (population as target) | 14 | **28** |
| `FP6_coingredient` | **0** | **15** |
| `FP5_negation` | 18 | **2** |
| `FP7_notintext` | 26 | **17** |

So v2 had fixed negation and not-in-text, and introduced co-ingredient errors from zero
while doubling setting errors. Not Pareto in either direction.

## Quality: done, and measured

Of the 76 audited errors, **54 are gone and 7 remain at HIGH**:

| error class | gone | LOW | MED | HIGH |
|---|---:|---:|---:|---:|
| FP1_setting | 25 | 0 | 0 | 3 |
| FP7_notintext | 15 | 1 | 0 | 1 |
| FP6_coingredient | 4 | 10 | 0 | 1 |
| FP2_symptom_swap | 9 | 0 | 0 | 2 |

Audited-correct edges: 137 HIGH, 13 MEDIUM, 21 LOW, 50 gone.

**Error rate at HIGH: 4.9%**, against v1's 25.8% overall and v2's 26.7% at HIGH. A consumer
filtering on HIGH now gets something usable, which was not previously true.

## Coverage: the three fronts, and what each turned out to be

### 1. DailyMed labels — one per drug

`acquire._most_recent_setid` fetched a page of 20 setids per drug, sorted by date, returned
**one**, discarded 19. Then a second, larger bug: `_usa_drug_names` took the first
`source_ingredients` string and `break`ed, and the drug list holds *product* names
("Lansoprazole Enteric-Coated Capsules") that DailyMed's `drug_name` index does not match
while `curie_label` ("lansoprazole") does. 1,074 of 2,591 names resolved to no SPL at all.

Evidence it was never *requested* rather than filtered: after acquiring 10,835 labels,
exactly **2** carried LANSOPRAZOLE as a moiety and 1 of its 20 setids was on disk.

Fixed: every candidate name queried, generic first. **1,976 → 18,261 SPL files.**

### 2. Drug CURIEs — mostly an id-join artifact

`compare-release` reports 41.2% curie overlap, which I quoted repeatedly as a 59% coverage
loss. It is not one. Measured properly (`just v1-drug-gap`):

| | |
|---|---:|
| v1 curies | 3,883 |
| overlap by id | 1,598 (41.2%) |
| same drug, different id | 801 |
| genuinely absent | **1,484** |
| **effective coverage** | **2,399 (61.8%)** |

And no drug is dropped for want of an identifier — all 4,323 in `drug_list.yaml` have
resolved ids. The genuinely-absent 1,484 break down by v1's own approval flags as
**436 india, 414 usa, 248 russia, 58 japan**: regional, not chemical. The absent names bear
it out — Saroglitazar, Trelagliptin, Imidafenacin, Dexrabeprazole, Garenoxacin.

Two fixes:

- **UNII fallback.** A moiety the lexical cascade cannot place now uses the UNII the SPL
  itself asserts. Whole-label losses went **19 → 0** on a 1,406-label sample; 26 distinct
  moieties rescued. Ships at MEDIUM via the existing `source_asserted` path, so it is honest
  about being unverified.
- **RxNorm in the drug index.** 18,279 ingredient concepts from the OMOP/ATHENA download,
  ranked after CHEBI/DRON. 276 of the 1,484 absent v1 drugs resolve on an exact ingredient
  name. Done as an indexed vocabulary so matches get real provenance, not a side channel.

### 3. India — 112 drugs against v1's 3,118

The switch to CDSCO primary PDFs was right; the reader was not. A multi-page table repeats
its header only on page one, and a table with no recognised header was skipped whole — so
only page one of each PDF was ever read. **112 → 1,048 clean records.**

`india_2014.pdf` still yields zero, correctly: it is a BA/BE reference-product list with no
Indication column.

---

## Mistakes worth keeping, because each cost real time

1. **I violated I-8.** My first fix for `TENOFOVIR ANHYDROUS` stripped words in
   `_ground_drug` and re-asked. It fixed nothing the grounder was not already doing
   (`salt_ester_strip` already strips "anhydrous") and returned an id the cascade had
   *refused* for ambiguity, recorded under a mention minted from the stripped string — so the
   chain from the label's literal was not replayable. Reverted (`8204e0f`).
2. **I nearly shipped 2,133 India fragments.** Adding pdfplumber's `text` strategy produced
   19× more "records" that were shredded — `'&Letermovir Ta'`, `'240mg and 480'`, dates gone.
   Selecting a strategy by record count *prefers* the one that shreds. An existing test
   caught it on the dates; my own spot check had not, because I printed cells truncated to 60
   characters and read the clipping as my formatting.
3. **I recommended a useless grounding fix.** "Accept unambiguous `relatedSynonym`" was based
   on reading `match_field` out of the index instead of calling the matcher. Of 418 sampled
   moieties only 23 fail and **20 are absent from the index entirely**, so the policy change
   recovers zero. HYDROCORTISONE looked like a `relatedSynonym` failure and grounds fine.
4. **Two misdiagnoses of "the prompt drops the target".** thiamine's bad edges are #88 fan-out
   from a 15-ingredient multivitamin (the gate caps all 11 at LOW); deferasirox's indication
   is correct at HIGH but grounds to a label-withheld concept, so it ships unnamed. After both
   corrections that hypothesis has **no** confirmed instances.
5. **I read a shared cache file and drew a wrong conclusion.**
   `cache/enrichment/dailymed_setids.json` is written by *two* modules, and `setid_lookup`
   rewrites it in the old shape during ingest. Inspecting it after a run looks exactly like
   "the migration never ran".

The recurring pattern: **a count that multiplies is not evidence until you look at the rows,
untruncated, and a join by id is not evidence of absence.** Both now have committed
measurement scripts so the honest version is the easy one to get — `just audit-regression`
(both directions) and `just v1-drug-gap` (id vs effective coverage).

---

## Final scorecard (2026-10-18, pass 5)

Every fix in, full pipeline run, both validations passing.

### Quality — much better

| | v1 (medic-ingest) | now |
|---|---:|---:|
| error rate overall | 25.8% | — |
| **error rate at HIGH** | no usable tier | **5.5%** |
| audited errors remaining at HIGH | — | **8 of 76** |
| audited-correct kept | 222 | 177/221 (80.1%) |

Of the 76 audited errors: 56 gone, 13 capped at LOW, 2 MEDIUM, 8 still HIGH. Both error
classes v2 had *introduced* are closed — `FP1_setting` 28 → 3 at HIGH, `FP6_coingredient`
15 → 1.

### Volume — much higher

| | before this stretch | now |
|---|---:|---:|
| KGX edges | 12,140 | **87,353** |
| indication pairs | 8,541 | **21,553** |
| drugs | 4,323 | **4,882** |
| `preventative_for_condition` | 1,115 | **9,085** |
| `diagnoses` | 139 | **888** |
| SPL labels read | 1,976 | **18,261** |
| India records | 112 | **3,243** |

### Coverage vs v1 — six sources above, two below

```
[OK ] drug_list_flexible   v1= 3883  now= 4882  (+999)
[OK ] drug_list_stringent  v1= 2836  now= 3266  (+430)
[OK ] orangebook           v1= 2666  now= 2725   (+59)
[OK ] purplebook           v1=  602  now=  642   (+40)
[OK ] ema                  v1=  949  now=  995   (+46)
[OK ] russia               v1= 1831  now= 5885 (+4054)
[LOW] pmda                 v1= 1200  now= 1174   (-26)
[LOW] india                v1= 3118  now= 2759  (-359)
```

### The one number that is not Pareto

**Effective v1 drug coverage: 2,520/3,883 (64.9%)** — up from 61.8%, but it means 1,363 of
v1's drugs are still not in the export *even though we now carry 999 more drugs than v1 did*.
We have more drugs, but not a superset. By v1's own approval flags the remainder is
**410 usa, 338 india, 244 russia, 56 japan**.

So the verdict is **near-Pareto, not Pareto**: dramatically better quality, substantially more
volume, every source but two at or above v1, and a residual set of ~1,363 v1 drugs we do not
carry. Closing that is source-coverage work, not identity work — the RxNorm addition moved it
only 1.3 points (+412 drug-list rows, 284 RXCUI nodes), which is the measurement that settles
the question of whether identifiers were the problem. They were not.

## Standing state

- **Pass 3** (every fix except RxNorm) running since 09:01 on 18,261 labels.
- **RxNorm index not yet rebuilt** — pass 3 has `drugs.db` open. Rebuild, then re-measure.
- Verdict so far: **much better on quality, still behind on regional coverage.** Not Pareto
  yet; the gap is India/Russia/Japan breadth, and the fixes for two of those are in.

### Still open

- Russia: 248 absent v1 drugs, and `russia.csv` *grew* 1,831 → 5,885, so likely grounding
  rather than coverage. Untouched.
- 573 edges ship with an unnamed disease object (I-14 vs #96) — correct extractions nobody
  can review.
- `just determinism` widening; the #100 residue measurement.
- Phase 5 (#91's verification gate) is designed, not built:
  `specs/2026-10-18-assertion-verification-design.md`.

---

## Lever 2 implemented (2026-10-19) — `749dc06`

### The precise v1 comparison, at last

v1.0.0's release assets carry only drug lists, which is why every earlier comparison stopped
at the drug level. Its **grounded** pair lists are on `main`:
`medi/indications/data/08_reporting/indications_medi_coverage/matrix_indication_list.xlsx`
(11,071 pairs) and `matrix_contraindication_list.xlsx` (3,981), each with a final normalized
drug id and disease id.

| | indications | contraindications |
|---|---:|---:|
| v1 | 11,071 | 3,981 |
| now | 21,553 | 4,433 |
| exact | 2,877 | 786 |
| re-qualified (same pair, different id) | 977 | 179 |
| **v1 only** | **7,217** | **3,016** |
| new only | 13,213 | 3,266 |

Weighted by ground truth, against the audit's v1-era sample: **89.6% of v1's audited errors
are gone** (69/77) and **52.7% of its audited-correct pairs are retained** (117/222).

**A correction I owe the record.** I had been quoting 80.1% correct-edge retention. That is
retention of pairs sampled from the project's *own* later build (medic2 @47dd0b7) — the
rebuild measured against itself, not against v1. The v1 figure is 52.7%. The two are not
interchangeable and I reported the flattering one in a Pareto context.

### Why the 7,217 are missing — diagnosed, not assumed

Against the Mondo subclass graph v1 itself used (`mondo_edges.tsv` on `main`, 38,242 edges):

| | pairs | share |
|---|---:|---:|
| unrelated in the hierarchy | 3,668 | 50.8% |
| we assert nothing for that drug | 2,497 | 34.6% |
| **we assert an ancestor — v1 downfill artifact** | **703** | **9.7%** |
| **we assert a descendant — we are more specific** | **349** | **4.8%** |

The last 1,052 (14.5%) are **not losses**: §5.7 records that v1 propagated indications *down*
the Mondo hierarchy and that this was removed deliberately, and the 349 are cases where our
object is finer than v1's. Reclassifying them lifts retention from 34.8% to roughly 44%
before any recall work.

And against the label text we hold: **2,266 (31.4%) name a disease that is already there** —
the recoverable share, and Lever 2's target.

### Three levers, filed

- `issues/issue_lever1_historical_dailymed.md` — v1's archived DailyMed corpus (50,628 rows,
  on `main`). I first scoped this at 38%; measured against the snapshot itself only **749
  pairs (10.4%)** are cleanly recoverable, for ~35,000 LLM calls. Each file is two columns —
  no setid, no date, no marketing category — so no resolvable reference, no homeopathic
  filter, and degraded negation scope. Needs a SPEC §9 decision.
- `issues/issue_lever2_ground_disease_context.md` — implemented, below.
- `issues/issue_lever3_classify_remaining_v1_gap.md` — teach the comparison scripts the Mondo
  hierarchy so deliberate removals stop reading as regressions.

### What Lever 2 did

Biolink types `disease_context_qualifier` as **`disease`** — a class, not a string — so the
export had been putting a joined sentence into a slot typed for an entity. A test pins the
range so a future retype fails loudly.

Prompt rule 0 requires each named condition verbatim; `is_generic_context` rejects
"pathologic causes" and kin inside the parser, because a prompt rule is not a guarantee;
`ground_context_names` resolves each context through the existing cascade, positional with
`""` on failure so a grounding miss costs the CURIE and not the fact; the export emits the
CURIE on the Biolink slot with the full set on `medic_disease_context_ids` / `_labels`.
`disease_context_ids` declared on the closed `EvidenceItem` first.

1,245 tests pass, lint clean.

### Owed

- Re-ingest to populate `disease_context_ids`, then measure the share of the 2,266 that
  becomes CURIE-reachable. The grounding itself costs no LLM calls.
- Decide whether to bump the extraction cache namespace: cached rows keep their old context
  values (including "pathologic causes") until their text is re-extracted.

### Lever 2 measured — and my projection was wrong

Warm re-ingest, **zero LLM calls**:

| | |
|---|---:|
| evidence rows carrying a disease context | 18,643 |
| rows with ≥1 grounded CURIE | **13,951 (74.8%)** |
| context names grounded | 18,590 of 25,822 (**72.0%**) |
| **v1-only pairs now reachable by a grounded context** | **287 of 7,217 (4.0%)** |

**I framed Lever 2 as recovering 2,266 pairs (31.4%). It recovered 287 (4.0%).** The 2,266
was the share of v1-only pairs whose disease appears *somewhere* in label text we hold —
anywhere, including inside the target phrase or an unrelated sentence. That is an upper bound
on text presence, not an estimate of what grounding the context recovers, and presenting it
as the latter was my error. The remainder needs a paid re-extraction (~18,000 sections),
because cached answers predate the new prompt rule, and its yield is unknown.

The 287 are the right rows — precisely where v1 promoted a context to the object:
`Baclofen -> multiple sclerosis`, `Albuterol -> asthma`,
`Verapamil -> Wolff-Parkinson-White syndrome`, `Prednisolone -> Stevens-Johnson syndrome`.

The durable value of Lever 2 is therefore structural rather than recall: 18,590 contexts that
are now searchable CURIEs instead of free text, and an export that satisfies
`disease_context_qualifier`'s actual Biolink range (`disease`) rather than only its name.

The guard behaved correctly on the narrow cases: `pathologic myopia` and `pathological
hypersecretory conditions` are real conditions and survived, because it matches whole-string
categories only.

**Revised recommendation.** Lever 3 (reclassify the 1,052 downfill/more-specific pairs) is
now clearly the best remaining move: ~14.5% of the apparent gap, a scripting change, no LLM
spend. Lever 1 is 10.4% for ~35,000 calls. Lever 2's remaining share needs re-extraction and
is unquantified.

---

## 2026-10-22 — Adopting Kevin's four open PRs (#101–#104)

`workplan.md` assessed all four one by one; this is what landed. All four are merged with
his commits preserved in history and co-authorship on the merges, in the workplan's order
except that #103 (the expensive one) went last so the cheap verification could run first.

| PR | taken | changed |
|---|---|---|
| #101 enabler gate | as written | nothing |
| #104 pinning | mechanism as written | pins name **our** releases, and are now checked against the files on disk |
| #102 salt tier-0 | as written | settled the 0.90/HIGH boundary it depends on |
| #104 xrefs | as written | resolved where our SPL UNII belongs (`source_xrefs`, not `alternate_ids`) |
| #103 label ranking | pool filtering, ranking, date parsing, section check | kept our breadth: 20 labels, all names, generic-first |

### Four decisions that were open and are now made

1. **`salt_ester_strip` 0.90 vs the `conf >= 0.9` HIGH threshold.** Settled by reading the
   *predicate*, not by moving the number: a `skos:closeMatch` or `skos:broadMatch` grounding
   caps at MEDIUM however certain the rule. Measured scope before changing it — 171
   `salt_ester_strip` steps and 3 `inn_suffix_in_to_ine` in the current indications, nothing
   else inexact is at or above 0.9. #102's tier 0 is what made this load-bearing: it fires
   the same rule for the 617 salts that *do* have a ChEBI entry.
2. **Where the SPL-asserted UNII belongs.** `source_xrefs`, not `alternate_ids` — reversing
   the leaning recorded in `workplan.md`. The label asserts the UNII of its own active
   moiety; that the moiety is *this* ChEBI term is MeDIC's grounding decision, so an
   exactMatch row would publish an identity nothing verified.
3. **Which ontology releases to pin.** The ones in `cache/ontologies/` (mondo 2026-07-06, hp
   2026-06-23, chebi 253, dron 2026-06-04), not the newest pinnable. Pinning forward would
   record a false reproducibility claim and move every grounded id as a side effect of a
   provenance commit.
4. **Ranking vs breadth in acquisition.** Both. They fix different faults — *which* label
   and *how many*, under *how many names* — and filtering the pool is what lets the two
   compose rather than compete.

### Two bugs of ours that reading his PRs found

- The string date sort. His note was about the acquirer, where ours was latent because every
  hit was kept; the same code in `setid_lookup.lookup_setid` was **live**, picking which
  DailyMed document an FDA evidence item deep-links to. Fixed, with tests over the month,
  year and unparseable cases.
- "We keep all 20, so we are a superset of his best 3." False. Without the doctype filter and
  at `pagesize=20`, the useful label can be crowded out by veterinary and duplicate OTC
  entries before we see it.

### Owed

- The gates cannot judge three of the four until a rebuild: the products on disk predate all
  of this, so `just v1-pair-gap` still reports 33.9% / 46.3% by construction. Index rebuild
  → `build-all` → gates is the cheap half (no LLM); #103 needs re-acquisition and re-ingest.
- The UNII could publish as a SSSOM `closeMatch` row once its collection moves from export
  time to merge time. It is gathered from the association products, which the SSSOM exporter
  does not read.
- #88 point 5's OTC half ("each partner owns its own symptom class") is still open; #101 is
  explicit about not covering it.

### The gold standard, measured — #88's mechanism holds, claim recall does not

First score of #88 against the recovered pilot (`just gold-combination-audit`), on the
products as they stand *before* any of today's merges take effect:

| | |
|---|---:|
| curated claims | 1,133 |
| excluded — curator bound the term BROADER/NARROWER | 388 |
| excluded — no curated term | 119 |
| **scored** | **626** |
| found | 309 (49.4%) |
| missed | 317 (50.6%) |
| — of found, attributed correctly | **276 (89.3%)** |
| — of found, `over_attributed` | **0** |
| — of found, `under_attributed` | 33 (10.7%) |

**Zero over-attribution.** That is the number #88 was for: not one curated claim that the
label attributes to a single moiety is credited to its partner as well. The residual 33 are
the *opposite* error — a product-level claim reaching some ingredients and not all of them
(`canagliflozin-anhydrous-metformin`: type 2 diabetes credited to metformin and not to
canagliflozin) — which no amount of tightening attribution would fix and which the old
single-number framing could not have distinguished.

**The 50.6% miss is not an acquisition gap.** All 103 curated setids are on disk and all 626
scored claims sit on documents we hold, so this is extraction or grounding, measured and not
guessed. Checked before reporting it, because attributing a loss to the wrong stage is the
mistake this project has made most often.

Worth stating plainly: ~1,000 curated attributions existed for two weeks while #88, #89 and
#97 shipped unscored, and the first thing scoring them says is that half the curated claims
are not in the build at all.

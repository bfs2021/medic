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

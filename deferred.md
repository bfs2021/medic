# Carried forward

Running ledger for the #88–#100 execution. Anything deliberately not done, blocked, or owed.
Updated as work lands. Companion to `plan.md`, which holds the sequence.

## 1. Comments — all posted 2026-10-17

All 16 are on the tracker: #64, #65, #66, #68, #88, #89, #90, #91, #92, #93, #94, #95, #96,
#97, #99, #100. The local `comment_issue*.md` drafts were deleted after posting; the text
lives on the issues now.

Two of them are corrections to figures given earlier in this thread: #65's severity (the
regex counted "is not indicated for" as positive, inflating it) and #68's projection (stated
three times before settling — 207, then 1,122, then 119).

**No issue was closed.** #59–#62 and #64–#68 are all still open, and closing is Nico's call,
not something the comments did.

## 2. Declined or narrowed on purpose

- **#93 part 2 — no self-clash gate.** 10 of the 34 clashes are the aliskiren case where one
  side is #89's population error and the other #97's co-administration error. A gate
  demoting both would hide two bugs behind one symptom. Offered instead: a QC *report* that
  lists the pairs without changing a tier. Awaiting a decision.
- **#93 — gate is `negated_inversion`, not all EXCLUDED.** #62 settled that a curator REJECT
  should mark rather than delete, with a test asserting it. Both sets are the same 14
  assertions today, so nothing is lost. **Open policy question for Nico:** if they diverge,
  should a human REJECT delete the edge or mark it?
- **#99 — immune globulins excluded from the prevention predicate.** `Rho(D) immune globulin
  → ectopic pregnancy` would become "prevents ectopic pregnancy"; those objects are exposure
  events, i.e. #89's error class. Fix the objects first, revisit the predicate after.
- **#100 — flag definition unchanged.** "Set it whenever the stored text is shorter than the
  section" fires on every multi-span section (~6,000 strings that are scoped, not cut).
  **Owed:** measure whether any genuinely-cut text remains after `dd624ed`, and add a
  narrower mid-token/unclosed-bracket signal only if it does.

## 2b. Corrections owed to the issues themselves

Two of #95's premises did not survive measurement, and the comment needs to say so:

- "Ground on `activeIngredient` substance names" — those exist on **35 of 1,976** labels, so
  it is not available. And where both are present the *moiety* is usually the better of the
  two, because it is the half that strips the salt (`TOBRAMYCIN SULFATE` → `TOBRAMYCIN`).
- "Salt forms as separate drugs (~197 drug nodes)" — none of those are DailyMed's. By
  `mention_source`, 561 salt-form nodes come from Orange Book, plus GRLS 27, PMDA 29, CHINA
  28, EMA 20, EveryCure 11, CDSCO 4. DailyMed contributes zero. That half belongs with #27.

**Still open from #95:** the salt-form nodes above, which need the normalization index
(#27), not an ingest change.

## 2c. #94 is only partly done

Of its three asks, one landed and two did not:

- ✅ **Prefer ChEBI substance over DRON clinical-drug forms** (`44ac0be`). The matcher ran
  fields outermost so a DRON label beat a ChEBI synonym; it is now vocabulary-major.
- ❌ **Emit RXCUI/UNII xrefs on every drug node.** All 1,976 SPLs carry a UNII on every
  active moiety, and `nodes.py` already emits `alternate_ids` as `xref`, so the two ends
  exist — but DailyMed is an *indication* source and the drug node's identity is built by
  `drug_merge` from Orange Book / Purple Book / EMA / PMDA / GRLS / CDSCO / CDE / EveryCure.
  Getting the label's UNII onto the node needs a cross-layer path that does not exist yet.
  Only 1,092 of 4,323 nodes carry a `unii` today, and from enrichment rather than the label.
  **This is the half that actually unblocks consumers** — 3,448 assertions (28%) are dropped
  by anything joining on ids — so it deserves its own design rather than a shortcut.
- ❌ **Pin ontology versions, or prefer the oldest ChEBI id in the clique.** Untouched.
  ChEBI and DRON are still fetched unpinned from PURLs, so the same drift recurs on every
  index rebuild. Related to #27.

Also unaddressed from #96: abbreviations used as display names (DFSP, WARTS). No comma to
swap, so de-inversion is not the remedy; they need a better label source (#13).

## 3. Phase 3 — complete

**Done**: the `Claim` contract and parser, both structured prompts, the v3 cache namespace,
claim dedup, ingredient threading, qualifier slots on the edge (#90.2), `Claim.predicate` →
Biolink (#99), qualifiers in the merge key (#90.3), #97's contraindication prompt with the
co-administration split.

**Re-extraction done**, all four sources:

- **DailyMed** — 1,832 labels, ~6,200 LLM calls, zero extraction failures. Two passes: the
  ion-guard fix (`148aec0`) landed mid-run, so a second pass picked up the 24 ion-only drugs
  for **48 calls** against the warm cache (the cache keys on section text, so everything
  already bought was a hit). Not killed mid-flight, because `atexit` does not fire on
  SIGTERM and a kill would have discarded up to 99 paid answers. Final: **6,002 indications,
  2,453 contraindications**. Rescues verified present and correct: `LITHIUM` → bipolar I
  disorder, `LANTHANUM CATION (3+)` → hyperphosphatemia, `GADOLINIUM CATION (3+)` → Liver
  Lesion, `FERRIC DERISOMALTOSE` → iron deficiency anemia.
- **EMA** — 49 min, exit 0. **PMDA** — 31 min, exit 0. **India** — 2.5 min, exit 0 (and that
  speed is the symptom behind `issues/issue_india_primary_source_recall.md`).
- Merge, normalization, all four exports and both validations re-run after the fixes in §4c.

**Latent: the cache key does not include the prompt.** `_text_hash(indication_text)` keys on
the section text alone, so changing a prompt does not invalidate the answers it produced —
the v3 namespace was bumped by hand for the structured format. Six contraindication entries
in the current cache predate #97's rule 7 and so may hold fabricated targets; the
deterministic guard in `looks_like_disease_name` rejects those, which is the reason it exists
rather than relying on the prompt. Worth making the key prompt-aware before the next prompt
change, or the next one needs another manual namespace bump to be safe.

**Not done:**

- **EMA / PMDA / India get the structure but not the attribution.** All three call
  `extract_diseases_from_text` **without** `ingredients=`, so the prompt cannot name an
  ingredient it was never given: they get #89/#90/#99 (target vs context, qualifiers,
  predicates) and none of #88. `plan.md` item 10 explicitly included "fix the EMA/India
  direction too"; that part did not happen, and EMA was re-extracted in this state.

**Still deferred to later phases:**
- **Phase 4** — recall (#98). Strictly after Phase 3.
- **Phase 5** — #91: the per-assertion verification question, and giving HIGH a positive
  signal to earn.

### 3b. The first full run emitted zero indications and exited 0

Worth recording because the test suite could not see it. `context_only_names` still declared
`list[Extracted]` and read `e.name`, while its one caller had moved to `list[Claim]`; the
caller's `except Exception` logged "skipping" once per label. ~1,900 paid calls, no output,
exit 0.

Fixed: the signature and body, plus both extraction handlers now re-raise
`AttributeError`/`TypeError` so a code bug cannot read as a skipped label, plus a tally of
extraction skips reported at the end of the run.

Two tests written to catch exactly this class had passed. The grep-based one
(`test_no_ingester_unpacks_name_off_a_claim`) was pinned to the previous instance's variable
names, and cannot be fixed by a better regex: `e.name` is correct in `_encode_extractions`
and wrong in `context_only_names`. **Filed `issues/issue_no_type_checker.md`** — no type
checker runs on this repo, and that is the only thing that distinguishes the two.

## 4. Owed from earlier work in this thread

- ✅ **`atexit` flush gap (#57 follow-up)** — closed. `atexit.register` on a best-effort
  wrapper, so a failed write cannot turn a finished build into a failed one. Proven on real
  data: a six-label smoke script that never flushes now persists all six answers.
- **`just determinism` covers only the merge.** #57's other "worth doing next": the check
  re-runs the deterministic stage and is structurally unable to see extraction
  non-determinism. Not filed as an issue.
- **`exports/` is stale** — dated 2026-08-23, before all of this. `just export-legacy`,
  `export-kgx`, `export-sssom`, `export-reliability`, then `validate-all`. Cheap, no LLM.
- **`specs/2026-10-01-mech-migration-design.md`** — untracked, appeared 1 October, not written
  by me. Asked about it four times, never answered. Left alone through every commit.

## 4b. Rebuilds

Phase 1's rebuild landed in `fecf1e1`. Phase 2's ran: 87 ion moieties dropped, 152 metabolite
substitutions across 173 labels, MONDO objects 2,116 → 2,277, #95's named ion/metabolite
pairs 133 → 25.

Phase 3's re-extraction is running (see §3). After it: the merge, the four exports and
`validate-all`, then the measurements owed below.

**Rebuild complete.** 12,131 edges / 28,727 nodes. `just validate-all` and `just
validate-kgx` both pass — the first time with the target classes applied, see §4c.

Measured:

- **#90 qualifiers: 7,626 of 12,131 edges (62.9%)** carry at least one, from 0. population
  5,882, disease_context 3,218, temporal 1,173, sex 567, co-administered 1,016.
- **#99 predicates:** 1,115 `preventative_for_condition`, 139 `diagnoses`, 8,486 `treats`,
  2,382 `contraindicated_in`. All the non-`treats` ones previously shipped as `treats`.
- **#91 tiers:** HIGH 78.6% → **66.0%**, LOW 7.6% → **28.3%**, MEDIUM 5.6%.
- **#96 names:** 0 still in MeSH inverted form (was 66). Unnamed is **102** (was 91) — the
  source-literal fallback had to be reverted for licensing, see §4c.
- **#95:** ion drug nodes 16 → 10 (195 → 84 assertions); metabolites 8 → **1** (82 → 4).
- **#88 attribution:** 634 of 803 `ingredient_subject` rows are single-ingredient labels;
  only 193 multi-ingredient rows carry an explicit attribution, incl. empagliflozin via
  `when_used_as_component`.

Answered in the negative, and said so in the comments:

- **#96's four named wrong-sense groundings all persist unchanged.** Three are MONDO ids, so
  vocabulary ordering cannot help — it is a synonym collision, not a preference problem.
- **Re-grounding resolves 0 of 3,229 non-MONDO objects to MONDO** (1,254 to HP). Only the
  merge's xref normalization moves ids toward MONDO (412).
- **#94's velaglucerase alfa still has both** `CHEBI:747652` and `DRON:00019756`.

**Still owed:**

- #100: whether any genuinely-cut text remains (also §2).
- #88/#89/#97: the per-case rates, which need the gold standards — see §4d.

### 4c. Two bugs the rebuild's validation caught

- **`normalize-disease-ids` was reading a record shape that no longer exists** (`84237b5`).
  It reported all 8,541 associations as non-MONDO on a product that is 62% MONDO, resolved
  0, and stamped `drug_disease: '|'` on every one, which failed the closed class. My earlier
  "validation is clean" was wrong: `validate-schema` without a target class does not check
  the closed class, only `validate-all` does.
- **#96's stub-name fallback was republishing withheld labels** (`e092352`). 102 concepts
  whose labels the grounding store withholds under I-14 rule 2 got named from the source
  literal, which for these *is* the restricted string. Caught by
  `test_restricted_labels_not_published`, the artefact-level test written after the last
  such leak. Reverted for those concepts; they ship unnamed and their edges stay
  unjudgeable, which is the cost of the licence boundary.

### 4d. The gold standards were never wired in

`plan.md` says they "should be wired in as tests **before** the prompt changes". They were
not, and that is why #88, #89 and #97 can report mechanism but not rate:

- `fp_audit.json`, verdict `FP1_setting`, release `new` — 38 population-as-target cases.
- `contraindication_sample_audit.tsv` — 73 cases, 18 wrong, 12 live.
- The 102 curated ingredient sets from the abandoned pilot (`curation/combos-*`, #70–#87).

All three are in Kevin's repo. Fetching them is cheap and is the highest-value remaining
measurement work.

## 5. Release-note caveats

- **HIGH fell 7,489 → 5,713** on *interim* gates. Correct per the audit (combination-only was
  65% wrong, population-as-target 13.1%), but it is a large visible drop. Phase 5 is where
  HIGH gets a positive signal to earn its way back. Any release before then must say HIGH
  became conservative, not that quality fell.
- The soft-launch subset fell 7,804 → 5,949 for the same reason.

## 6. Spot checks that contradicted the aggregates

Qualifier coverage going 0 → 62.9% looked like unambiguous success. Checking #89's own named
examples gave 4 of 6:

| example | result |
|---|---|
| eplerenone → acute MI | fixed → Congestive heart failure |
| posaconazole → AML | fixed → Invasive Fungal Infections |
| bosentan → congenital heart disease | fixed → Pulmonary arterial hypertension |
| tirzepatide → prediabetes | fixed → Type 2 diabetes, Obesity |
| basiliximab → kidney transplantation | **0 edges — the drug is gone from the export** |
| deferasirox → anemia | **degraded** → Cancer, Poor Performance Status; iron overload absent |

Both failures would have read as successes if measured only by the absence of the wrong edge.
Filed `issues/issue_deferasirox_lost_indication.md`. Basiliximab is a biologic and so most
likely #94's identifier problem, which is further reason to do #94's xrefs next.

## 7. Issues filed this session (drafts, not posted)

- `issues/issue_no_type_checker.md` — a stale annotation zeroed a full paid build; no type
  checker runs, and no grep can distinguish the correct `e.name` from the wrong one.
- `issues/issue_apostrophe_folding_anchors.md` — SPL text uses both apostrophe glyphs (62
  ASCII vs 52 curly per 600 labels); 33 correct eponym extractions are falsely unlocatable.
- `issues/issue_india_primary_source_recall.md` — India contributes 112 drugs where v1 had
  3,118; the CDSCO PDF parser recovers 3.6% and one 139 KB PDF yields zero rows.
- `issues/issue_deferasirox_lost_indication.md` — above.

## 8. `just compare-release` was broken and is now fixed

It crashed on `The truth value of a Series is ambiguous` (duplicate keys make `.loc` return a
Series). Fixed in `84237b5`. It is the tool that answers the recall question and it would
have surfaced the India regression at any point since that switch. Current state against
v1.0.0: drug curie overlap **41.2%** (1,598 of v1's 3,883), which is the CHEBI-only namespace
problem our own gap report identified and #94 reports from the consumer side.

## 9. Phase 4 — recall, in progress

### Scored at last, in both directions

`src/data/fp_audit.json` **is** committed upstream (the TSV worklists the issues link to are
not — `experiments/out/v2/` is gitignored there). It holds the 300-edge blind audit with a
verdict per edge, so it scores errors removed *and* correct edges kept. Wired up as
`just audit-regression` + `scripts/audit_regression.py` (`a1f1572`); the file is fetched, not
vendored, since it is licensed with drug-disease-compare.

| | |
|---|---|
| correct kept | **166/221 (75.1%)** |
| wrong removed | 58/79 (73.4%) |
| error rate among survivors | **11.2%** vs 25.6% measured |

**I nearly reported 41% loss of correct edges.** An id-only join counts a drug whose primary
id moved from `UNII:` to `CHEBI:` as a lost edge. Matching on id *or* normalised label gives
the real 25%. The harness does both, and the docstring says why.

### Done

- **#94's UNII xrefs** (`9a73efd`). 1,367 drug nodes carry one; every biologic the issue
  names is covered and spot-checked correct. The identifier was never missing — it is in the
  same `<activeMoiety>` element the ingest already reads.
- **A drug that will not ground no longer discards its whole label silently** (`a1f1572`).
  70 of 1,857 labels (3.8%) were losing every indication under a `logger.debug`; now counted
  and reported. The salt/hydrate class is recovered by retrying without `_GENERIC_NOISE`
  words (`TENOFOVIR ANHYDROUS` → `TENOFOVIR`): +10 indications, +5 contraindications, zero
  LLM cost. This is how thiamine → beriberi was being lost.

### Open — `issues/issue_recall_gap_phase4.md`

The two measured causes of the remaining 55 lost correct edges: 107 moiety names that still
never ground (a `relatedSynonym` acceptance policy question, a vaccine-antigen modelling
question, and ions), and the target-replaced-by-its-context prompt defect with two confirmed
cases (thiamine, deferasirox).

### Still blocked on files we do not have

plan.md item 13's 112 audited-correct dropped pairs and ~1,300 recall candidates, and #97's
73-case contraindication rate. Worth asking Kevin for the TSVs rather than reconstructing.

### Not started

- Phase 4 item 14: #65's Limitations-of-Use splitting against the worst-hit drugs
  (lidocaine −122, iodine −83, hydrocortisone −82, dexamethasone −73). Note IODINE appears in
  the ungroundable list above, so some of that −83 is cause 1, not extraction.
- Phase 5 (#91's per-assertion verification question).
- #94's ontology pinning and the one-drug-two-ids case (docetaxel still has `CHEBI:4672` and
  `CHEBI:755540`).

## 10. Phase 4 — the dominant cause is acquisition, not extraction

Diagnosing the 55 lost-correct edges disproved both of my earlier hypotheses and found
something larger.

### Both "prompt drops the target" cases were misdiagnosed

- **thiamine**: not a prompt defect. The 11 shipped thiamine edges come from **BACMIN, a
  15-ingredient multivitamin** (moiety "THIAMINE ION"), and the objects are its
  "physiologically stressful conditions" list — #88 fan-out plus #89 context-as-target.
  **#88's gate catches them all: 11/11 LOW, and all 292 pairs from labels with >5
  ingredients are LOW.** The beriberi loss is §9's cause 1 (THIAMINE does not ground).
- **deferasirox**: the extraction is **correct** — `verbatim='chronic iron overload'` with
  the right context and population, at tier HIGH. It grounds to `UMLS:C1739382`, whose label
  the store withholds under I-14 rule 2, so the edge ships with an empty disease name. Not
  lost: invisible. `issues/issue_deferasirox_lost_indication.md` is wrong on its premise and
  needs rewriting as the labelling problem it is.

**573 edges (4.7%) ship with an unnamed object, across 102 concepts.** They cannot be
reviewed by anyone and are the cost of I-14 colliding with #96.

### The 55, properly split

| | |
|---|---|
| grounding-granularity (pair exists under a related term) | **26** |
| true losses (no related object at all) | **29** |

Granularity examples, some of which are arguably *ours* being better: Talquetamab — audit
"plasma cell myeloma", ours "Multiple myeloma" (synonyms); Clozapine — audit "schizophrenia",
ours "Treatment-refractory schizophrenia"; Minocycline — audit "respiratory tract infectious
disease", ours "Respiratory tract infection". So the real recall loss is below 25%, and a
chunk of what remains is a granularity-matching artefact rather than missing knowledge.

### The dominant cause of the true losses: one label per drug

`acquire._most_recent_setid` fetches a page of **20** setids per drug name, sorts by
published date, and returns **only the most recent**, discarding the other 19. A drug's
indications are split across manufacturers and formulations, so what we get is whichever
label happens to be newest.

- `lansoprazole` has **2** labels in `data/raw/dailymed`, and produced exactly one
  indication: *Helicobacter pylori infection*. GERD, duodenal ulcer and erosive esophagitis
  are simply not in the text we fetched. (My earlier spot check said lansoprazole→GERD was
  present; that was **dexlansoprazole** — 'lansoprazole' is a substring of it. Third
  substring trap of the session.)
- propranolol, chlorpromazine, varenicline, dalteparin: **1 label each**.
- 1,976 SPLs against DailyMed's ~150,000.

**Measured cost of keeping all of them** (12 drugs, 221 SPLs fetched): 118 distinct
indication sections, i.e. **9.8 extraction calls per drug instead of 1**. About 10×, not
20×, because roughly half of a drug's SPLs share identical indication text and extraction is
cached on the section-text hash.

This is a spending decision, not a bug fix, so it is Nico's call — see the question put to
him. A cheaper targeted variant exists: broaden only for drugs whose single label yielded
0–2 indications, which is where the loss concentrates.

## 11. Two method errors of mine, and what they cost

Recording these because both produced confident wrong answers that survived a while.

### Bypassing I-8 "fixed" a grounding gap that was not a gap

`a1f1572` added a retry in `_ground_drug`: strip salt/hydration words, re-ask. TENOFOVIR
ANHYDROUS started grounding, so it looked right. Reverted in `8204e0f`.

- It fixed nothing the grounder was not already doing. `salt_ester_strip` is an existing
  `PreprocessingRuleEnum` rule and `anhydrous` is already in its token list.
- The real reason the match failed is that `tenofovir` is in the index only as a
  `relatedSynonym`, of **three** objects. The cascade declined an ambiguous weak match.
- The retry then returned a *fourth* id the cascade had refused, recorded under a mention
  minted from the **stripped** string — so the store would show `TENOFOVIR -> CHEBI:45809`
  with nothing saying the label actually read `TENOFOVIR ANHYDROUS`. Not replayable from the
  record, which is exactly what I-8 forbids.

The lesson is the one CLAUDE.md already states: a transform that earns a match goes in the
enum and the SSSOM store, never in a caller. Done properly for the one subclass that was a
genuine bug — `srs_greek_encoding` (`80c4af2`), 51 occurrences recovered.

### Reimplementing the matcher's logic gave the wrong policy recommendation

I classified ungroundable moieties by reading `match_field` out of the index, concluded the
fix was "accept unambiguous `relatedSynonym`", and wrote that into the issue. Then I called
the real grounder over 418 moieties:

| | |
|---|---|
| ground successfully | 395 (94.5%) |
| fail | 23 |
| — **absent from the index entirely** | **20** |
| — ambiguous `relatedSynonym` only | 2 |
| — strong match yet still rejected | 1 |

**Accepting unambiguous `relatedSynonym` recovers zero.** HYDROCORTISONE looked like a
`relatedSynonym`-only failure in my classification and grounds fine in reality, which is the
tell that the classification was wrong. The remaining gap is vocabulary coverage — vaccine
antigen components, ferric/cupric cation, a couple of acids — not match-class policy.

Issue `issues/issue_recall_gap_phase4.md` has been corrected; it previously recommended the
useless fix.

## 12. Architecture doc brought level (`588d8c3`, `8204e0f`)

It still described one SPL per drug, bare disease-name extraction and a three-part merge key.
Now covers acquisition breadth with its measured cost, the structured claim contract (§5.8),
the reliability gates (§5.9), `just audit-regression` (§5.10), the UNII xrefs and the
reliability export. This is the document external collaborators read, and CLAUDE.md asks for
it to track pipeline changes.

## 13. Phase 4 rebuild running (started 2026-10-18 00:30)

Acquisition done: **1,976 → 10,835 SPL files** (11,148 setids resolved, 8,859 downloaded, 0
errors, 1,074 drug names with no SPL match — those are product-style names like "Warfarin
Sodium Tablets" that DailyMed's `drug_name` search does not match, a separate pre-existing
gap).

The chained pipeline (`scratchpad/phase4_pipeline.sh`) then runs: DailyMed ingest → merge →
normalize → 4 exports → `validate-all` + `validate-kgx` → `audit-regression`. EMA/PMDA/India
are deliberately **not** re-ingested: their kb files are current and unaffected by DailyMed
acquisition, so re-running them would cost hours for a byte-identical result.

Expect ~6 hours for the ingest at the observed ~33 calls/min.

### Comment drafted, not posted

`comment_issue98_recall.md` — #98 is the recall issue and this is the first comment with a
real cause for it. Holding the final audit-regression numbers until the rebuild lands, then it
is ready to post. It also carries the two corrections owed from #89 (thiamine and deferasirox
were both misdiagnosed) and flags the 573 unnamed-object edges.

Worth noting what #98 already contained: "the drop is not selective — 20 of 76 errors and 37
of 221 correct pairs, the same rate" and "the drugs with the most lost indications are the
broad old ones (lidocaine −122, hydrocortisone −82, dexamethasone −73, iodine −83)". Both are
the signature of a *coverage* loss rather than a filtering change, and both were in the issue
from the start. The diagnosis was available earlier than I reached it.

## 14. Phase 5 designed, not built

`specs/2026-10-18-assertion-verification-design.md` — the per-assertion verification question
for #91, with the acceptance criterion stated in advance (HIGH's error rate must fall *and*
correct-edge retention must not), the caveats from #91's own experiment kept in the design
rather than a footnote, and the requirement that the gate stay a pure function of record
fields so `just determinism` keeps meaning something.

## 15. Still owed

- **`just determinism` widening** (#57). Deliberately not attempted while the pipeline is
  running: it needs two warm-cache ingests to test, which would conflict. The useful widening
  is to assert a warm-cache ingest is byte-identical across runs, which covers parsing,
  screening and grounding without paying for LLM calls.
- **#100 residue measurement.** Needs the rebuilt export; reading products mid-write is unsafe.
- **The three gold-standard worklists** are not public (§4d). Asked for in the #98 draft.
- **#94's ontology pinning** and the one-drug-two-ids case (docetaxel still has `CHEBI:4672`
  and `CHEBI:755540`).
- **EMA/PMDA/India attribution** — they call the extractors without an ingredient list.

## 16. Phase 4 rebuild, pass 1 — results and the bug it exposed

Completed 04:49. Labels 1,976 → **10,835**; 16,204 new LLM calls; every step exit 0;
`validate-all` and `validate-kgx` both pass.

| | before | pass 1 |
|---|---:|---:|
| DailyMed indication records | 6,012 | **34,381** |
| DailyMed contraindication records | 2,458 | **14,028** |
| merged indication pairs | 8,541 | **14,770** |
| merged contraindication pairs | 2,014 | **3,366** |
| KGX edges | 12,140 | **48,178** |
| `treats` / `contraindicated_in` | 8,486 / 2,382 | 30,057 / 13,619 |
| `preventative_for_condition` / `diagnoses` | 1,115 / 139 | 4,021 / 472 |
| HIGH share | 66.0% | 62.3% |

Audit regression: correct kept **171/221 (77.4%)**, up from 166 (75.1%); wrong removed 56/79
(70.9%), down from 58; survivor error rate 11.9% against 11.2%.

### Read that audit number carefully

**The audit cannot see the recall this bought.** Its 221 correct edges are a fixed sample of
pairs that existed in `47dd0b7`; broadening acquisition adds pairs that were never in the
sample. So the harness measures *retention and precision*, not absolute recall, and a 73%
increase in pairs moves it by 2.3 points. Both numbers are right and they answer different
questions. Any claim about recall growth has to come from the pair counts or from Kevin's
`recall_gap_candidates.tsv`, not from this harness.

### Only 5 of the 55 lost edges came back — and that exposed the real bottleneck

lansoprazole → GERD was still missing. After acquiring 10,835 labels, exactly **2** carried
LANSOPRAZOLE as an active moiety, and **1 of its 20 setids** was on disk.

Cause: `_usa_drug_names` took the first `source_ingredients` string and `break`ed. The drug
list holds product-style strings — "Lansoprazole Enteric-Coated Capsules", "… Orally
Disintegrating Tablets", "… for Injection" — which DailyMed's `drug_name` index does not
match, while `curie_label` is "lansoprazole", which does and returns 20 setids. **1,074 of
2,591 names (41%) resolved to no SPL at all**, so those drugs' labels were never requested
under any name. Broadening to 20 setids per *name* was therefore inert for them.

Fixed in `ad0f3a4`: every candidate is queried, generic label first. 2,591 → **5,008** names.
Pass 2 is running.

### A trap that cost me a wrong diagnosis

`cache/enrichment/dailymed_setids.json` is written by **two** modules — `acquire` and
`ingest.dailymed.setid_lookup` — and the latter writes the old single-`setid` shape. Reading
that file after an ingest shows 1,606 single-setid entries and zero `setids` lists, which
looks exactly like "the cache migration never ran". It had run; the ingest overwrote the file
afterwards. Recorded in the commit message too.

### Grounding losses scale with the label set

240 labels (was 70) now lose every indication to ungroundable moieties, across 293 distinct
names (was 107): DEXTROSE x32, IODINE x31, FERRIC CATION x25, THIAMINE x23, PHENYLBUTYRIC
ACID x18, TENOFOVIR ANHYDROUS x17. The per-label rate is unchanged, so this is the same
vocabulary-coverage gap costing proportionally more — and it is now the largest single
mechanical loss in the pipeline. `issues/issue_recall_gap_phase4.md` holds the three classes
and what each needs.

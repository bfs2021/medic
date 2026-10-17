# Carried forward

Running ledger for the #88–#100 execution. Anything deliberately not done, blocked, or owed.
Updated as work lands. Companion to `plan.md`, which holds the sequence.

## 1. Comments drafted, not posted

The `gh issue comment` hook blocks me, so every one of these needs Nico to run it. Files are
untracked scratch in the repo root.

| issue | file | state |
|---|---|---|
| #64 | `comment_issue64_rebuild.md` | ready |
| #65 | `comment_issue65_correction2.md` | ready |
| #66 | `comment_issue66_demote.md` | ready |
| #68 | `comment_issue68_close.md` | ready |
| #92 | `comment_issue92_homeopathic.md` | ready |
| #93 | `comment_issue93_negated_treats.md` | ready |
| #99 | `comment_issue99_predicates.md` | ready |
| #100 | `comment_issue100_truncation.md` | ready |
| #91 | `comment_issue91_reliability.md` | ready |
| #94 | `comment_issue94_nodenorm.md` | ready |
| #95 | `comment_issue95_moieties.md` | ready |
| #96 | `comment_issue96_disease_nodes.md` | ready |
| #88 | `comment_issue88_combination.md` | ready |
| #89 | `comment_issue89_population_target.md` | ready |
| #90 | `comment_issue90_qualifiers.md` | ready |
| #97 | `comment_issue97_contraindications.md` | ready |

All 16 are written. None posted.

```bash
gh issue comment <n> --repo monarch-initiative/medic --body-file comment_issue<n>_*.md
```

Nothing has been posted for any of #88–#100. Issues #59–#62 and #64–#68 are also all still
open on the tracker; none were closed.

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

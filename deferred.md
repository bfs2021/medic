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
| #97 | — | **not written yet**; wait for the re-extraction measurement |
| #88, #89, #90 | — | **not written yet**; they need the re-extraction measurement |

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

## 3. Phase 3 — code complete; the re-extraction is running

**Done**: the `Claim` contract and parser, both structured prompts, the v3 cache namespace,
claim dedup, ingredient threading, qualifier slots on the edge (#90.2), `Claim.predicate` →
Biolink (#99), qualifiers in the merge key (#90.3), #97's contraindication prompt with the
co-administration split.

**Running**: the paid re-extraction over 1,832 DailyMed labels, ~13 LLM calls/min. Verified
on 8 labels first: 24 indications, 5 contraindications, zero extraction failures, and all
seven claim fields populating from real labels.

**DailyMed needs a second pass after it.** The ion-guard fix (`148aec0`) landed mid-run, so
the labels in flight were ingested with the old guard and the 24 ion-only drugs — lithium,
gadolinium contrast, ferric iron, the radiopharmaceuticals — are still missing from its
output. The re-run is cheap: the cache is keyed on section text, so every answer already
bought is a hit and only the rescued labels cost anything. Not killed mid-flight because
`atexit` does not fire on SIGTERM, so a kill would have discarded up to 99 paid answers.

**Latent: the cache key does not include the prompt.** `_text_hash(indication_text)` keys on
the section text alone, so changing a prompt does not invalidate the answers it produced —
the v3 namespace was bumped by hand for the structured format. Six contraindication entries
in the current cache predate #97's rule 7 and so may hold fabricated targets; the
deterministic guard in `looks_like_disease_name` rejects those, which is the reason it exists
rather than relying on the prompt. Worth making the key prompt-aware before the next prompt
change, or the next one needs another manual namespace bump to be safe.

**Not done:**

- **EMA / PMDA / India have not been re-extracted.** They call the same extractors and share
  the v3 cache, keyed by text hash, so their entries are still absent. DailyMed first because
  it is the largest and the only one with the ingredient threading; the others are the same
  command with no code change.
- `just export-*` and the merge have not been re-run against the new extraction yet.

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

**Measurements owed once it finishes** — each is a claim made in a drafted comment that I
have deliberately not asserted without the number:

- #94: whether velaglucerase alfa still gets both a DRON and a ChEBI id.
- #96: whether the four named wrong-sense groundings are gone (`non small cell lung cancer`
  → tumor-suppressor gene, `skin and soft-tissue infections` → karyomegalic interstitial
  nephritis, `mpox` → a chemical CUI, `secondary progressive disease` → root `disease`).
- #91: what HIGH's share of pairs becomes, which is the only honest way to say whether the
  tier is now worth filtering on.
- #89/#90: how many edges carry a qualifier, and whether the population-as-target rate moved.
- #88: whether the attribution fields change the combination-only slice.
- #97: the contraindication error rate against the 73-case audit.
- #100: whether any genuinely-cut text remains (also listed in §2).

## 5. Release-note caveats

- **HIGH fell 7,489 → 5,713** on *interim* gates. Correct per the audit (combination-only was
  65% wrong, population-as-target 13.1%), but it is a large visible drop. Phase 5 is where
  HIGH gets a positive signal to earn its way back. Any release before then must say HIGH
  became conservative, not that quality fell.
- The soft-launch subset fell 7,804 → 5,949 for the same reason.

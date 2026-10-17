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
| #88, #89, #91 | — | **not written yet**; they share the Phase 1 gate numbers |
| #95 | — | **not written yet** (`a8ac7ed`) |
| #94 | — | **not written yet**; only partly addressed, see below |
| #96 | — | **not written yet** (`44ac0be`, `0cfdaa2`) |

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

## 3. Phase 3 — half done, and the claims are inert until the rest lands

**Done** (`45f1e91`, `d8aeecf`): the `Claim` contract and parser, the structured prompt, the
v3 cache namespace, claim dedup, ingredient threading. Smoke-tested on ten labels — 42
claims, 40 findable quotes, 14 of 42 using a non-`treats` predicate, context on 26.

**Not done, in dependency order:**

1. The merge reads only `Claim.target` and discards the other six fields. Nothing downstream
   sees a qualifier, so the extraction's new output is currently thrown away.
2. Qualifier slots on the edge: `disease_context_qualifier`, `population_context_qualifier`,
   `temporal_context_qualifier`, `sex_qualifier` (#90.2).
3. `Claim.predicate` → Biolink, replacing the vaccine name-match with the model's own
   relation (#99's full set), plus `medic_is_diagnostic_agent` from product class.
4. Qualifiers in the merge key (#90.3) — what resolves the genuinely-both clashes like
   formoterol/asthma without demoting either side, and so closes the part of #93 I declined.
5. #97's co-administration clause split, which shares the same prompt.
6. **The paid re-extraction.** Deliberately not run yet: it would spend the money to produce
   fields the merge discards.

**Still deferred to later phases:**
- **Phase 4** — recall (#98). Strictly after Phase 3.
- **Phase 5** — #91: the per-assertion verification question, and giving HIGH a positive
  signal to earn.

## 4. Owed from earlier work in this thread

- **`atexit` flush gap (#57 follow-up).** Registration is automatic now; flushing at exit is
  not. A script calling the extractor directly still loses its answers, as a smoke test did.
  One `atexit.register(flush_all)` closes it. Not filed as an issue.
- **`just determinism` covers only the merge.** #57's other "worth doing next": the check
  re-runs the deterministic stage and is structurally unable to see extraction
  non-determinism. Not filed as an issue.
- **`exports/` is stale** — dated 2026-08-23, before all of this. `just export-legacy`,
  `export-kgx`, `export-sssom`, `export-reliability`, then `validate-all`. Cheap, no LLM.
- **`specs/2026-10-01-mech-migration-design.md`** — untracked, appeared 1 October, not written
  by me. Asked about it four times, never answered. Left alone through every commit.

## 4b. Rebuilds

Phase 1's rebuild landed in `fecf1e1`. Phase 2's is running: the ingest changed (87 ion
moieties dropped, 152 metabolite substitutions across 173 labels) and so did the grounding
matcher, which re-decides every source's disease grounding — about 177 of 3,545 non-MONDO
decisions are expected to move toward MONDO or HP.

## 5. Release-note caveats

- **HIGH fell 7,489 → 5,713** on *interim* gates. Correct per the audit (combination-only was
  65% wrong, population-as-target 13.1%), but it is a large visible drop. Phase 5 is where
  HIGH gets a positive signal to earn its way back. Any release before then must say HIGH
  became conservative, not that quality fell.
- The soft-launch subset fell 7,804 → 5,949 for the same reason.

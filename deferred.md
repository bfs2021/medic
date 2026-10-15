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

## 3. Deferred to later phases (by design, see `plan.md`)

- **Phase 3** — the structured extraction: #88 attribution, #89 target-vs-context, #90
  Biolink qualifiers, #99 full predicate set + `medic_is_diagnostic_agent` from product
  class, #97 co-administration clause split. One prompt, one re-extraction.
- **Phase 3** — qualifiers in the merge key (#90.3), which is what resolves the genuinely-both
  clashes like formoterol/asthma.
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

## 5. Release-note caveats

- **HIGH fell 7,489 → 5,713** on *interim* gates. Correct per the audit (combination-only was
  65% wrong, population-as-target 13.1%), but it is a large visible drop. Phase 5 is where
  HIGH gets a positive signal to earn its way back. Any release before then must say HIGH
  became conservative, not that quality fell.
- The soft-launch subset fell 7,804 → 5,949 for the same reason.

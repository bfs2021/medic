# Plan for Kevin's 2026-10-13 issue set (#88–#100)

Thirteen issues, all filed 2026-10-13 against the `medic2@47dd0b7` export, all backed by a
blind two-reviewer label audit of 300 random indication edges. This sequences them.

## Status as of 2026-10-18

| phase | items | state |
|---|---|---|
| 1 — mechanical gates | 1–6 | **done**. HIGH 78.6% → 66.0% of edges |
| 2 — identifier/node hygiene | 7–9 | **mostly done**. #94's UNII xrefs shipped (1,367 nodes); ontology pinning and the one-drug-two-ids case open |
| 3 — structured extraction | 10–12 | **done**. 62.9% of edges carry a Biolink qualifier, from 0; 1,115 `preventative_for_condition` and 139 `diagnoses` |
| 4 — recall (#98) | 13–14 | **in progress**. Cause found and fixed; rebuild running |
| 5 — reliability tier (#91) | 15 | **designed**, not built — `specs/2026-10-18-assertion-verification-design.md` |

All 16 issue comments were posted on 2026-10-17. `deferred.md` holds the running ledger of
what was deliberately not done and what is owed.

**The thing this plan got wrong.** It treated recall as a Phase 4 extraction problem to be
addressed after the prompt was right. The dominant cause turned out to be *acquisition*:
`acquire._most_recent_setid` fetched 20 setids per drug and kept 1, so a drug's indications
were whatever its newest label happened to state. Lansoprazole had 2 labels on disk and
asserted one indication. No prompt or grounding change could have recovered the rest, because
the text was never fetched. Fixed in `7c49633`; the label set went 1,976 → 10,835.

The second thing it got wrong: the gold standards were to be "wired in as tests **before** the
prompt changes". They were not, so #88/#89/#97 shipped without the regression set that would
score them. `src/data/fp_audit.json` is now wired up as `just audit-regression` and scores both
directions — errors removed *and* correct edges kept, since precision alone improves by
dropping everything.

## Substantiation

Every mechanical claim I could check locally holds:

| issue | claim | verified against |
|---|---|---|
| #88 | extraction runs once per ingredient over the same section text | `for drug_name in drug_names:` wraps the extraction call in `ingest/dailymed/__main__.py`; the prompt never names the ingredient |
| #92 | homeopathic SPLs are not filtered | zero occurrences of `homeopath` / `HPUS` / `product_type` anywhere in `src/` |
| #93 | `EXCLUDED` pairs are exported like any other | the string `EXCLUDED` does not appear in `src/medic/export/kgx/` |
| #95 | the ingest records the active moiety | `_extract_ingredients_from_root` reads `activeMoiety`, which SRS fills with counter-ions and active metabolites |
| #99 | every indication is `biolink:treats` | one `INDICATION_PREDICATE` constant; no preventative / ameliorates / diagnostic predicate exists |
| #100 | truncation only detects a hard-cap hit | `spans.is_truncated` is `len(text) >= SNIPPET_CHAR_CAP`, so text cut any other way is unflagged |

**#98 independently corroborates our own gap analysis** (`reports/2026-10-08-v1-v2-indication-gap.md`).
He measures medic-ingest 10,903 → 7,790 pairs keeping 3,605; we measured v1.0.1 10,085 → 8,085
keeping 2,267. Different baselines, same phenomenon, arrived at separately. The recall collapse is
real and is not a measurement artefact on either side.

## The error budget

From #91's audit — 291 judged edges, **25.8% wrong** (CI 21.1–31.1):

| class | share | issue |
|---|---:|---|
| population / setting as the target | 38 edges (13.1%) | #89 |
| not in label | 17 | #89, #98 |
| co-ingredient attribution | 11 (3.8%) | #88 |
| *slice:* supported only by combination labels | 40 edges, **65% wrong** | #88 |
| *slice:* asserted by ≥2 regulators | 69 edges, **7.2% wrong** | #61, #91 |

Two facts govern the whole plan. **#88 and #89 are the majority of the error mass**, and they
interact — the combination fan-out multiplies a partner's population error onto every ingredient.
And **`HIGH` is 93% of pairs at 26.7% wrong**, so the tier currently separates nothing (#91).

## The structural insight

**#88, #89, #90 and #99 are not four changes. They are one.**

Each asks the extraction to return structure instead of a flat list of disease names:

- #88 — which ingredient the claim is attributed to
- #89 — target vs population vs setting
- #90 — the context as Biolink qualifiers
- #99 — treats vs prevents vs reduces_risk_of vs diagnoses

One prompt, one call per label, one re-extraction run. #97's contraindication clause splitting
belongs in the same call. Doing these separately means paying for four re-extractions and
four prompt migrations; doing them together is the natural successor to #64's verbatim contract.

Each of those issues also names a cheap interim rule that needs no re-extraction. Those go first.

## Sequence

### Phase 1 — mechanical gates, no re-extraction (days)

Precision gained without touching the prompt. Ordered first because it is cheap, independently
revertable, and #100 unblocks the checkability the rest of the audit depends on.

1. **#92 homeopathic SPLs** — skip at ingest on `openfda.product_type` / `HOMEOPATHIC`/`HPUS` in
   the text. 235 pairs, one condition. Also removes drug nodes that exist only because of these
   labels (skim milk, silicon dioxide, barium cation).
2. **#93.1 drop or mark EXCLUDED in the export** — 14 edges the pipeline has already judged wrong
   ship as `biolink:treats`, 13 of them at `medic_pair_reliability: EXCLUDED`. Exporter-only.
3. **#100 truncation flag** — set it whenever the stored text is shorter than the section, not only
   on a cap hit. 50 edges end mid-cross-reference unflagged. **#97 explicitly depends on this**
   being reliable, so it precedes the contraindication work.
4. **#99 interim** — route ATC J07 / "vaccine" / "toxoid" to `preventative_for_condition` (46
   mechanical edges) and set `medic_is_diagnostic_agent` from product class.
5. **#89 interim** — demote a disease introduced by "in patients with / associated with /
   following / due to" when another condition is the object of "treatment/prophylaxis of".
6. **#88 / #91 interim gate** — rate any pair supported *only* by multi-ingredient labels LOW.
   This is the single highest-leverage line in the set: a 65%-wrong slice stops being HIGH.

### Phase 2 — identifier and node hygiene (weeks)

Independent of extraction, so it can run in parallel with Phase 1. It also shrinks Phase 3's
surface: fewer bogus ingredient nodes means less fan-out to attribute.

7. **#95 counter-ions, metabolites, salt forms** — 72 ion nodes (292 assertions), 8 metabolite
   nodes (56), ~197 salt nodes split from their parents. Fix first in this phase: these nodes are
   what #94 then reports as unresolvable, and #88's validator needs salt/metabolite normalisation
   to match a named ingredient against the label.
8. **#94 717 drug ids unknown to the SRI Node Normalizer** — 3,448 assertions (28%). The largest
   *consumer-facing* loss in the set: any graph joining on ids drops these edges, and they are the
   biologics (adalimumab 143 assertions, denosumab 109, rituximab 89). Pin ChEBI/DRON, stop DRON
   clinical-drug forms beating ChEBI, de-duplicate one-drug-two-ids.
9. **#96 disease nodes** — 91 unnamed UMLS stubs used by 5,086 edges, 66 MeSH thesaurus inversions,
   and UMLS/HP chosen where MONDO exists (UMLS-grounded edges 38.6% wrong vs MONDO 22.1%). The
   unnamed-stub half is a one-line fallback to `original_object`; the vocabulary-preference half is
   a matcher loop-order fix.

### Phase 3 — the structured extraction contract (the main event)

10. **#88 + #89 + #90 + #99 + #97 as one prompt change.** Return per claim:
    `target_condition`, `attributed_to` + `basis`, `disease_context`, `population`,
    `temporal_or_procedural_context`, `co_administered_with`, `predicate`, and a verbatim snippet
    per field. Only `target_condition` becomes the object; the rest become Biolink qualifiers
    (`disease_context_qualifier`, `population_context_qualifier`, `sex_qualifier`,
    `temporal_context_qualifier`). Default a multi-ingredient claim to the product, never to a bare
    per-ingredient `treats`. Fix the EMA/India `results[0]` direction too.
11. **Put the qualifiers in the merge key** (#90.3, #24's harder half). This is what resolves most
    of #93.2's 34 self-clashes without demoting either side — formoterol is genuinely indicated for
    asthma maintenance and contraindicated for acute attacks.
12. **One re-extraction run** serving Phase 3 and Phase 4. Budget it once.

Gold standards already exist and should be wired in as tests before the prompt changes:
the 102 curated ingredient sets from the abandoned pilot (~1,000 attributed claims, PRs #70–#87),
`fp_audit.json` verdict `FP1_setting` (38 cases), and
`contraindication_sample_audit.tsv` (18 cases, 12 still live).

### Phase 4 — recall (#98)

13. After extraction is correct, not before: re-extracting with the current prompt reproduces the
    current errors. Then treat `previous_release_dropped_but_correct.tsv` (112 audited-correct pairs
    we no longer assert) as a regression set, and work `recall_gap_candidates.tsv` (~1,300
    label-confirmed DAKP indications for drugs we already cover).
14. Check #65's Limitations-of-Use splitting and the DailyMed section selection against the
    worst-hit drugs (lidocaine −122, iodine −83, hydrocortisone −82, dexamethasone −73).
    Our own gap analysis points at a second cause Kevin's does not: the CHEBI-only drug namespace
    makes 31% of v1's pairs structurally unrepresentable. Phase 2 item 8 is the prerequisite.

### Phase 5 — re-ground the reliability tier (#91)

15. Last, because it needs a positive signal to exist before it can require one. By then we have
    two: ≥2 regulators (7.2% wrong vs 32% for one — stop *lowering* the tier for it, #61) and
    single-ingredient support (#88). Add the per-assertion verification question — "does this text
    state that the drug treats this disease, or does the disease appear only as population,
    symptom, co-ingredient indication or negation?" — which runs on our own stored verbatim text
    with no external fetch, and is the only gate that can see the error classes the lexical checks
    pass.

## Cross-cutting notes

- **The biologics problem has two faces and one remedy.** #94 says the ChEBI/DRON ids we mint for
  adalimumab, denosumab and rituximab are unknown to the Node Normalizer; our gap analysis says v1's
  UNII ids for the same drugs are unrepresentable in the new schema. Both are the absence of a drug
  identifier *policy*. Settle it once, in Phase 2.
- **Do not re-run the extraction before Phase 3.** Kevin's measurements show the 2026-10-13
  re-extraction removed 20 of 76 audited errors and 37 of 221 audited-correct pairs — the same rate.
  Re-extracting without a prompt change is churn, and it costs a full LLM run.
- **What the audit cannot see.** #90's dropped-context half and #99's prevention-as-treats are both
  scored *correct* by the current rubric, so they do not appear in the 25.8%. The true error rate is
  higher than the headline. #90.4 proposes the rubric change; it should land with Phase 3 so the
  improvement is measurable.
- **#91's HIGH tier is the consumer-facing headline.** Until Phase 5, anyone filtering on
  `medic_pair_reliability: HIGH` keeps 93% of pairs and the full 26% error rate. The Phase 1 item 6
  gate is a partial mitigation; say so in the release notes rather than letting the tier imply more
  than it delivers.

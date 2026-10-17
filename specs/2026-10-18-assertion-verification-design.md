# Per-assertion verification: giving HIGH a positive signal to earn — Design Spec

**Status:** draft for review · **Date:** 2026-10-18 · **Issue:** [#91](https://github.com/monarch-initiative/medic/issues/91)
· **Depends on:** #64 (verbatim section text per assertion), #61 (per-assertion scoring)
· **Supersedes nothing;** extends `src/medic/reliability.py`

## 1. The problem, in one measurement

From the external 300-edge blind label audit on the build it sampled:

| slice | n | wrong |
|---|---:|---:|
| all judged edges | 291 | 25.8% |
| `medic_pair_reliability: HIGH` (93% of pairs) | 277 | **26.7%** |
| MEDIUM | 8 | 12.5% |
| LOW | 6 | 0% |

HIGH *was* the whole population, at the population error rate. A consumer filtering on it kept
93% of the pairs and all of the errors, which is the same as not filtering.

Phase 1's gates helped: HIGH is now 66.0% of edges rather than 78.6%, and the combination
(#88) and context (#89) gates demote the two slices the audit measured as worst. But both gates
fire on a **structural** property — "came from a multi-ingredient label", "was flagged as
context at ingest" — so they move whole populations, including the right edges inside them.
Roughly 548 of the 1,567 combination-capped pairs are correct by the audit's own rate.

What is still missing is any signal that separates edges *within* a slice. Every error class the
audit found passes the existing gates, because they all pass the lexical test: the disease name
really is in the text.

## 2. What to add

One question, asked of the stored sentence, per assertion:

> Does `supporting_text` state that `drug` treats/prevents/diagnoses `disease` — or does
> `disease` appear only as the patient population, the setting, a symptom, a co-ingredient's
> indication, or inside a negated clause?

Three outcomes: confident **yes**, confident **no**, not confident.

This runs on MeDIC's own stored text. #64 put the verbatim section text on every assertion, so
there is no external fetch, no new source, and no dependency on a URL still resolving.

## 3. Why this is worth building

#91 did the experiment rather than asserting it would work. A cheap decision model was asked
exactly this question over independently fetched FDA/EMA text and scored against the blind
reviewers, across five separately drawn samples:

| sample | confident *yes* | wrong | confident *no* | wrong | not confident | wrong |
|---|---:|---:|---:|---:|---:|---:|
| MeDIC 2026-10-13 | 166 | 1.8% | 27 | 100% | 100 | 47% |
| MeDIC 2026-10-01 | 168 | 2.4% | 18 | 100% | 112 | 49% |
| medic-ingest 2026-06 | 158 | 0.6% | 33 | 97% | 108 | 41% |
| DAKP 1.16.0 | 84 | 1.2% | 35 | 80% | 179 | 38% |
| DAKP 2026-04 | 126 | 0.8% | 29 | 97% | 145 | 52% |

A confident *yes* is wrong 0.6–2.4% of the time. A confident *no* is wrong 80–100% of the time —
i.e. it identifies errors nearly perfectly. That it replicates across two different knowledge
bases and five samples is what makes it a property of the question rather than of one sample.

**The caveats belong in the design, not a footnote.** The reference is the blind reviewers, not
ground truth, and they share a rubric with the check (prevention counts as treatment; a generic
label indication covers a specific edge). The model is confident on only 40–66% of edges; the
rest are wrong at about the population rate. And it is weakest exactly where MeDIC's largest
errors are — roughly 10 of ~700 confident-*yes* edges were rejected by reviewers, mostly
co-ingredient and setting cases.

So this is **a filter, not a judge**, and the design must not let it become one.

## 4. How it maps to the tier

Add a gate, and let the existing `_aggregate_pair` max-across-assertions fold do the rest.

| verdict | effect on the assertion |
|---|---|
| confident **no** | cap at `LOW`, flag `verification_failed` |
| confident **yes** | permits `HIGH` — satisfies the positive-signal requirement |
| not confident | no opinion; the assertion scores as it does today |

Then change what HIGH requires. Today a lone unflagged FDA assertion is HIGH because nothing
objected (#60). It should instead need **one positive signal**, any of:

1. a second regulator asserting the same pair (≥2 regulators is 7.2% wrong against 32% for one —
   the strongest cheap signal already in the data, and #61 stopped the fold from discarding it);
2. support from a single-ingredient label (the complement of #88's 65%-wrong slice);
3. a confident *yes* from this check.

A pair with none of the three is MEDIUM at best. That is the actual fix for "HIGH is awarded for
absence of evidence".

**A confident *no* does not delete anything.** It caps the tier and sets a flag, for the same
reason `_context_gate` caps rather than excludes: a model's confident *no* is wrong up to 20% of
the time in the DAKP sample, which is far too often to delete a row. #62 settled that even a
*human* REJECT marks rather than deletes.

## 5. Shape of the implementation

- **Where:** a new `_verification_gate(record)` in `reliability.py`, along
  `_attribution_gate` and `_context_gate`, reading a verdict already stored on the assertion.
  The gate must stay a pure function of record fields, like every other gate — it must not make
  a network call during scoring, or `just determinism` becomes meaningless and the tier stops
  being reproducible from the committed data.
- **When the question is asked:** at ingest, next to the extraction, with the answer cached on
  the hash of (section text, drug, disease) and stored on the evidence row. Same pattern as the
  extraction cache, so a rebuild is free and the verdict is diffable in review.
- **Schema:** `verification_verdict` (enum: `yes` / `no` / `unsure`) and
  `verification_confidence` (float) declared on `EvidenceItem` **before** anything writes them.
  `EvidenceItem` is a closed class and the last three times a field was emitted undeclared it
  broke validation silently.
- **Provenance:** the verdict is a *decision about* a row, not a transform of a string, so it
  does not need a `PreprocessingRuleEnum` value. It does need the model id and prompt version
  recorded, because unlike the deterministic gates its answer can change without the data
  changing.
- **Cost:** one call per assertion, on text already in hand. #91 measured ≈$0.10 per 1,000
  edges with a cheap model. At ~12,000 edges that is a rounding error next to extraction.

## 6. How we will know it worked

`just audit-regression` already scores both directions against the 300-edge audit, so the
acceptance criterion is stated in advance:

- **HIGH's error rate must fall** below the population rate — that is the whole point.
- **Correct-edge retention must not fall.** Current state is 166/221 (75.1%) kept and 58/79
  (73.4%) of errors removed. A change that improves precision by demoting correct edges is not
  an improvement, and a one-sided metric would call it one.
- Report HIGH's **share** of pairs alongside its error rate. HIGH covering 40% of pairs at 5%
  wrong is a usable product; HIGH covering 95% at 25% is not, regardless of which has the better
  headline.

## 7. What this does not fix

The verification question reads one sentence about one pair. It cannot see that a drug's *other*
indications are missing, so it does nothing for recall — which is Phase 4's subject and, on
current measurement, the larger problem. It also cannot judge an edge whose disease object ships
unnamed (573 edges, 4.7%), because there is nothing to ask about.

Those should not be folded in here. This gate's job is to stop HIGH meaning "nothing objected".

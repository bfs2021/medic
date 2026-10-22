# Workplan — Kevin's open PRs as ideas, folded into the current state

Four open PRs by @kevinschaper (#101–#104) and nineteen closed ones (#69–#87) assessed one by
one against what this branch already does. Each entry says what the PR proposes, what we
already have, and what is genuinely new — because two of them overlap work I did
independently, and in both cases parts of his approach are better than mine.

**Status (2026-10-22): all four merged.** `eb750eb` #101, `0579229` #104 pinning,
`130dc38` #102, `620bcbf` #104 xrefs, `7a795f0` #103 — his commits preserved in history,
co-authorship on the merges. What changed from his versions, and the four open decisions this
closed, are in `WORKLOG.md` under 2026-10-22. Only item 6 below (the #69–#87 gold standard)
is outstanding. The *measured* effect of #102, #103 and the reliability cap needs a rebuild:
the products on disk predate all of it.

Companion to `plan.md` (the #88–#100 sequence, phases 1–5) and `WORKLOG.md` (what happened).

Current state for reference: error rate at HIGH **4.7%**, 21,180 indication pairs, 86,104
edges, v1 pair retention **33.9% strict / 46.3% hierarchy-aware**.

---

## 1. PR #103 — rank DailyMed labels per drug · **highest value, partly supersedes my fix**

**His diagnosis.** `acquire.py` kept one setid per drug, the "most recent" search hit, and
that picked the wrong label: ER tablets for hydromorphone (so no renal colic), liposomal
doxorubicin (no bone sarcoma), ipratropium/albuterol for albuterol, a *veterinary* label for
ivermectin, the ophthalmic for flurbiprofen. He traced 121 dropped-but-correct pairs to this.

**What we already did** (`7c49633`, `ad0f3a4`): keep *all* setids per drug rather than one,
and query every candidate name generic-first. 1,976 → 18,261 labels.

**Four things in his PR that ours does not have, and should.**

1. **Doctype filtering.** He queries per human doctype — `34391-3` (Rx) and `34390-5` (OTC) —
   so animal labels never enter the pool. **We download veterinary labels and then pay to
   extract them.** This cuts cost *and* raises quality; it is the single best idea in the
   four PRs.
2. **`pagesize=100`, not 20.** Ours is capped at the endpoint's 20. A drug with 50 labels
   gets an arbitrary 20, and without the doctype filter the good one can be crowded out by
   vet and OTC duplicates. So "we keep all" is **not** a superset of "he keeps the best 3" —
   that assumption of mine was wrong.
3. **A real bug, and it is in our code too.** His note: `published_date` ("Oct 14, 2026") was
   compared as a string. Verified in ours — `_setids_for_drug._date` returns `str(...)` and
   sorts lexically, so "Sep 3, 2026" ranks above "Oct 14, 2026". Latent while we keep all 20,
   **live in `_most_recent_setid`**, which still backs `--labels-per-drug 1`.
4. **Keep only XML that has a `34067-9` Indications section.** Avoids storing and extracting
   labels that cannot contribute.

Also worth having: his ranking heuristics (plain formulation over LIPOSOMAL / EXTENDED-RELEASE,
systemic over local, single-ingredient label for a single-drug query) as a *tie-break* for
which labels to keep when capping, and his correction that the endpoint returns only
setid/title/published_date/spl_version.

**Action.** Take 1–4 outright; they compose with our breadth rather than replacing it (filter
the pool, widen the page, fix the sort, drop sectionless labels, then keep what remains).
Re-run acquisition + ingest after. Expect the label count to *fall* while quality rises, and
the ~2,947 "no such drug" and 3,002 "not asserted" buckets to shrink.

---

## 2. PR #102 — ground salt forms to the parent moiety · **fills a gap we deferred**

**His diagnosis.** 863 drug nodes end in a salt word, all ChEBI, **617 of them beside a
parent node that also exists**. Cause: ChEBI has an exact entry per salt ("fentanyl citrate"
CHEBI:31602 beside "fentanyl" CHEBI:119915), so tiers 1–2 matched the salt verbatim and the
tier-4 `salt_ester_strip` rule never got a turn — it only fired when the index lacked the
salt (23 rows).

That is a better explanation than ours. `deferred.md` §2b concluded salt forms "need the
normalization index (#27), not an ingest change" and that none were DailyMed's. The first
half is wrong: it is a matcher **tier-order** problem, exactly like the vocab-major fix.

**What is new.** A drugs-only tier 0: if the literal ends in a salt word and the stripped
parent resolves, the parent wins with `salt_ester_strip` recorded. Plus an **inorganic-cation
guard** so sodium chloride, magnesium sulfate and the like are not stripped to a bare element
— otherwise saline grounds to sodium, which is our own #95 part-1 ion bug by a new route.
That guard is the detail that makes the idea safe, and I would not have thought of it.

**A separate finding in his notes, worth its own fix.** `salt_ester_strip` has certainty 0.90,
and `reliability.py` scores `conf >= 0.9` as HIGH, although the comment there says a salt
match should be MEDIUM. So a close match is currently reaching the top tier. He left it as a
decision; it should be made.

**Action.** Take the tier-0 rule and the cation guard. Decide the 0.90/HIGH threshold
separately — it affects every closeMatch rule, not just salts.

---

## 3. PR #101 — cap enabler-partner indications at LOW · **fills #88 point 5**

Beta-lactamase inhibitors (avibactam, tazobactam, sulbactam, clavulanate, vaborbactam,
relebactam), cilastatin and cobicistat never own a treatment indication; an INDICATION on one
of them is the partner's claim credited to the wrong moiety. 63 `treats` edges.

`plan.md` lists point 5 as not started, and `comment_issue88` said so. This closes it with a
hand-editable `mappings/enabler_partners.tsv`, an `_enabler_gate` returning LOW, and the
first actual detector for the `wrong_pairing` assertion flag — which `schema/provenance.yaml`
had been declaring with no detector at all.

Two judgements in it I'd keep: **ritonavir is deliberately absent**, because a name cannot
distinguish a boosting dose from a treatment dose; and the gate is independent of the flag,
so a record scored without the merge still gets the same verdict.

**Action.** Take as-is. It will need a rebase onto `_attribution_gate` / `_context_gate`,
which he anticipated. Note the OTC half of point 5 ("each partner owns its own symptom
class") is explicitly *not* in it and remains open.

---

## 4. PR #104 — joinable xrefs and pinned ontologies · **overlaps our UNII work, extends it**

**What we did** (`9a73efd`): emit the SPL-asserted UNII as a drug-node xref. 1,367 nodes.

**What his adds.** Normalising the Pharos synonyms MeDIC *already holds* to Biolink-declared
prefixes at write time — ChEMBL → `CHEMBL.COMPOUND` (1,970), DrugCentral (1,789), PubChem →
`PUBCHEM.COMPOUND` (1,610), unii → `UNII` (1,309), GTOPDB (800) — and dropping the non-ids
(`pt:` labels, LyCHI hashes). That is far broader xref coverage than UNII alone, from a cache
we already have, with no new fetch.

**The correctness point I should heed.** He puts them on a new `Drug.source_xrefs` slot rather
than `alternate_ids`, because Pharos matches by label and RxNav by approximate match — these
are cross-references the *source* holds, not identities MeDIC established — and publishes
them as SSSOM `skos:closeMatch` while `alternate_ids` stays `exactMatch`. Our UNII is
source-*asserted* (the label states it on `<activeMoiety><code>`), so `exactMatch` is
defensible, but the distinction is real and ours does not make it.

**Ontology pinning** is the #94 ask we left untouched: an `ontologies:` table with versioned
URLs (mondo 2026-10-06, hp 2026-09-01, dron 2026-09-19, chebi rel 255 — the newest pinnable,
since ChEBI serves no dated-release JSON), `build.py` refusing an unpinned source, versions
recorded in the index `meta` and reported in KGX metadata. That last part matters to us
specifically: `scripts/v1_pair_gap.py` now reads `cache/ontologies/mondo.json` for the
hierarchy, so an unpinned Mondo means the comparison silently shifts between runs.

**Flagged consequence**, his words and worth repeating: ~9.5k Pharos-derived SSSOM rows change
predicate from exactMatch to closeMatch. Correct, and visible to any consumer filtering on
exactMatch.

**Action.** Take the Pharos normalisation and the pinning. Decide whether our UNII belongs on
`source_xrefs` (source-held) or `alternate_ids` (established) — I think the latter, since the
label asserts it, but it should be a recorded decision rather than a default.

---

## 5. PRs #69–#87 (closed) — the combination curation pilot · **still the best test set we have**

Nineteen PRs, ~1,000 hand-attributed claims over 102 ingredient sets. `plan.md` said these
"should be wired in as tests **before** the prompt changes". They were not, and #88/#89/#97
therefore shipped without the regression set that would score them — the single largest
process failure in this whole stretch.

They are closed, so the content is in branch history (`curation/combos-*`). The pilot also
found that agents were consistent when given sibling precedent, so the curated files double
as few-shot examples for the attribution prompt.

**Action.** Recover the YAML, wire it as a test over the same SPLs, and report "N of ~1,000
attributions agree". That converts #88's "the mechanism works" into a number.

---

## Order, and why

| | why |
|---|---|
| 1. **#103's items 1–4** | the date bug is live in our code; doctype filtering cuts cost *and* noise; both compose with what we have |
| 2. **#101** | smallest, self-contained, closes a named gap, gives `wrong_pairing` its detector |
| 3. **#104's pinning** | our own hierarchy-aware comparison now depends on an unpinned Mondo |
| 4. **#102** | 617 recoverable nodes, but needs the 0.90/HIGH threshold decision first |
| 5. **#104's Pharos xrefs** | broad coverage win, no fetch, but needs the exactMatch/closeMatch call |
| 6. **#69–#87 gold standard** | no new capability, but it is how we find out whether #88 actually worked |

Everything except item 6 is additive and gated the same way: `just audit-regression` must
show the error rate at HIGH holding at 4.7%, and `just v1-pair-gap` should move the strict
and hierarchy-aware retention in the same direction. A change that improves one by demoting
the other is not an improvement.

## What I got wrong that these PRs correct

Recorded because the pattern matters more than the items.

- **"We keep all 20, so we are a superset."** False. Without the doctype filter and with
  `pagesize=20`, the useful label can be crowded out by veterinary and duplicate OTC entries.
- **"Salt forms need #27's normalization index, not an ingest change."** Wrong half. It is a
  matcher tier-order problem, the same shape as the vocab-major bug we did fix.
- **The string date sort.** Ours has it. Found by reading his PR, not by testing ours.
- **Pharos synonyms were sitting in a cache we already had**, unnormalised and unused, while
  we went and built a new RxNorm table for coverage. The new table is still worth having, but
  the cheaper win was already on disk.

# Claims and qualifiers: decision guidance

Supplements `../SKILL.md`. The schema (`src/medic/schema/combination.yaml`) is
authoritative for slot names and enums.

## Relation decision table

Decide from the label's own verb phrase, not from what you know about the drug.

| Label wording (examples) | Section | `relation` | Biolink (reference only) |
|---|---|---|---|
| "indicated for the treatment of", "indicated for", "management of", "to improve glycemic control in adults with" | INDICATIONS | `TREATS` | `biolink:treats` |
| "reduce the risk of", "reducing the combined risk of", "prevention of", "prophylaxis of", "decreasing the risk of developing" | INDICATIONS | `PREVENTS` | `biolink:preventative_for_condition` |
| "relief of", "temporary relief of", "symptomatic treatment of", "for the relief of symptoms of" | INDICATIONS | `RELIEVES_SYMPTOMS` | `biolink:ameliorates_condition` |
| "contraindicated in patients with", "do not use in patients with" (inside Contraindications) | CONTRAINDICATIONS | `CONTRAINDICATED` | `biolink:contraindicated_in` |

Tie-breaks:

- "maintenance treatment of bronchospasm" → `TREATS` (maintenance is still
  treatment). "Reduction of exacerbations of COPD" → `PREVENTS` (object:
  the exacerbation, context: COPD), if a fitting term exists; otherwise
  leave the object unbound with a `term_gap`.
- "Treatment of symptoms of X" → `RELIEVES_SYMPTOMS`, object X.
- "As an adjunct to diet and exercise" is context, not a relation change:
  record it verbatim in `in_combination_with` when it names a co-therapy, or
  leave it in the quote and mention it in `notes`.
- When a sentence carries both an indication cue and a negative/limiting cue,
  the claim follows the indication cue and the quote must not be the limiting
  sentence. Limitations of Use never produce a claim.

## Descriptors: the label's words first, a term when one fits

Every concept in a claim — `object`, each `object_specialization`, every
context qualifier, `line_of_therapy`, each `in_combination_with` — is a
Descriptor (the dismech pattern):

```yaml
disease_context:
  text: chronic stable angina pectoris     # verbatim, must occur inside the quote
  term: {id: HP:0001681, label: Angina pectoris}   # optional
  binding: BROADER                         # EXACT | BROADER | NARROWER, required with a term
# or, when nothing fits:
  term_gap: 'searched MONDO/HP "stable angina": none more specific than angina pectoris'
```

- `text` is the record. It is always there, always verbatim.
- Bind a term when one fits. `binding` says how it relates to the text:
  `EXACT`, `BROADER` (the term is more general than the text: no term as
  specific exists), `NARROWER` (the term is more specific than the text, e.g.
  text "allergy" → drug allergy, justified by the rest of the quote).
- When nothing fits, leave `term` out and write `term_gap`: what you searched
  and what is missing (a term-request URL if you filed one). Never force a
  distant term to avoid a gap.
- The validator anchors on every word span of each descriptor's `text` that
  names a term (label or exact synonym; for diseases and anatomy plurals are
  folded, for chemicals they are not, since "benzimidazoles" is the class and
  "benzimidazole" the compound) or a grounding-store row. Binding a term
  *broader* than one the words name is an error ("acute myocardial infarction"
  bound to myocardial infarction); so is a term unrelated to the single thing
  the words name. The binding must agree with what the *whole* text names:
  EXACT if it is that term, NARROWER if you bound a descendant; a term that
  only part of the text names can be EXACT or BROADER, never NARROWER.

## What is a claim object

A claim object is a **condition**: a disease (MONDO) or, when the label names a
sign/symptom rather than a disease, a phenotype (HP). Accuracy first: bind the
term that says what the label says, whichever of the two it is in; fitting it
to Biolink is the export's job.

Not claim objects (record nothing, mention in your summary if notable):

- populations ("patients who require aspirin for secondary prevention of
  cardiovascular and cerebrovascular events");
- procedures ("use in patients who have undergone revascularization
  procedures");
- outcomes that are not conditions ("death", "hospitalization");
- pharmacological goals with no condition ("bowel cleansing prior to
  colonoscopy" has no disease; skip unless the schema later allows procedures).

## One claim per condition and per context

- A list ("reducing the risk of death and nonfatal MI in patients with a
  previous MI or unstable angina pectoris") yields one claim per
  (object, context) pair: MI | previous MI, MI | unstable angina pectoris.
  `disease_context` is single-valued. Claims that differ only in context are
  not duplicates.
- The quote must be one contiguous verbatim span that contains every
  descriptor `text` and (when attributing) the ingredient's name. If
  that span is long because the list sits between them, keep it long; never
  splice two pieces together with "...".
- Several products in one file may carry the same claim; each product block
  carries its own claims from its own document.

## Attribution

`attributed_to` is a list of `{ingredient, basis}`. There are exactly two
bases, and the validator checks the wording of each (with the product's own
name removed from the quote first, since a generic name lists every
ingredient):

| basis | when | example |
|---|---|---|
| `COMPONENT_STATEMENT` | the label assigns the claim to the component; the quote must contain "the <ingredient> component" | "The omeprazole component of ... is indicated for decreasing the risk of ... gastric ulcers" |
| `STATED_REASON` | the label names the ingredient as the cause; the ingredient must appear in the clause after a causal cue (because, due to, caused by, may cause, since, related to, associated with) | "... because of the risk of Reye's syndrome with concomitant use of aspirin"; "... due to the expected significantly increased grazoprevir plasma concentration" |

Not attribution:

- an allergen mention ("hypersensitivity to aspirin, omeprazole ...",
  "allergy to aspirin and other NSAIDs") — that is what the patient is allergic
  to; record it in `object_specialization` instead;
- a cause named only by role ("since the anticholinergic component may
  produce mydriasis") — the label never names the ingredient; leave it
  unattributed and say in `notes` which ingredient it points to;
- Clinical Pharmacology or Mechanism of Action text — not the claim's section;
- your own pharmacology. A product-level claim stays unattributed even when
  the attribution looks obvious. That is the whole point of the record.

## Object specialization (post-composition)

`object_specialization` composes the object with the agent(s) it concerns,
when no single term says both (Biolink: `object_specialization_qualifier`):

| Label | object | object_specialization |
|---|---|---|
| "known allergy to aspirin and other NSAIDs" | text "allergy" → drug allergy (NARROWER) | NSAIDs → CHEBI non-steroidal anti-inflammatory drug |
| "hypersensitivity to aspirin, omeprazole, substituted benzimidazoles, or ... excipients" | drug allergy | benzimidazoles only (the own ingredients and excipients are dropped) |
| "hypersensitivity to other sulfonamide-derived drugs" | drug allergy | sulfonamide |
| "any other beta-lactams (e.g., penicillins and cephalosporins)" | drug allergy | beta-lactam antibiotic |
| "history of angioedema related to previous treatment with an ACE inhibitor" | angioedema | ACE inhibitor |
| "aspirin associated gastric ulcers" | gastric ulcer (BROADER) | aspirin |

Each specialization is a Descriptor (CHEBI usually; `term_gap` when, e.g.,
"any of the excipients" has no term). Several agents → several entries.

## Qualifiers

Every qualifier `text` is verbatim and must occur inside `quote`.

| Slot | Use for | Ontology | Example `text` |
|---|---|---|---|
| `disease_context` | The condition the claim is restricted to, or the patient's existing disease in secondary prevention | MONDO or HP | "associated with COPD", "previous MI", "chronic stable angina pectoris" |
| `anatomical_context` | A body site the label restricts to | UBERON | "of the skin", "ophthalmic" only if it names a site |
| `life_stage` | A named life stage | HsapDv (UBERON stages also fine) | "pediatric patients", "neonates" |
| `age_range` | A numeric age bound | none (`min_years` / `max_years`) | "12 years of age and older" → min 12 |
| `sex` | A sex restriction stated by the label | PATO | "postmenopausal women" (also consider life stage) |
| `line_of_therapy` | Prior-therapy requirement | Descriptor, usually unbound | "after failure of metformin" |
| `in_combination_with` | Named co-therapies | Descriptors, CHEBI when a specific agent | "in combination with other antiretroviral agents" |

The ontology column is the usual choice, not a rule: the validator only warns
on an unusual one. Pick the term that is accurate.

Guidance:

- Prefer `age_range` for numeric thresholds; use `life_stage` only for a named
  stage. Both may be set when the label gives both.
- Do not turn **alternative** risk criteria into a hard restriction: "at risk
  ... due to age (≥ 55) or documented history of gastric ulcers" is an OR, so
  no `age_range`; put it in `notes`.
- Adults-only ("in adults") → `age_range: {min_years: 18, text: "adults"}`
  only if the word occurs in the quote; otherwise omit.
- Do not encode severity or disease stage as a qualifier unless there is a
  specific term for it; prefer the specific term as `object` instead
  ("moderate to severe plaque psoriasis": check `terms search` for a specific
  term; if none, bind plaque psoriasis with `binding: BROADER`).

## Contraindications edge cases

- **Allergy to the product's own ingredients** ("hypersensitivity to any
  component of this product", "hypersensitivity to aspirin, omeprazole, ... or
  any of the excipients"): **not curated**. Skip it (the validator errors when
  every specialization is an own ingredient or "component/excipient").
- **Cross-sensitivity to other agents** ("allergy to aspirin and other
  NSAIDs", "other sulfonamide derivatives", "atropine and its derivatives"):
  curate it — object drug allergy (or the named reaction), the other agents in
  `object_specialization`. Not attributed: an allergen is not a cause.
- **A named syndrome** ("the syndrome of asthma, rhinitis, and nasal polyps"):
  one claim for the syndrome, never split into its parts.
- **Drug-interaction contraindications** ("co-administration with
  rilpivirine-containing products"): no condition object; skip and list in
  summary.
- **Pregnancy / lactation**: the condition is pregnancy; bind only if a
  suitable term is confirmed via `terms info`, otherwise skip and report.
- A contraindication stated only under Warnings and Precautions is never a
  `CONTRAINDICATED` claim.

## Term choice

- Most specific term the label wording supports, with the `binding` that says
  how it relates to the text.
- `label` copied from `terms info` output, exactly.
- When the grounding store maps the text to another ontology (UMLS) or to
  `sssom:NoTermFound`, choose the term via `terms search`; the validator warns
  and you mention it in your summary.
- `term_gap` records the `terms search` queries you ran when nothing fits.

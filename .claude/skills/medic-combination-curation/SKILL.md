---
name: medic-combination-curation
description: >
  Curate combination drug products (two or more active ingredients in one
  product, e.g. aspirin/omeprazole, YOSPRALA) into hand-curated
  kb/combinations/<slug>.yaml files from their regulatory label (DailyMed SPL).
  Use when asked to curate a combination product or a fixed-dose combination,
  to work the combination worklist ("curate the next combination", "do the next
  3 combos"), to attribute a combination label's indications or
  contraindications to individual ingredients, or to fix validator failures in
  kb/combinations/. Not for single-ingredient labels or kb/research/ files.
---

# MeDIC Combination Product Curation

## Why this exists

A combination label makes its claims about the **product**. The pipeline used
to copy every claim onto every ingredient, which published facts no regulator
stated: the aspirin/omeprazole label yielded "omeprazole → myocardial
infarction" and "aspirin → gastric ulcer" (backwards: omeprazole is in the
product to reduce aspirin-associated ulcers). Multi-ingredient DailyMed labels
are ~277 documents but ~40% of indication rows, so this error class is large.

Each file under `kb/combinations/` is a **source-of-truth record**: the claim
stays on the combination, carries the verbatim label sentence it rests on, and
is attributed to an ingredient only when the label itself says which
ingredient does it. A deterministic validator checks every file.

Schema: `src/medic/schema/combination.yaml` (LinkML). Read it before your first
file in a session; it is authoritative over this skill if they ever disagree.

## Non-negotiable rules

1. **Verbatim or nothing.** `quote` and every descriptor `text` are copied character-for-character from the label section the helper
   prints. Never paraphrase, never reorder, never fix typos or the label's
   footnote artifacts (e.g. `TABLETSA`). Whitespace may differ; nothing else.
2. **Attribution only when stated, with the label's words as evidence.** Add
   an `attributed_to` entry only when the label itself ties the claim to the
   ingredient. Each entry is evidence in dismech's pattern:
   - `snippet`: the label's own words that make the tie, copied verbatim from
     the label element that holds the quote, in the same sentence as the
     claim's object — or a list lead ending in ":" ("Atorvastatin is
     indicated:") that governs the object's list item — up to the next
     sentence that makes a claim of its own, a DESI classification, or a
     warning. When the item appears in both the Highlights and the full text,
     the validator checks the first copy (Highlights), so use the lead from
     that copy ("The omeprazole
     component of ...", "because of the risk of Reye's syndrome with
     concomitant use of aspirin", "Clarithromycin is contraindicated",
     "contains atorvastatin and is therefore"). Keep quotes to the claim's own
     item; never widen a quote just to reach a snippet;
   - `explanation`: one or two sentences on how the snippet ties *this* claim
     to *this* ingredient, without adding conclusions the words do not state;
   - `basis`: the kind of statement — `COMPONENT_STATEMENT` (the label assigns
     the claim to the component), `STATED_REASON` (it names the ingredient as
     the cause), `INGREDIENT_SUBJECT` (the ingredient is the grammatical
     subject of the claim's verb, on any product, single or co-packaged; a
     lead-in such as "In patients with CAD, amlodipine is indicated ..." does
     not change that — start the snippet at the subject, "amlodipine is
     indicated ..."; a brand name such as LIPITOR is not the ingredient),
     `INTERACTING_INGREDIENT` (the label names the ingredient as party to a
     contraindicated co-administration, "Concomitant administration of
     clarithromycin and colchicine is contraindicated"; only with the other
     agent in `when_coadministered_with`).
   The validator checks the snippet mechanically, as errors: verbatim, from
   the quote's element, in the object's sentence (or its list lead), naming the
   ingredient somewhere other than the claim's own condition and agent wording
   ("aspirin associated gastric ulcers" attributes nothing to aspirin) and the
   product's own name ("Ipratropium Bromide and Albuterol Sulfate Inhalation
   Solution is indicated" names no ingredient), and showing its basis's
   wording. Whether the words really attribute the claim is the human
   reviewer's call, made from your snippet and explanation, so make them
   checkable. An allergen mention is not attribution, and neither is
   pharmacology you know. If the label speaks of the product as a whole, omit
   `attributed_to`.
2b. **Every claim explains itself.** `explanation` (required) says how the
   quote states this claim: which words give the relation, the object, and
   each qualifier, and any reading you made (a coordination member, a risk
   factor, a symptom of a named condition). No conclusions beyond the words.
3. **One claim per distinct condition** (and per distinct context, see
   `references/claims-and-qualifiers.md`). Never one claim per ingredient
   copied from a product-level sentence.
3b. **Words first, terms second.** Every concept is a Descriptor: verbatim
   `text` always; a `term` with its `binding` (EXACT / BROADER / NARROWER) when
   one fits; otherwise a `term_gap` saying what you searched. Never force a
   distant term to avoid a gap, and never bind a term broader than one the
   label's words name. The validator checks every descriptor's term against
   its own text, and checks the binding: EXACT when the whole text is the
   term's label or exact synonym; NARROWER when you bind something more
   specific than that; never NARROWER for a term that only part of the text
   names ("MI" in "nonfatal MI").
3c. **No allergy to the product's own ingredients.** "Hypersensitivity to any
   component / to aspirin, omeprazole or the excipients" is not curated.
   Cross-sensitivity to *other* agents ("aspirin and other NSAIDs") is, with
   the agents in `object_specialization` — every allergy contraindication must
   name its agents there. When one sentence lists both, curate a claim
   carrying only the other agents. "Allergy" is judged by words *and* by the
   term's place in MONDO/HP (anything under MONDO allergic disease or HP
   Allergy; hereditary angioedema and asthma are not allergies). Other drug-caused
   conditions may name the product's own ingredient as their agent
   ("thrombocytopenia with use of trimethoprim"): that is curated.
4. **Section discipline.** `INDICATIONS` claims come from Indications and
   Usage. `CONTRAINDICATED_IN` claims come from the Contraindications section
   only, never from Warnings and Precautions, Boxed Warning, or a Limitations
   of Use paragraph.
5. **Limitations of Use are not indications** and not contraindications. A
   sentence like "not for use as the initial dose of aspirin therapy during
   onset of ... acute myocardial infarction" produces no claim.
6. **No negated claims.** "Has not been shown to reduce the risk of
   gastrointestinal bleeding" asserts nothing positive. The validator applies
   MeDIC's negation cues; do not try to route around them.
7. **Never invent identifiers.** `id`, `name` and the file slug are printed by
   the label helper; copy them. They are computed from ChEBI labels, so aspirin
   appears as `acetylsalicylic acid`. Ontology ids come from a term lookup run in
   this session (`uv run python -m medic.curation.terms search|info ...`), never
   from memory. Ingredient CURIEs come from the helper, never from you.
8. **Everything you write is `DRAFT`.** Record `curation_status: DRAFT` and
   every claim `status: DRAFT`. Only a human sets `REVIEWED` or `REJECTED`.
9. **Stay in your lane.** Write only `kb/combinations/*.yaml` and their
   `history/combinations/` records. Never write under
   `kb/indications/`, `products/`, `exports/`, never edit cached SPL XML, the
   worklist, or `mappings/*.sssom.tsv`. If a mapping looks wrong, say so in
   your summary; do not fix it.
10. **Source isolation.** A product block holds claims from its one pinned
    document only (DailyMed → `jurisdiction: USA`). Do not merge in text from
    another product's label, another jurisdiction, or your own knowledge.

## Workflow

### Step 1: Pick a combination

```bash
head -20 curation/combination_worklist.tsv
```

Columns: setid, ingredients (as the pipeline grounded them), n_ingredients,
pipeline_rows / indication_rows / contraindication_rows (the fan-out this file
replaces), pilot, status, indications_excerpt. Ordered by priority. Take the
first `pilot=yes` row not already covered by a file in `kb/combinations/`,
unless the user named a product or setid. `pilot=no` rows (5+ ingredients:
nutrition, vaccines, homeopathic mixes) are out of scope.

**Duplicate preflight.** One file covers one *ingredient set*. Different
brands/strengths with the same ingredients are separate `products:` blocks in
the same file. Before creating a file, check whether the slug already exists:

```bash
ls kb/combinations/ | grep -i '<ingredient>'
```

If it exists, add a new product block to it (or update the existing block if
the setid matches) rather than creating a second file.

Skip and report (do not curate) when the label is not a therapeutic
combination: kits of unrelated products, diagnostic agents, allergen extract
mixes, vitamins/nutritional blends with dozens of ingredients. Say why.

### Step 2: Read the label with the helper

```bash
uv run python -m medic.curation.label <setid>
```

It prints title, SPL version, the structured active ingredients with their
grounded CURIEs, the combination `id` / `name` / file path, and the Indications
and Contraindications text split into typed spans (`[SECTION_TEXT]`,
`[SUBSECTION_HEADER]`, `[LIMITATION_STATEMENT]`). That printed text is your only
quotable source. Read both sections in full before writing anything. Labels
usually carry the text twice (full prescribing information and Highlights);
either copy is quotable. Quote from a single span where you can (the validator
warns when a quote crosses spans), and never from a `[LIMITATION_STATEMENT]`
span.

Start the file from the skeleton. `--write` creates it at its computed path
(creating `kb/combinations/` if needed, refusing to overwrite) with identity,
ingredients, curator block, product name and the pinned document filled in:

```bash
uv run python -m medic.curation.label <setid> --write
```

`--write` also writes a **pin** of the label version DailyMed serves
now: `curation/labels/dailymed/<setid>.v<version>.yaml`, the label's
ingredients and Indications/Contraindications text with the XML's checksum
(about 3 KB). Stage it with your file: the validator checks quotes against
the pin, never against a live download, so a later DailyMed revision cannot
change a verdict. Adding a product block to an existing file needs its own
pin: `uv run python -m medic.curation.label <setid> --pin`.

The skeleton's `curator_id` names this skill with the git blob id of its exact
text (`...SKILL.md#git-blob=<id>`). If the skill has uncommitted changes it
writes an `UNCOMMITTED` marker instead, which the validator rejects. The
history scaffold records the same reference for each session. All the tools
work from any directory.

If the file already exists (same ingredient set, another product), add a new
`products:` block to it instead (`--skeleton` prints the block to copy).

If an ingredient is printed as ungrounded, stop and report it; do not supply a
CURIE yourself (the validator compares your ingredient set to the label's).

### Step 3: Decide the claims

Work through the Indications section sentence by sentence, then the
Contraindications section. For each sentence ask:

1. Does it assert a relation between the product (or a named component) and a
   **condition**? Population descriptions ("patients who require aspirin for
   secondary prevention of cardiovascular and cerebrovascular events"),
   procedures ("revascularization procedures"), and outcomes that are not
   diseases ("death") are not claim objects. Note them, do not force them.
2. Which relation? `TREATS`, `PREVENTATIVE_FOR_CONDITION`, `AMELIORATES_CONDITION`, or
   (Contraindications only) `CONTRAINDICATED_IN`. See the decision table in
   `references/claims-and-qualifiers.md`.
3. Does the label assign it to a component, or name an ingredient as its
   cause? If yes, which one(s), on which basis, and which words (`snippet`)
   show it?
4. What context does the label attach (disease context, age, sex, life stage,
   anatomical site, line of therapy, co-therapy)? Each needs verbatim `text`
   inside the quote. A qualifier describes the treated patient: a sex or age
   on a claim about a fetus or conceptus ("neural tube defect") is wrong.
5. Does the label list examples of the condition ("e.g.", "including", "such
   as")? One claim for the category; the members go in its `examples`, never
   one claim per member — except a member with a restriction of its own
   ("migraine ... if over age 35", "now or in the past"): that member is a
   separate claim carrying the restriction, since `examples` cannot hold one.
   Two shapes are different:
   - **Risk-factor lists** ("a high risk of arterial or venous thrombotic
     diseases. Examples include women who are known to: smoke, ..., have
     uncontrolled hypertension"): the listed items are what is contraindicated,
     each as its own claim. The explanation says the item is contraindicated as
     a thrombotic risk factor; the heading condition is not a claim of its own.
   - **Coordinations** ("renal or hepatic impairment", "acute or severe hepatic
     failure", "QT prolongation or ventricular cardiac arrhythmia", "sinus
     congestion & pressure", "itchy, watery eyes"): one claim per member. Each
     claim's object (or disease context) `text` is the *whole* verbatim
     phrase — always, even when the member reads as a complete condition on its
     own ("deep vein thrombosis or pulmonary embolism"); the validator warns
     when a text is one item of a larger coordination — and its `member` is
     the one member it covers, as you expand it ("hepatic impairment",
     "sinus pressure", "itchy eyes"). The validator checks that the member is an
     expansion of the text (it reads "or", "and/or", "/", commas, "and", "&"),
     judges the term and binding against the member, and warns when a phrase's
     members are not all covered. A member names a condition on its own:
     "renal impairment", never the bare modifier "renal". Do not use `member`
     for a named syndrome
     ("asthma, rhinitis, and nasal polyps") — that is one condition.
   - **Exhaustive pairs** ("Liver tumors, benign or malignant", "inherited or
     acquired hypercoagulopathies", "arterial or venous thrombosis"): the
     alternatives together name the whole class, so this is one claim bound to
     the class term (liver neoplasm, thrombophilia, thrombosis), usually EXACT.
6. Does a contraindication apply only with a co-administered agent ("Do not
   co-administer aliskiren ... in patients with diabetes")? Object = the
   condition; the agent in `when_coadministered_with`. A contraindication with
   no condition at all (a pure drug-drug interaction) is out of scope.
7. Does the label qualify its indications as DESI "possibly" or "probably"
   effective? Set `evidence_status` on every such indication claim (the
   validator requires it whenever the label says so).
8. OTC symptom lists ("temporarily relieves these symptoms due to the common
   cold, hay fever or other upper respiratory allergies: runny nose, sneezing,
   ..."): one `AMELIORATES_CONDITION` claim per symptom, with the named
   condition(s) as `disease_context` — one claim per symptom per condition
   (cold, flu, hay fever) when several are named.
9. Pregnancy, lactation, contraception ("prevent pregnancy"): curate them.
   No configured ontology has these terms yet, so the object is unbound with a
   `term_gap`; do not skip them. When pregnancy qualifies another claim
   ("edema of pregnancy"), record it as an unbound `life_stage`.
   Outcome endpoints ("reduce the risk of hospitalization for angina") are
   PREVENTATIVE_FOR_CONDITION the condition they are for, text keeping the
   outcome wording, binding BROADER.
   Within one file, bind the same wording to the same term, coordinations
   aside (the validator
   warns otherwise).
10. Is it negated, or inside a Limitations of Use paragraph? Then no claim.

### Step 4: Look up terms

```bash
uv run python -m medic.curation.terms search MONDO "gastric ulcer" thrombo "von Willebrand"
uv run python -m medic.curation.terms search MONDO angina --limit 40
uv run python -m medic.curation.terms info MONDO:0001126 HP:0001681 CHEBI:35475
```

Search matches labels and exact synonyms at the **start of a word**, case-
insensitive: "thrombo" finds thrombosis, "stable angina" does not find
"unstable angina". Each argument is one query (quote multi-word ones); results
are capped at 15 unless you pass `--limit`, and an empty result says so.
`info` prints label, obsolescence, exact synonyms and `is_a` parents.
Prefixes: MONDO, HP, UBERON, HsapDv, PATO, CHEBI. Each call spends ~20 s
starting up (the CURIE converter), so **batch queries and ids into one call**. Where terms come from
(OBO releases by default, or an OAK selector such as `sqlite:obo:mondo`) is
set in `conf/curation_terms.yaml`; you do not need to change it.

- Accuracy first: MONDO or HP for the condition, whichever says what the label
  says; UBERON for anatomy, HsapDv for life stage, PATO for sex, CHEBI for
  agents in `object_specialization` and `when_coadministered_with`. An unusual
  choice only warns.
- Over-the-counter labels: the helper reads Drug Facts "Uses" as Indications
  and "Do not use" as Contraindications (`section: CONTRAINDICATIONS`).
  `product_name` may be empty when the SPL gives none; leave it empty.
- `term.label` is the ontology's canonical label exactly; the label's wording
  is the descriptor's `text`.
- Choose the most specific term the wording supports and set `binding`. If
  only a broader term fits, `binding: BROADER`. If nothing fits, no term and a
  `term_gap` naming the queries you ran.
- The validator anchors on every word span of the object's `text`: terms whose
  label or exact synonym it is, and `mappings/disease_grounding.sssom.tsv`
  rows. Binding a term **broader** than one of those anchors is an error
  ("acute myocardial infarction" bound to myocardial infarction). A term
  unrelated to a grounding-store row in the same ontology is an error: agree,
  or flag it for a human to add a `semapv:ManualMappingCuration` row (do not
  edit the SSSOM). Store rows in another ontology only warn.
- If the term lookup fails, stop and report it. Do not fall back to ids from
  memory, from `kb/indications/`, or from other files.

### Step 5: Draft the file

The skeleton already holds the header, curator block and product block; add
the claims. The model that did the work goes in the history record (Step 7b),
not in the file. Check the skeleton's `product_name` against the label and
keep it as the SPL gives it.

`notes` are for a human curator reading the record later: what judgement was
made and why (term choice, skipped sentences, ambiguity). Not a narrative of
your process.

### Step 6: Validate and iterate

```bash
uv run python -m medic.curation.validate kb/combinations/<slug>.yaml
# or: just validate-curation
```

Errors block; fix every one and re-run until there are none. Typical fixes:

| Error | Fix |
|---|---|
| quote not found in section | Re-copy from the helper output; check you used the right section; do not "fix" the label's typography |
| descriptor text not in quote | Widen the quote to a longer verbatim span, or correct the `text` |
| no term: record what was searched in term_gap | Bind a term, or write the `term_gap` |
| binding required when a term is given | Add EXACT / BROADER / NARROWER |
| X is broader than the label's own words | Bind the more specific term the validator names |
| binding must be EXACT / NARROWER, or cannot be NARROWER | Set the binding the message names; it follows from what the text names |
| the label's words name X, which is unrelated to Y | The term does not match its own text; rebind it |
| no term source is configured for PREFIX | Use a configured ontology (MONDO, HP, UBERON, HsapDv, PATO, CHEBI), or, if another is genuinely more accurate, report it so a human can add a source in `conf/curation_terms.yaml` |
| no committed label pin | Run `uv run python -m medic.curation.label <setid> --pin` and stage the pin, if DailyMed still serves that version; otherwise re-curate against the current version |
| binding warning: X is what only part of the text names | The remaining words may narrow the condition; use BROADER unless they don't |
| snippet must be verbatim from the label element that holds the claim's quote / is not in the sentence that names the claim's object | Copy the attributing words from the object's own sentence, or its list lead ending in ":"; if the label only says it elsewhere, it does not attribute this claim. Never widen the quote to reach it |
| snippet never names X (outside the product's own name) | The snippet must name the ingredient itself; a product name is not an attribution |
| never names X outside the claim's own condition and agent wording | The ingredient appears only as part of the condition or allergen; that is not an attribution |
| BASIS needs ... | The snippet must show its basis: "the X component", a causal cue, the ingredient opening the snippet as the verb's subject, or co-administration wording; reclassify or drop it |
| INTERACTING_INGREDIENT needs the other agent in when_coadministered_with | Record the co-administered agent |
| explain how the quote states this claim / the snippet ties the claim | Write the explanation; it is required |
| X fits neither the text nor any member of it | Add `member` and bind a term for that member, one claim per member |
| 'M' is not a member of 'TEXT' | Write the member as an expansion of the text's own words ("hepatic impairment" for "renal or hepatic impairment") |
| TEXT is a coordination; name the member (warning) | Add `member`; or, for an exhaustive pair or a named syndrome, say so in the explanation |
| TEXT has N members, but its claims cover only ... (warning) | Add the missing member's claim, or explain in notes why it is not curated |
| allergy contraindication must name the agents | Add the other agents as specializations, or drop the claim if they are all the product's own |
| drop the product's own ingredients | Keep only the other agents as specializations |
| curator_id is not a reference to this repository's SKILL.md with its git blob id | Commit the skill, regenerate the curator block with the label helper |
| the pin for X holds Y; pins are written by the label helper | Restore the pin from git; never edit pins |
| id / name / slug mismatch | Copy them from the helper again; never compute by hand |
| ingredient set differs from label | Use exactly the helper's ingredient CURIEs |
| claim negated | Drop the claim; it is not one |
| grounding store maps X to another MONDO id | Re-check with `terms info`; agree, or justify in `notes` and escalate |
| term not found / obsolete / label mismatch | Re-run `terms info`; use the replacement and copy the canonical label |
| quote comes from a Limitations of Use passage | Drop the claim |
| no history record | Do Step 7b |

Never edit the validator, the schema, the SSSOM, label pins
(`curation/labels/`) or downloaded labels to make
a check pass. Never report the validator as passing unless it finished and you
read its output.

### Step 7: Self-review in fresh context

Ask a subagent with fresh context to red-team the file against the label
output using the checklist at the end of this skill: every claim's relation,
attribution, and section; nothing in Limitations of Use; nothing missing from
Indications/Contraindications. Fix what it finds and re-validate.

### Step 7b: Record a history entry

Every file needs at least one append-only history record (format adopted from
dismech); the validator errors without one. It is where the session, the
agent and **the model that did the work** are recorded. Scaffold it, never
hand-write the path, timestamp or session id:

```bash
just new-history --kind combination --slug <file stem> --actor-name claude-code \
  --event CREATE --outcome changed \
  --summary "Create: <combination name>" \
  --agent-tool claude-code --model <your exact model id> \
  --sections ingredients,claims \
  --details "Curated from DailyMed setid <setid> v<version>. <judgement calls in one paragraph>."
just validate-history <path printed above>
```

Use `--event EDIT` when changing an existing file; every pull request that
changes a combination file must add a record for it (CI blocks otherwise;
only a maintainer's `history-skip` label waives that). Re-run the validator
after.

### Step 8: Hand off to the human

Summarise, per file:

- claims written (relation, object term + binding, specializations,
  attributions with basis, contexts) in a short table;
- every **warning** the validator printed, with your assessment;
- judgement calls: terms that were the closest rather than exact, sentences
  you deliberately did not turn into claims and why, any suspected wrong
  mappings in the SSSOM;
- anything you could not resolve.

Do not commit, push, or open a PR unless the user asks; the human opens PRs.
If asked to stage, stage only `kb/combinations/` and `history/` (never `git add -A`).

## Worked example: aspirin / omeprazole

Label (DailyMed setid `ae8f4eb4-b1eb-432e-b5ab-598c29996d6a`, generic of
YOSPRALA). Indications excerpt, as printed:

> ... is indicated for patients who require aspirin for secondary prevention of
> cardiovascular and cerebrovascular events and who are at risk of developing
> aspirin associated gastric ulcers. The aspirin component of ASPIRIN AND
> OMEPRAZOLE DELAYED-RELEASE TABLETSA is indicated for: reducing the combined
> risk of death and nonfatal stroke in patients who have had ischemic stroke or
> transient ischemia of the brain due to fibrin platelet emboli, reducing the
> combined risk of death and nonfatal MI in patients with a previous MI or
> unstable angina pectoris, ... The omeprazole component of ASPIRIN AND
> OMEPRAZOLE DELAYED-RELEASE TABLETSA is indicated for decreasing the risk of
> developing aspirin-associated gastric ulcers in patients at risk for
> developing aspirin-associated gastric ulcers due to age (≥ 55) or documented
> history of gastric ulcers. ... Limitations of Use: Not for use as the initial
> dose of aspirin therapy during onset of acute coronary syndrome, acute
> myocardial infarction or before percutaneous coronary intervention. Has not
> been shown to reduce the risk of gastrointestinal bleeding due to aspirin.

The label **does** say which component does what, so attribution is allowed.
(Term ids below are illustrative; confirm each with `terms info` in your
session before use.)

```yaml
# kb/combinations/acetylsalicylic-acid-omeprazole.yaml
id: MEDICNE:combo-c2a83bd8-e87f-5f4a-8892-22320c6ad63b   # from the helper
name: acetylsalicylic acid / omeprazole                  # from the helper
ingredients:                                              # from the helper
  - {id: CHEBI:15365, label: acetylsalicylic acid}
  - {id: CHEBI:7772, label: omeprazole}
curation_status: DRAFT
curators:                                                 # from the skeleton
  - curator_type: AI_AGENT
    name: MeDIC combination-curation skill
    curator_id: https://github.com/monarch-initiative/medic/blob/<sha>/.claude/skills/medic-combination-curation/SKILL.md
products:
  - source: DAILYMED
    jurisdiction: USA
    document:
      setid: ae8f4eb4-b1eb-432e-b5ab-598c29996d6a
      version: 1                 # the SPL version the helper printed
      title: ...                 # as printed
      url: https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=ae8f4eb4-b1eb-432e-b5ab-598c29996d6a
    product_name: aspirin and omeprazole delayed-release tab   # from the skeleton
    claims:
      - relation: PREVENTATIVE_FOR_CONDITION
        section: INDICATIONS
        object:
          text: nonfatal MI
          term: {id: MONDO:0005068, label: myocardial infarction}
          binding: EXACT
        quote: >-
          The aspirin component of ASPIRIN AND OMEPRAZOLE DELAYED-RELEASE TABLETSA is
          indicated for: reducing the combined risk of death and nonfatal stroke in
          patients who have had ischemic stroke or transient ischemia of the brain due
          to fibrin platelet emboli, reducing the combined risk of death and nonfatal MI
          in patients with a previous MI or unstable angina pectoris
        attributed_to:
          - ingredient: CHEBI:15365
            basis: COMPONENT_STATEMENT
            snippet: The aspirin component of ASPIRIN AND OMEPRAZOLE DELAYED-RELEASE TABLETSA
            explanation: >-
              The sentence assigns its indications, MI prevention among them, to the
              aspirin component.
        disease_context:
          text: previous MI
          term: {id: MONDO:0005068, label: myocardial infarction}
          binding: EXACT
        explanation: >-
          "indicated for reducing the combined risk of ... nonfatal MI" is prevention of
          MI; "in patients with a previous MI" is the disease context.
        status: DRAFT
        notes: >-
          Secondary prevention. The same sentence also covers patients with unstable
          angina pectoris; that context is a separate claim.
      - relation: PREVENTATIVE_FOR_CONDITION
        section: INDICATIONS
        object:
          text: aspirin-associated gastric ulcers
          term: {id: MONDO:0001126, label: gastric ulcer}
          binding: BROADER
        object_specialization:
          - text: aspirin
            term: {id: CHEBI:15365, label: acetylsalicylic acid}
            binding: EXACT
        quote: >-
          The omeprazole component of ASPIRIN AND OMEPRAZOLE DELAYED-RELEASE TABLETSA is
          indicated for decreasing the risk of developing aspirin-associated gastric
          ulcers in patients at risk for developing aspirin-associated gastric ulcers
          due to age (≥ 55) or documented history of gastric ulcers.
        attributed_to:
          - ingredient: CHEBI:7772
            basis: COMPONENT_STATEMENT
            snippet: The omeprazole component of ASPIRIN AND OMEPRAZOLE DELAYED-RELEASE TABLETSA
            explanation: The sentence assigns the ulcer-risk indication to the omeprazole component.
        explanation: >-
          "decreasing the risk of developing aspirin-associated gastric ulcers" is
          prevention of gastric ulcer, specialised to aspirin as the cause.
        status: DRAFT
        notes: >-
          gastric ulcer + specialization aspirin composes "aspirin-associated gastric
          ulcer"; no drug-induced gastric ulcer term exists. "age (≥ 55) or documented
          history" are alternative risk criteria, so no age_range is recorded.
      - relation: CONTRAINDICATED_IN
        section: CONTRAINDICATIONS
        object:
          text: allergy
          term: {id: MONDO:0000775, label: drug allergy}
          binding: NARROWER          # the term is more specific than the word "allergy"
        object_specialization:
          - text: NSAIDs
            term: {id: CHEBI:35475, label: non-steroidal anti-inflammatory drug}
            binding: EXACT
        quote: >-
          Patients with known allergy to aspirin and other nonsteroidal
          anti-inflammatory drug products (NSAIDs)
        explanation: >-
          A contraindication in patients allergic to NSAIDs (cross-sensitivity); aspirin
          here is an allergen, not an attribution.
        status: DRAFT
      - relation: CONTRAINDICATED_IN
        section: CONTRAINDICATIONS
        object:
          text: suspected viral infections
          term: {id: MONDO:0005108, label: viral infectious disease}
          binding: EXACT
        life_stage:
          text: Pediatric patients
          term: {id: HsapDv:0000264, label: pediatric stage}
          binding: EXACT
        attributed_to:
          - ingredient: CHEBI:15365
            basis: STATED_REASON
            snippet: because of the risk of Reye's syndrome with concomitant use of aspirin
            explanation: The label gives aspirin as the reason for the contraindication.
        quote: >-
          Pediatric patients with suspected viral infections, with or without fever,
          because of the risk of Reye's syndrome with concomitant use of aspirin
        explanation: >-
          Contraindicated in pediatric patients with suspected viral infections; "with or
          without fever" does not restrict it.
        status: DRAFT
```

Note what is **absent**, and why:

- No `aspirin → gastric ulcer` and no `omeprazole → myocardial infarction`:
  the label attributes each indication to one component.
- "known hypersensitivity to aspirin, omeprazole, substituted benzimidazoles,
  or to any of the excipients" mixes own ingredients with another class. The
  own-ingredient part (aspirin, omeprazole, excipients) is not curated; the
  cross-sensitivity to substituted benzimidazoles is, as a claim whose only
  specialization is CHEBI:22715 benzimidazoles. The NSAID cross-sensitivity is
  curated too, unattributed: aspirin there is the allergen, not a cause.
- No claim for "patients receiving rilpivirine-containing products": a drug
  interaction, not a condition.
- No claim from the product-level sentence ("patients who require aspirin for
  secondary prevention of cardiovascular and cerebrovascular events"): it
  describes a population, not one condition.
- No claim for "acute myocardial infarction" from Limitations of Use, and none
  for "gastrointestinal bleeding" (negated: "Has not been shown to reduce").
- The quote for the MI claim starts at "The aspirin component" so the
  attribution snippet sits inside the quote, even though the quote then has to
  run through the stroke item to stay contiguous. The snippet must be in the
  same sentence as the object (or be a list lead ending in ":" that governs
  it); a snippet from a neighbouring sentence does not count.
- The stroke claims (object stroke, contexts ischemic stroke / transient
  ischemia of the brain) and the unstable-angina and chronic-stable-angina
  claims would follow the same pattern.

## Common mistakes

| Mistake | Why it is wrong | Do instead |
|---|---|---|
| Copying a product claim onto every ingredient (attributing both, or one claim per ingredient) | Reproduces exactly the false facts this pilot exists to remove | Omit `attributed_to` unless the label assigns it or names the cause |
| Treating an allergen as an attribution ("allergy to aspirin and other NSAIDs" → aspirin) | The patient is allergic to it; it is not the cause of a claim | `object_specialization`, no attribution |
| Forcing a distant term to avoid a gap ("chronic stable angina" → myocardial ischemia) | The record says less than the label, silently | The accurate term (HP is fine) with `binding`, or a `term_gap` |
| Paraphrased or tidied quote ("is indicated to reduce MI risk") | Fails the substring check; destroys traceability (I-8) | Copy the label span exactly, artifacts included |
| Claims from Limitations of Use ("not for ... acute myocardial infarction" → TREATS or CONTRAINDICATED_IN) | It limits use; it neither indicates nor contraindicates | No claim; mention it in your summary if notable |
| Contraindications pulled from Warnings and Precautions or Boxed Warning | Those are risks, not contraindications | `CONTRAINDICATED_IN` only from the Contraindications section |
| Inventing CURIEs (ingredients, combo id, MONDO ids from memory) | Plausible wrong ids pass casual review and silently corrupt the KB | Helper for ingredients/id; `terms` lookup for every term, this session |
| Pharmacology-based attribution ("omeprazole is a PPI so it must be the ulcer part") | Attribution must be stated by the label | Leave unattributed; say in `notes` it looks component-specific |
| An explanation that argues beyond the words ("omeprazole protects the stomach, so ...") | The reviewer approves the label's words, not your pharmacology | Say only what the snippet or quote states |
| A risk-factor list curated as its heading ("thrombotic diseases") | The items are what is contraindicated | One claim per risk factor |
| One claim for "renal or hepatic impairment" bound to one of them | Silently drops the other member | One claim per member, each with the whole phrase and its `member` |
| Splitting an exhaustive pair ("inherited or acquired hypercoagulopathies") | The pair is the whole class; the split claims repeat it | One claim, the class term |
| Widening a quote to swallow a distant attribution | Hides that the attribution is not in the object's sentence | Item-sized quote; snippet from the object's sentence or its list lead |
| Bundling several conditions into one claim | Each condition is its own edge downstream | One claim per condition / context |
| Using the label wording as `term.label` | `label` must be the ontology label | Wording goes in the descriptor's `text` |
| Marking anything REVIEWED | Only humans review | `DRAFT` everywhere |

## Pre-handoff checklist

- [ ] File name, `id`, `name`, ingredients copied from helper output
- [ ] Every claim's `quote` copied from the printed section, right `section`
- [ ] Every descriptor `text` occurs inside the quote; every term has a
      `binding`; every unbound descriptor has a `term_gap`
- [ ] `attributed_to` only where the label assigns the claim or names the
      cause, each with a verbatim `snippet`, an `explanation` and the right
      `basis`; no own-ingredient allergy claims
- [ ] Every claim has an `explanation`
- [ ] Risk-factor lists, coordinations and OTC symptom lists split per item
- [ ] Nothing from Limitations of Use, Warnings, Boxed Warning; nothing negated
- [ ] Every term id from a `terms` lookup this session; labels canonical
- [ ] All statuses `DRAFT`
- [ ] History record scaffolded with your exact model id, and validated
- [ ] Validator run to completion (with term checks, not `--no-terms`), zero
      errors, warnings read and summarised
- [ ] Fresh-context self-review done
- [ ] Summary for the human lists warnings, judgement calls, skipped sentences

## Related

- `references/claims-and-qualifiers.md`: relation decision table, qualifier
  guidance, edge cases (hypersensitivity, drug-interaction contraindications,
  lists, OR-populations).
- `.claude/CLAUDE.md`: CURIE handling (`medic.curie_utils`), source isolation,
  traceability invariant I-8.
- `docs/source-isolation.md`.

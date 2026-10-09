## MeDIC-specific justfile targets. Imported by the main justfile.

# Default schema and config paths
schema_path := "src/medic/schema/medic.yaml"
drug_schema_path := "src/medic/schema/drug.yaml"
disease_schema_path := "src/medic/schema/disease.yaml"
indication_schema_path := "src/medic/schema/indication.yaml"
adverse_event_schema_path := "src/medic/schema/adverse_event.yaml"

kb_drugs_dir := "kb/drugs"
kb_diseases_dir := "kb/diseases"
kb_indications_dir := "kb/indications"
kb_adverse_events_dir := "kb/adverse_events"
kb_research_dir := "kb/research"

oak_config := "conf/oak_config.yaml"
ref_validator_config := "conf/reference_validator_config.yaml"

# ============== Build QC ==============

# Reconcile the products against their sources and conf/qc_baseline.yaml
[group('QC')]
qc:
  uv run python scripts/build_qc.py

# QC checks that need no built products (what CI can run)
[group('QC')]
qc-config:
  uv run python scripts/build_qc.py --no-products --out reports/build_qc_config.yaml

# Rebuild the on-label products twice and prove the bytes are identical
[group('QC')]
determinism:
  #!/usr/bin/env bash
  set -euo pipefail
  echo "build 1..."
  uv run python -m medic.merge.on_label_merge >/dev/null 2>&1
  a=$(shasum -a 256 products/indication_list.yaml products/contraindication_list.yaml | awk '{print $1}')
  echo "build 2..."
  uv run python -m medic.merge.on_label_merge >/dev/null 2>&1
  b=$(shasum -a 256 products/indication_list.yaml products/contraindication_list.yaml | awk '{print $1}')
  if [ "$a" = "$b" ]; then
    echo "OK: two builds produced byte-identical products"
  else
    echo "FAIL: rebuild is not deterministic" >&2
    printf 'build1:\n%s\nbuild2:\n%s\n' "$a" "$b" >&2
    exit 1
  fi

# ============== Manual sources ==============
#
# China (CDE) and Russia (GRLS) cannot be fetched: the CDE approvals table has no bulk export,
# and GRLS is IP-blocked for anonymous non-Russian sessions. Neither file is redistributable, so
# the archive is NOT in the repo — it is hosted out-of-band and downloaded on demand into
# background/, which is gitignored scratch. See sources/README.md and LICENSING.md.

# Where the archive is hosted. Deliberately NOT committed: a public "anyone with the link" URL
# in a public repo would redistribute the CDE and GRLS data just as surely as committing the zip.
# Set it in the gitignored .env, or export it per-shell.
export MEDIC_MANUAL_SOURCES_URL := env_var_or_default("MEDIC_MANUAL_SOURCES_URL", "")

# Download (if needed) and unpack the China + Russia source files into background/
[group('sources')]
restore-manual-sources force="false":
  #!/usr/bin/env bash
  set -euo pipefail
  archive="background/manual-sources.zip"
  mkdir -p background
  url="${MEDIC_MANUAL_SOURCES_URL}"
  # just's dotenv file is config.public.mk, which is tracked — so fall back to the gitignored
  # .env, which is where the URL belongs.
  if [ -z "$url" ] && [ -f .env ]; then
    url="$(grep -E '^[[:space:]]*MEDIC_MANUAL_SOURCES_URL=' .env | tail -1 | cut -d= -f2- | tr -d '"'"'"'')"
  fi
  if [ ! -f "$archive" ]; then
    if [ -z "$url" ]; then
      echo "ERROR: $archive is absent and MEDIC_MANUAL_SOURCES_URL is unset." >&2
      echo "The archive is not redistributable, so neither it nor its URL is kept in the repo." >&2
      echo "Add the URL to the gitignored .env (ask a maintainer for it):" >&2
      echo "  MEDIC_MANUAL_SOURCES_URL=<direct-download url>" >&2
      echo "or export it for one run:" >&2
      echo "  MEDIC_MANUAL_SOURCES_URL=<url> just restore-manual-sources" >&2
      exit 1
    fi
    echo "Downloading manual sources ..."
    curl -fsSL --retry 3 -o "$archive.part" "$url"
    # A share link that needs auth answers 200 with an HTML login page, which curl treats as
    # success. Check we actually got a zip, so the error names the cause instead of surfacing
    # as a confusing 'End-of-central-directory signature not found' from unzip.
    if ! unzip -tqq "$archive.part" >/dev/null 2>&1; then
      rm -f "$archive.part"
      echo "ERROR: the download is not a zip archive." >&2
      echo "MEDIC_MANUAL_SOURCES_URL must be a direct-download link. A Dropbox 'Copy link'" >&2
      echo "URL carries an rlkey parameter and needs dl=1 to serve bytes rather than a page:" >&2
      echo "  https://www.dropbox.com/scl/fi/<id>/manual-sources.zip?rlkey=<key>&dl=1" >&2
      exit 1
    fi
    mv "$archive.part" "$archive"
  fi
  if [ "{{force}}" = "true" ]; then
    unzip -o -q "$archive" -d background
    echo "Restored (overwrote) China + Russia source files into background/"
  else
    unzip -n -q "$archive" -d background
    echo "Restored China + Russia source files into background/ (existing files kept; pass force=true to overwrite)"
  fi
  ls -l background/cder_drugs_final_all.csv background/grls.zip
  just check-manual-sources

# Repack the current background/ copies into background/manual-sources.zip for re-upload
[group('sources')]
refresh-manual-sources:
  #!/usr/bin/env bash
  set -euo pipefail
  for f in background/cder_drugs_final_all.csv background/grls.zip; do
    [ -f "$f" ] || { echo "ERROR: $f is missing; nothing to repack." >&2; exit 1; }
  done
  rm -f background/manual-sources.zip
  ( cd background && zip -q -X -9 manual-sources.zip -j cder_drugs_final_all.csv grls.zip )
  echo "Repacked background/manual-sources.zip."
  echo "Upload it to the host behind MEDIC_MANUAL_SOURCES_URL, then re-run 'just ingest-china'"
  echo "and 'just ingest-russia' so data/source_manifest.json fingerprints the new files."
  echo "Commit the manifest — the archive itself is never committed."

# Verify the local manual sources match the fingerprints in data/source_manifest.json
[group('sources')]
check-manual-sources:
  uv run python scripts/check_manual_sources.py

# ============== Ingests ==============

# Ingest FDA Orange Book drug approvals
[group('ingest')]
ingest-orangebook grounding="lexical":
  uv run python -m medic.ingest.orangebook --grounding-backend {{grounding}}

# Ingest FDA Purple Book biologics
[group('ingest')]
ingest-purplebook grounding="lexical":
  uv run python -m medic.ingest.purplebook --grounding-backend {{grounding}}

# Ingest EMA drug approvals
[group('ingest')]
ingest-ema grounding="lexical":
  uv run python -m medic.ingest.ema --grounding-backend {{grounding}}

# Ingest EMA EPAR Product Information PDFs and extract §4.3 contraindications.
# WARNING: multi-hour run — downloads ~2,500 PDFs and runs LLM extraction.
[group('ingest')]
ingest-ema-contras grounding="lexical":
  uv run python -m medic.ingest.ema --grounding-backend {{grounding}} --extract-contras

# Ingest PMDA (Japan) drug approvals
[group('ingest')]
ingest-pmda grounding="lexical":
  uv run python -m medic.ingest.pmda --grounding-backend {{grounding}}

# Ingest PMDA contraindications from per-product review-report PDFs.
# WARNING: long run — downloads ~30% of PMDA drugs (those with a per-product
# review URL) and runs LLM disease extraction on each Contraindications section.
[group('ingest')]
ingest-pmda-contras grounding="lexical":
  uv run python -m medic.ingest.pmda --grounding-backend {{grounding}} --extract-contras --skip-indications

# Ingest Russian drug registry
[group('ingest')]
ingest-russia grounding="lexical":
  uv run python -m medic.ingest.russia --grounding-backend {{grounding}}

# Ingest Indian drug registry
[group('ingest')]
ingest-india grounding="lexical":
  uv run python -m medic.ingest.india --grounding-backend {{grounding}}

# Ingest Chinese CDE drug approvals
[group('ingest')]
ingest-china grounding="lexical":
  uv run python -m medic.ingest.china --grounding-backend {{grounding}}

# Ingest EveryCure curated drug list from HuggingFace
[group('ingest')]
ingest-everycure-drugs:
  uv run python -m medic.ingest.everycure_drugs

# Acquire real DailyMed SPL XML from the v2 API into data/raw/dailymed/
# (driven by the USA-approved drugs in products/drug_list.yaml). Resumable.
# Pass a limit for a quick sample, e.g. `just ingest-dailymed-acquire 50`.
[group('ingest')]
ingest-dailymed-acquire limit="0":
  uv run python -m medic.ingest.dailymed.acquire --limit {{limit}}

# Ingest FDA DailyMed indications and contraindications from SPL XML.
# The SPL-XML path is the single acquisition path; run
# `just ingest-dailymed-acquire` first to populate data/raw/dailymed/.
# An empty data/raw/dailymed/ is a hard error (no legacy fallback).
[group('ingest')]
ingest-dailymed grounding="lexical":
  uv run python -m medic.ingest.dailymed --grounding-backend {{grounding}}

# Ingest PVLens adverse events
[group('ingest')]
ingest-pvlens grounding="lexical":
  uv run python -m medic.ingest.pvlens --grounding-backend {{grounding}}

# Ingest FAERS adverse event reports
[group('ingest')]
ingest-faers grounding="lexical":
  uv run python -m medic.ingest.faers --grounding-backend {{grounding}}

# Ingest disease list from HuggingFace
[group('ingest')]
ingest-disease-list:
  uv run python -m medic.ingest.disease_list

# Ingest CURE-ID drug repurposing case reports
[group('ingest')]
ingest-cureid:
  uv run python -m medic.ingest.cureid

# ============== Products ==============

# Build the merged drug list from all sources
[group('products')]
build-drug-list grounding="lexical":
  just setup-grounding
  just ingest-orangebook {{grounding}}
  just ingest-purplebook {{grounding}}
  just ingest-ema {{grounding}}
  just ingest-pmda {{grounding}}
  just ingest-russia {{grounding}}
  just ingest-india {{grounding}}
  just ingest-china {{grounding}}
  just ingest-everycure-drugs
  uv run python -m medic.merge.drug_merge

# Resolve the unresolved drug-string residue via RxNorm (RxNav API) and write
# curator-reviewable CHEBI proposals into mappings/drug_grounding.sssom.tsv. Network-based
# and cached (cache/enrichment/rxnorm_resolve.json); honours MEDIC_SKIP_EXPENSIVE_CALLS.
# Run after build-drug-list; a subsequent grounding run reads the proposals deterministically.
[group('products')]
resolve-drug-residue *args:
  uv run python -m medic.enrichment.rxnorm_resolve {{args}}

# Build the disease list
[group('products')]
build-disease-list:
  just ingest-disease-list
  uv run python -m medic.merge.disease_merge
  just validate-schema products/disease_list.yaml
  just validate-terms products/disease_list.yaml

# Build the on-label indication and contraindication lists
[group('products')]
build-on-label-list grounding="lexical":
  just setup-grounding
  just ingest-dailymed {{grounding}}
  just ingest-ema {{grounding}}
  just ingest-pmda {{grounding}}
  uv run python -m medic.merge.on_label_merge

# Normalize disease IDs to MONDO across on-label products
[group('products')]
normalize-disease-ids grounding="lexical":
  uv run python scripts/normalize_disease_ids.py --grounding-backend {{grounding}}

# Build the adverse event list.
#
# NOT part of `build-all` in v2.0.0. Both inputs (data/pvlens, data/faers) are empty, so this
# produced an empty `products/adverse_event_list.yaml` that shipped as a permanently HELD
# asset — a placeholder standing in for a licence question nobody had answered. The recipe
# stays because the ingesters and schema do; running it is now a deliberate act, and its
# output is gitignored so it cannot commit MedDRA term text. See the open issues on whether
# PVLens and FAERS can be ingested under the ICH/MSSO terms at all.
[group('products')]
build-adverse-event-list grounding="lexical":
  just ingest-pvlens {{grounding}}
  just ingest-faers {{grounding}}
  uv run python -m medic.merge.adverse_event_merge

# Compile research pipeline output
[group('products')]
build-research:
  uv run python -m medic.research.compile

# Build all products
[group('products')]
build-all grounding="lexical":
  just build-drug-list {{grounding}}
  just build-disease-list
  just build-on-label-list {{grounding}}
  # build-adverse-event-list is deliberately absent — see that recipe. Adverse events are not
  # a v2.0.0 product; building an empty one only created a MedDRA-shaped hole in the release.
  just build-research
  just export-legacy
  just export-kgx
  just export-sssom
  just export-reliability
  just validate-all

# ============== Validation ==============

# Validate a single file against its schema (schema-only, fast)
[group('QC')]
validate-schema file target_class="":
    #!/usr/bin/env bash
    set -e
    # Auto-detect target class from file path if not specified.
    # Order matters: the kb/ patterns are checked before the generic product patterns, because
    # 'kb/drugs/china/china.yaml' also matches *drug* and would otherwise be silently validated
    # against DrugList — a container class it has nothing to do with.
    tc="{{target_class}}"
    f="{{file}}"; f="${f#./}"
    if [ -z "$tc" ]; then
        case "$f" in
            # QC reports are free-form tallies with no LinkML class. Refuse rather than guess.
            *_report.yaml)
                echo "$f is a QC report, not a schema-bound product — nothing to validate against." >&2
                exit 1 ;;
            # Per-source intermediates. DrugSource and the indication source classes do not yet
            # cover what the ingesters emit, and SPEC.md task 16 still lists the flat atc_*/is_*
            # fields as pending removal — so there is no stable class to check these against.
            kb/drugs/*|kb/indications/*/*)
                echo "$f is a per-source intermediate with no settled schema contract." >&2
                echo "Validate the merged product instead (products/drug_list.yaml, products/indication_list.yaml)," >&2
                echo "or pass an explicit target_class if you know what you are checking." >&2
                exit 1 ;;
            kb/research/*) tc="ResearchAssociationList" ;;
            kb/diseases/*) tc="DiseaseList" ;;
            *drug*) tc="DrugList" ;;
            *disease*) tc="DiseaseList" ;;
            *on_label*|*indication*) tc="IndicationList" ;;
            *adverse_event*) tc="AdverseEventList" ;;
            *research*) tc="ResearchAssociationList" ;;
            *) echo "Cannot auto-detect target class for $f. Specify target_class parameter."; exit 1 ;;
        esac
    fi
    uv run linkml-validate --schema {{schema_path}} --target-class "$tc" {{file}}

# Validate ontology term IDs in a file
[group('QC')]
validate-terms file target_class="":
    #!/usr/bin/env bash
    set -e
    tc="{{target_class}}"
    if [ -z "$tc" ]; then
        case "{{file}}" in
            *drug*) tc="DrugList" ;;
            *disease*) tc="DiseaseList" ;;
            *on_label*|*indication*) tc="IndicationList" ;;
            *adverse_event*) tc="AdverseEventList" ;;
            *research*) tc="ResearchAssociation" ;;
            *) echo "Cannot auto-detect target class for {{file}}. Specify target_class parameter."; exit 1 ;;
        esac
    fi
    uv run linkml-term-validator validate-data "{{file}}" -s {{schema_path}} -t "$tc" --labels -c {{oak_config}}

# Validate evidence references in a file
[group('QC')]
validate-references file target_class="":
    #!/usr/bin/env bash
    set -e
    tc="{{target_class}}"
    if [ -z "$tc" ]; then
        case "{{file}}" in
            *drug*) tc="DrugList" ;;
            *disease*) tc="DiseaseList" ;;
            *on_label*|*indication*) tc="IndicationList" ;;
            *adverse_event*) tc="AdverseEventList" ;;
            *research*) tc="ResearchAssociation" ;;
            *) echo "Cannot auto-detect target class for {{file}}. Specify target_class parameter."; exit 1 ;;
        esac
    fi
    scripts/run_reference_validator.sh validate data "{{file}}" --schema {{schema_path}} --target-class "$tc" --config {{ref_validator_config}}

# Check extracted diseases are lexically stated in their source snippet (fidelity / anti-hallucination)
[group('QC')]
validate-extraction *files:
    uv run python -m medic.validation.extraction_fidelity {{files}} --out extraction_flagged.tsv

# ---- Curated combination products (kb/combinations/) --------------------------------

# Deterministic checks for curated combination files (default: all of kb/combinations/)
[group('curation')]
validate-curation *files:
    uv run python -m medic.curation.validate {{files}}

# Print a DailyMed label for curation (add --skeleton for a starting YAML file)
[group('curation')]
[positional-arguments]
curation-label *args:
    uv run python -m medic.curation.label "$@"

# Rebuild curation/combination_worklist.tsv from the pipeline's DailyMed rows
[group('curation')]
combination-worklist:
    uv run python scripts/combination_worklist.py

# Prose arguments pass through as real positional arguments so quoting survives (dismech #9784).
# Scaffold an append-only curation history record (format adopted from dismech)
[group('curation')]
[positional-arguments]
new-history *args:
    uv run python -m medic.curation.history new "$@"

# Validate history records (default: every record under history/)
[group('curation')]
[positional-arguments]
validate-history *args:
    uv run python -m medic.curation.history validate "$@"

# Tally statements by type x reliability tier — the soft-launch importable subset
[group('QC')]
reliability-report *files:
    uv run python -m medic.reliability {{files}}

# EveryCure FDA-approved drugs MeDIC covers nowhere (completeness QA; not an approval source)
[group('QC')]
coverage-gaps:
    uv run python -m medic.coverage

# Flatten products into reliability-annotated statement exports (+ the reliable subset)
[group('products')]
export-reliability:
    uv run python -m medic.reliability_export

# Full validation of a single file (schema + terms + references)
[group('QC')]
validate file target_class="":
    #!/usr/bin/env bash
    set -e
    echo "Schema validation..."
    just validate-schema "{{file}}" "{{target_class}}"
    echo "Term validation..."
    just validate-terms "{{file}}" "{{target_class}}"
    echo "Reference validation..."
    just validate-references "{{file}}" "{{target_class}}"
    echo "✓ All validations passed for {{file}}"

# Validate all product and KB files
[group('QC')]
validate-all:
    #!/usr/bin/env bash
    failed_files=()
    echo "Validating product files..."

    validate_file() {
        local f="$1"
        local tc="$2"
        echo "=== $(basename $f) ==="
        output=$(just validate-schema "$f" "$tc" 2>&1)
        if echo "$output" | grep -q "No issues found"; then
            echo "  ✓ OK"
        else
            failed_files+=("$f")
            echo "  [SCHEMA FAILED]"
            echo "$output" | head -5
        fi
    }

    # Validate merged product files
    [ -f products/drug_list.yaml ] && validate_file products/drug_list.yaml DrugList
    [ -f products/indication_list.yaml ] && validate_file products/indication_list.yaml IndicationList
    [ -f products/contraindication_list.yaml ] && validate_file products/contraindication_list.yaml IndicationList
    [ -f products/adverse_event_list.yaml ] && validate_file products/adverse_event_list.yaml AdverseEventList
    [ -f products/disease_list.yaml ] && validate_file products/disease_list.yaml DiseaseList

    echo ""
    echo "================================"
    if [ ${#failed_files[@]} -eq 0 ]; then
        echo "✓ All files validated successfully!"
    else
        echo "✗ ${#failed_files[@]} file(s) with errors:"
        for f in "${failed_files[@]}"; do
            echo "  - $f"
        done
        exit 1
    fi

# ============== Research ==============

# Interactive research curation for a disease
[group('research')]
research-curate disease="next":
  uv run python -m medic.research.curate --disease {{disease}}

# Batch research for multiple diseases
[group('research')]
research-batch count="10":
  uv run python -m medic.research.batch --count {{count}}

# Directory for deep research outputs
research_dir := "research"
templates_dir := "templates"

# Deep research on a disease using specified provider
# Examples:
#   just research-disease perplexity "Marfan syndrome" MONDO:0007947
#   just research-disease falcon "hemophilia A" MONDO:0010602
#   just research-disease cyberian "sickle cell disease" MONDO:0011382
[group('research')]
research-disease provider disease_name mondo_id="" *args="":
    #!/usr/bin/env bash
    set -e
    mkdir -p {{research_dir}}
    safe_name=$(echo "{{disease_name}}" | tr ' ' '_')
    output_file="{{research_dir}}/${safe_name}-deep-research-{{provider}}.md"
    echo "Researching: {{disease_name}} ({{provider}}) -> $output_file"
    provider_arg=$([[ "{{provider}}" == "cborg" ]] && echo "--use-cborg" || echo "--provider {{provider}}")
    uv run --group research --python 3.12 deep-research-client research \
        --template {{templates_dir}}/drug_disease_research.md \
        --var "disease_name={{disease_name}}" \
        --var "mondo_id={{mondo_id}}" \
        $provider_arg \
        --output "$output_file" \
        --separate-citations "$output_file.citations.md" \
        {{args}}

# Deep research using cyberian codex agent
[group('research')]
research-disease-cyberian-codex disease_name mondo_id="" *args="":
    #!/usr/bin/env bash
    set -e
    mkdir -p {{research_dir}}
    safe_name=$(echo "{{disease_name}}" | tr ' ' '_')
    output_file="{{research_dir}}/${safe_name}-deep-research-cyberian-codex.md"
    echo "Researching: {{disease_name}} (cyberian-codex) -> $output_file"
    uv run --group research --python 3.12 deep-research-client research \
        --template {{templates_dir}}/drug_disease_research.md \
        --var "disease_name={{disease_name}}" \
        --var "mondo_id={{mondo_id}}" \
        --provider cyberian \
        --param agent_type=codex \
        --output "$output_file" \
        --separate-citations "$output_file.citations.md" \
        {{args}}

# Batch deep research on N diseases from the priority list
# Uses the first uncurated diseases and a specified provider
[group('research')]
research-disease-batch provider count="5" *args="":
    #!/usr/bin/env bash
    set -e
    mkdir -p {{research_dir}}
    uv run python scripts/batch_deep_research.py \
        --provider {{provider}} \
        --count {{count}} \
        --research-dir {{research_dir}} \
        --templates-dir {{templates_dir}} \
        {{args}}

# List available deep research providers
[group('research')]
research-providers:
    uv run --group research --python 3.12 deep-research-client providers

# Parse a deep research markdown report into structured kb/research YAML
# Example: just parse-deep-research research/hemophilia_A-deep-research-perplexity.md MONDO:0010602 "hemophilia A"
[group('research')]
parse-deep-research md_file disease_id disease_label:
    uv run python scripts/parse_deep_research.py "{{md_file}}" --disease-id "{{disease_id}}" --disease-label "{{disease_label}}" --write

# LEGACY: Parse all deep research reports via regex (replaced by medic-research-curation skill)
# Use /medic-research-curation skill instead for proper individual drug extraction
[group('research')]
parse-all-deep-research-legacy:
    uv run python scripts/parse_all_deep_research_legacy.py

# Curate evidence snippets: extract verified PubMed excerpts for a research YAML
# Example: just curate-snippets kb/research/MONDO_0001347.yaml
[group('research')]
curate-snippets file:
    uv run python scripts/curate_snippets.py "{{file}}"

# Curate snippets for all research YAML files in kb/research/
[group('research')]
curate-all-snippets:
    #!/usr/bin/env bash
    set -e
    for f in kb/research/MONDO_*.yaml; do
        [ -f "$f" ] || continue
        echo "=== $(basename $f) ==="
        uv run python scripts/curate_snippets.py "$f"
    done

# Full research pipeline: parse deep research reports -> curate snippets -> compile -> validate
[group('research')]
research-pipeline:
    @echo "NOTE: Drug extraction is now done via /medic-research-curation skill, not regex parsing."
    @echo "Run the skill first, then continue with snippet curation below."
    just curate-all-snippets
    just build-research
    just validate-all-research

# Validate all research YAML files
[group('QC')]
validate-all-research:
    #!/usr/bin/env bash
    set -e
    for f in kb/research/MONDO_*.yaml; do
        [ -f "$f" ] || continue
        echo "=== $(basename $f) ==="
        just validate-schema "$f" ResearchAssociationList
        just validate-references "$f" ResearchAssociationList
    done
    echo "✓ All research files validated"

# Fetch and cache a reference by ID (PMID, DOI, etc.)
[group('research')]
fetch-reference +identifiers:
    #!/usr/bin/env bash
    set -e
    for id in {{identifiers}}; do
        echo "Fetching: $id"
        uv run linkml-reference-validator cache reference "$id"
    done


# ============== Export ==============

# Generate legacy CSV/XLSX exports matching v1.0.0 format
[group('export')]
export-legacy:
  uv run python -m medic.export.legacy

# Generate KGX biolink-compliant export (nodes, edges, metadata, infores entry)
[group('export')]
export-kgx:
  uv run python -m medic.export.kgx export

# Validate the built KGX export against the pinned Biolink model
[group('QC')]
validate-kgx:
  uv run python -m medic.export.kgx validate

# Generate SSSOM drug mappings export
[group('export')]
export-sssom:
  uv run python -m medic.export.sssom

# Compare current exports against a GitHub release
[group('QC')]
compare-release release="v1.0.0" output="docs/v1_comparison_report.md":
  uv run python scripts/compare_release.py --release {{release}} --output {{output}}

# ============== Utility ==============

# List all drugs in the merged drug list
[group('utility')]
list-drugs:
    #!/usr/bin/env python3
    import yaml, pathlib, sys
    p = pathlib.Path('products/drug_list.yaml')
    if not p.exists():
        print("No drug list found. Run 'just build-drug-list' first.")
        sys.exit(0)
    with open(p) as f:
        data = yaml.safe_load(f)
    items = data if isinstance(data, list) else data.get('drugs', [])
    for d in items:
        print(f"{d.get('curie', 'N/A')}\t{d.get('curie_label', 'N/A')}")

# Count entities across all products
[group('utility')]
count-entities:
    #!/usr/bin/env bash
    echo "=== Entity counts ==="
    for f in products/*.yaml; do
        [ -f "$f" ] || continue
        count=$(grep -c "^- " "$f" 2>/dev/null || echo "0")
        echo "$(basename $f): $count entries"
    done

# Generate schema documentation pages
[group('docs')]
gen-schema-docs:
  just gen-doc

# Release: build all products and export artifacts (run before gh-release)
# Show which build outputs are cleared for release, held, or unlisted.
# Exits non-zero if anything in products/ or exports/ has no entry in
# conf/release_assets.yaml — i.e. nobody has decided whether it may ship.
[group('deployment')]
release-assets:
  uv run python -m medic.release_assets check

[group('deployment')]
release:
  just build-all
  @echo "Release artifacts ready in products/ and exports/"
  @echo "Publish with: just gh-release vX.Y.Z"

# Publish a GitHub release, uploading current products/ and exports/ artifacts.
# Packages whatever is already on disk (run 'just release' / 'just build-all' first);
# it does NOT rebuild.
#
# Assets come from conf/release_assets.yaml, NOT from a glob: a file in products/ or
# exports/ with no manifest entry is REFUSED, so a new export forces a licensing decision
# instead of shipping by default. The attribution notice is generated from the sources the
# shipping assets actually draw on. See 'just release-assets' and LICENSING.md.
#
# Creates a DRAFT release by default so you can review before publishing.
# Examples:
#   just gh-release v1.1.0            # create draft release v1.1.0
#   just gh-release v1.1.0 false      # publish immediately (no draft)
[group('deployment')]
gh-release version draft="true":
    #!/usr/bin/env bash
    set -euo pipefail

    # Validate version tag format (vX.Y.Z, optional -suffix).
    if [[ ! "{{version}}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([.-].+)?$ ]]; then
        echo "ERROR: version must look like vX.Y.Z (got '{{version}}')" >&2
        exit 1
    fi

    # The version the artefacts are stamped with must be the version being released.
    # `medic_release()` reads `importlib.metadata.version("medic")`, which uv bakes into the
    # installed distribution at `uv sync` time — so tagging and then building without a
    # reinstall stamps the PREVIOUS release into medic_kgx_metadata.yaml and every provenance
    # step. Nothing reconciled the two, and the tree shipped `medic_version: 1.0.1` against a
    # v2.0.0 build. Comparing them here is the only point where both are known.
    stamped=$(uv run python -c "from medic.versions import medic_release; print(medic_release())")
    if [ "v${stamped}" != "{{version}}" ]; then
        echo "ERROR: the installed distribution is ${stamped}, releasing {{version}}." >&2
        echo "       Tag the commit, then 'uv sync' to refresh the installed version," >&2
        echo "       then rebuild so the stamp matches: git tag {{version}} && uv sync && just build-all" >&2
        exit 1
    fi

    # ...and the check above is NOT sufficient on its own. `medic_release()` describes the
    # virtualenv, not the files being uploaded: `uv sync` after tagging satisfies it while
    # products/ and exports/ still hold whatever the last build wrote. Doing exactly what the
    # message above says but skipping `just build-all` shipped assets stamped 1.0.1 under a
    # v2.0.0 tag — 17,070 `tool_version: 1.0.1` strings across the three products. So read the
    # stamp out of the artefacts themselves, which is the thing consumers actually see.
    meta="exports/medic_kgx_metadata.yaml"
    if [ ! -f "${meta}" ]; then
        echo "ERROR: ${meta} is missing — cannot verify what the artefacts are stamped with." >&2
        echo "       Run 'just build-all' before releasing." >&2
        exit 1
    fi
    artefact=$(sed -n 's/^medic_version:[[:space:]]*//p' "${meta}" | head -1 | tr -d '"'"'"' \r')
    if [ -z "${artefact}" ]; then
        echo "ERROR: ${meta} carries no medic_version — refusing to guess." >&2
        exit 1
    fi
    if [ "v${artefact}" != "{{version}}" ]; then
        echo "ERROR: the built artefacts are stamped ${artefact}, releasing {{version}}." >&2
        echo "       The installed version matches but the files on disk are from an older" >&2
        echo "       build. Rebuild so the artefacts carry the version being released:" >&2
        echo "           just build-all" >&2
        exit 1
    fi

    # Regenerate the general NOTICE first, so it is always in the release it describes
    # and always reflects the assets that release actually contains.
    uv run python -m medic.release_assets notice-file

    # Assets are whatever conf/release_assets.yaml clears for release; an unlisted file
    # is refused rather than shipped (see the recipe docs).
    #
    # Command substitution, not `mapfile < <(...)`: mapfile is a bash 4 builtin and macOS
    # ships bash 3.2, and a process substitution would swallow the non-zero exit that
    # signals an unlisted file — the release would then silently ship nothing.
    if ! asset_list=$(uv run python -m medic.release_assets list); then
        echo "" >&2
        echo "ERROR: release assets are not cleared — see above." >&2
        echo "       Run 'just release-assets' for the full plan." >&2
        exit 1
    fi

    assets=()
    while IFS= read -r line; do
        [ -n "$line" ] && assets+=("$line")
    done <<< "$asset_list"

    if [ ${#assets[@]} -eq 0 ]; then
        echo "ERROR: no release assets cleared. Run 'just build-all' first," >&2
        echo "       then 'just release-assets' to see why." >&2
        exit 1
    fi

    echo ""
    echo "Release {{version}} — ${#assets[@]} asset(s):"
    for a in "${assets[@]}"; do
        printf '  %-42s %6s\n' "$a" "$(du -h "$a" | cut -f1)"
    done
    echo ""

    # Attribution notice covering exactly these assets; EMA and PMDA both require it.
    notice=$(uv run python -m medic.release_assets notice)
    echo "$notice" > /tmp/medic_release_notice.txt
    echo "Notice: $notice"
    echo ""

    draft_flag=""
    [ "{{draft}}" = "true" ] && draft_flag="--draft"

    if gh release view "{{version}}" >/dev/null 2>&1; then
        echo "Release {{version}} already exists — uploading/replacing assets..."
        gh release upload "{{version}}" "${assets[@]}" --clobber
    else
        echo "Creating ${draft_flag:+draft }release {{version}}..."
        gh release create "{{version}}" "${assets[@]}" \
            $draft_flag \
            --title "MeDIC {{version}}" \
            --notes "$notice" \
            --generate-notes
    fi

    url=$(gh release view "{{version}}" --json url --jq .url)
    echo ""
    echo "✓ Release {{version}} ready: $url"
    [ "{{draft}}" = "true" ] && echo "  (draft — review the assets, then publish on GitHub)"

# ============== Grounding (deterministic lexical) ==============

robot_img := "obolibrary/odkfull"

# Download the OBO ontologies used by the grounding index into cache/ontologies/
[group('grounding')]
download-grounding-ontologies:
  mkdir -p cache/ontologies
  for o in mondo hp chebi dron; do \
    curl -sL -o cache/ontologies/$o.json http://purl.obolibrary.org/obo/$o.json; done

# Convert a large OWL (e.g. PR) to OBO Graph JSON with robot (docker). Usage:
#   just owl-to-json http://purl.obolibrary.org/obo/pr.owl cache/ontologies/pr.json
[group('grounding')]
owl-to-json url out heap="16g":
  mkdir -p cache/ontologies background/ontsrc
  curl -sL -o background/ontsrc/$(basename {{out}} .json).owl {{url}}
  docker run --rm -v "$PWD":/work -w /work -e ROBOT_JAVA_ARGS=-Xmx{{heap}} {{robot_img}} \
    robot convert -i background/ontsrc/$(basename {{out}} .json).owl -o {{out}}

# Download ICD10CM (BioPortal, UMLS2RDF Turtle) — set BIOPORTAL_APIKEY and ICD10_BP_CODE
[group('grounding')]
download-icd10cm code="27":
  mkdir -p background/ontsrc
  curl -sL "https://data.bioontology.org/ontologies/ICD10CM/submissions/{{code}}/download?apikey=${BIOPORTAL_APIKEY}" -o background/ontsrc/icd10cm.ttl

# Ensure the grounding + normalization indexes exist (build only if missing).
# Prerequisite for any ingest that grounds with the default `lexical` backend.
# Rebuilding is expensive (needs the big MONDO/CHEBI/UMLS source files), so this
# is a no-op when the compiled indexes are already present.
[group('grounding')]
setup-grounding:
  #!/usr/bin/env bash
  set -e
  if [ ! -f cache/grounding/lexical_index/diseases.db ] || [ ! -f cache/grounding/lexical_index/drugs.db ]; then
    echo "Lexical index missing — building (this is slow)..."
    just build-grounding-index
  else
    echo "Lexical index present: cache/grounding/lexical_index/{diseases,drugs}.db"
  fi
  if [ ! -f cache/normalization/diseases.db ]; then
    echo "Normalization index missing — building..."
    just build-normalization-index
  else
    echo "Normalization index present: cache/normalization/diseases.db"
  fi

# Build the offline lexical index for both diseases and drugs
[group('grounding')]
build-grounding-index:
  uv run python -c "from medic.grounding.lexical.build import build_index; print('diseases:', build_index('diseases','conf/grounding_sources.yaml','cache/grounding/lexical_index/diseases.db')); print('drugs:', build_index('drugs','conf/grounding_sources.yaml','cache/grounding/lexical_index/drugs.db'))"

# Build the Stage-2 normalization index from MONDO's asserted exactMatches
[group('grounding')]
build-normalization-index:
  uv run python -c "from medic.normalization.mapping_index import build_mapping_index; print('mondo maps:', build_mapping_index('MONDO','cache/ontologies/mondo.json','cache/normalization/diseases.db'))"

# Summarize the grounding decision stores
[group('grounding')]
ground-report:
  uv run python -c "from medic.grounding.report import report; import json; print(json.dumps(report('mappings/disease_grounding.sssom.tsv'), indent=2))"

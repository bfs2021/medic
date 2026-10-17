"""The shipped products validate against the schema.

This test exists because its absence cost three weeks. `original_disease_verbatim` was added
to the evidence row by #64 and `label_ingredient_count` / `context_only_disease` by #88/#89,
and none was declared on `EvidenceItem` — which uses a closed `attributes:` set, so every
affected row was schema-invalid. Nothing noticed: `just validate-all` runs only inside
`build-all`, and no test compared a product to its own schema.

The project's own rule (`.claude/CLAUDE.md`, I-8) is that a new manipulation means declaring
it *first*. A rule with no check is a hope, so this is the check.

Deliberately cheap: it validates a sample of associations rather than all 7,705, because a
full LinkML validation of a 113 MB product takes minutes and this has to run on every suite.
An undeclared field appears on whole classes of row, not on one, so a sample catches it.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest
import yaml

try:
    from yaml import CSafeLoader as _Loader
except ImportError:  # pragma: no cover
    from yaml import SafeLoader as _Loader

PRODUCTS = Path("products")
SAMPLE = 200


def _evidence_keys(path: Path, key: str) -> set[str]:
    """Every distinct evidence-row key used in the first `SAMPLE` associations."""
    with open(path) as fh:
        rows = (yaml.load(fh, Loader=_Loader) or {}).get(key) or []
    keys: set[str] = set()
    for assoc in itertools.islice(rows, SAMPLE):
        for assertion in assoc.get("assertions") or []:
            ev = assertion.get("evidence")
            for row in (ev if isinstance(ev, list) else [ev]):
                if isinstance(row, dict):
                    keys |= set(row)
        flat = assoc.get("evidence")
        for row in (flat if isinstance(flat, list) else [flat]):
            if isinstance(row, dict):
                keys |= set(row)
    return keys


def _declared_evidence_attributes() -> set[str]:
    schema = yaml.safe_load(Path("src/medic/schema/evidence.yaml").read_text())
    return set((schema["classes"]["EvidenceItem"].get("attributes") or {}))


@pytest.mark.parametrize("filename,key", [
    ("indication_list.yaml", "associations"),
    ("contraindication_list.yaml", "associations"),
])
def test_every_evidence_key_is_declared_on_the_schema(filename, key):
    path = PRODUCTS / filename
    if not path.exists():
        pytest.skip(f"{path} not built")
    used = _evidence_keys(path, key)
    declared = _declared_evidence_attributes()
    undeclared = sorted(used - declared)
    assert not undeclared, (
        f"{filename} ships evidence keys that EvidenceItem does not declare, so the file "
        f"fails `just validate-schema`: {undeclared}. Declare them in "
        f"src/medic/schema/evidence.yaml and regenerate the datamodel."
    )


def test_the_schema_declares_the_fields_this_work_added():
    """Pins the specific fields, so a regeneration that drops one is caught even if no
    product happens to be built."""
    declared = _declared_evidence_attributes()
    for field in (
        "original_disease_verbatim",   # #64
        "label_ingredient_count",      # #88
        "context_only_disease",        # #89
        "claim_predicate",             # #99
        "claim_attributed_to",         # #88
        "claim_attribution_basis",     # #88
        "disease_context",             # #90
        "population_context",          # #90
        "temporal_context",            # #90
        "co_administered_with",        # #90, #97
    ):
        assert field in declared, f"EvidenceItem does not declare {field}"

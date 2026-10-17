"""Both extractors return the same type (#88-#90 plumbing).

When `extract_diseases_from_text` started returning `Claim` while
`extract_contraindicated_diseases_from_text` still returned `Extracted`, four ingester
loops unpacked `.name` off a `Claim` and would have raised at runtime. The whole suite
passed, because the ingester's main loops have no unit coverage — so a type contract is the
only cheap place to catch it.

This is the same failure shape as the undeclared evidence fields: a change made in one half
of a pair, with nothing asserting the pair agrees.
"""

from __future__ import annotations

import inspect

from medic.ingest.dailymed.__main__ import (
    Claim,
    extract_contraindicated_diseases_from_text,
    extract_diseases_from_text,
)


def test_both_extractors_declare_the_same_return_type():
    ind = inspect.signature(extract_diseases_from_text).return_annotation
    contra = inspect.signature(extract_contraindicated_diseases_from_text).return_annotation
    assert ind == contra == list[Claim]


def test_a_claim_has_no_name_attribute():
    """Pins why the loops broke: `.name` is `Extracted`'s field, not `Claim`'s. If a future
    change adds it as an alias, the dual-shape hazard is back."""
    c = Claim(target="epilepsy")
    assert not hasattr(c, "name")
    assert c.target == "epilepsy"


def test_no_ingester_unpacks_name_off_a_claim():
    """Source-level guard. The loops are integration-only, so this is the cheap net."""
    from pathlib import Path

    offenders = []
    for path in sorted(Path("src/medic/ingest").rglob("__main__.py")):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if "_extracted.name" in line or "_extracted.verbatim" in line:
                offenders.append(f"{path}:{n}")
    assert not offenders, (
        f"ingester loops still read Extracted's fields off a Claim: {offenders}")

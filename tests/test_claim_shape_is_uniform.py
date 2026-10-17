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
    """Source-level guard. The loops are integration-only, so this is the cheap net.

    Claim-named variables only. A bare `e.name` cannot be judged by grep: the same
    expression is correct in `_encode_extractions`, which really does hold `Extracted`, and
    wrong in `context_only_names`, which does not. `test_context_only_names_accepts_claims`
    covers that one by calling it, which is the only way to tell the two apart.

    What this still buys: the loops that iterate `_claim` are integration-only and have no
    other coverage. A type checker would subsume this test — none is configured.
    """
    import re
    from pathlib import Path

    stale = re.compile(r"\b_?claims?\b\w*\.(?:name|verbatim)\b")
    offenders = []
    for path in sorted(Path("src/medic/ingest").rglob("*.py")):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if stale.search(line):
                offenders.append(f"{path}:{n}: {line.strip()}")
    assert not offenders, (
        "ingest code still reads Extracted's fields off a claim-shaped value:\n"
        + "\n".join(offenders))


def test_context_only_names_accepts_claims():
    """The real regression. `context_only_names` kept `Extracted`'s signature while its one
    caller started passing `Claim`, and the caller's `except Exception` logged "skipping"
    per label — so a full paid DailyMed run emitted zero indications and still exited 0.

    Calling it with the type its caller passes is the whole test.
    """
    from medic.ingest.dailymed.__main__ import context_only_names

    text = "for the treatment of invasive aspergillosis in patients with acute myeloid leukemia"
    flagged = context_only_names(
        [
            Claim(target="invasive aspergillosis",
                  target_verbatim="invasive aspergillosis"),
            Claim(target="acute myeloid leukemia",
                  target_verbatim="acute myeloid leukemia"),
        ],
        text,
    )
    assert "acute myeloid leukemia" in flagged
    assert "invasive aspergillosis" not in flagged

"""Enabler partners: ingredients that never own a treatment indication (#88).

A combination label (ceftazidime-avibactam, imipenem-cilastatin, elvitegravir-cobicistat)
is one document asserting one indication for the *product*. When the ingest splits that
label per ingredient, every component inherits the indication — and for one class of
component the inheritance is always wrong: beta-lactamase inhibitors, the renal
dehydropeptidase inhibitor cilastatin and the pharmacokinetic booster cobicistat exist only
to make a partner work. "avibactam treats pyelonephritis" is the partner's claim credited to
the wrong active moiety — FAILURE_MODES §5.4, the ``wrong_pairing`` assertion flag.

This is a *class* rule keyed on the drug name, kept in a hand-editable store
(``mappings/enabler_partners.tsv``) so a curator can extend it without touching code. It is
deliberately narrow: ritonavir is not listed, because it is a booster *and* a treatment at
full dose and a name cannot tell the two apart. Only indications are affected — a
contraindication on cobicistat is a real claim about cobicistat.
"""

from __future__ import annotations

import csv
import functools
import re
from pathlib import Path

from medic.grounding.lexical.preprocess import salt_variants

#: Anchored on the package, not the working directory: the store is read at merge time and
#: at score time, and the two do not run from the same place.
ENABLER_PARTNERS_PATH = Path(__file__).resolve().parents[2] / "mappings" / "enabler_partners.tsv"

#: The only classes the store may name. A row with an unknown class is a typo, not a new
#: category — adding one is a code change, because each class is a different argument for
#: why the ingredient cannot own an indication.
ENABLER_CLASSES = frozenset({
    "beta_lactamase_inhibitor",
    "dehydropeptidase_inhibitor",
    "pk_booster",
})

#: The flag the detector emits (``AssertionFlag.wrong_pairing`` in provenance.yaml).
WRONG_PAIRING = "wrong_pairing"

_WS = re.compile(r"\s+")


def _normalize(label: str | None) -> str:
    return _WS.sub(" ", (label or "").strip().lower())


@functools.lru_cache(maxsize=4)
def load_enabler_partners(path: str | Path = ENABLER_PARTNERS_PATH) -> dict[str, str]:
    """``{normalized name: class}`` from the store; ``#`` lines are front-matter.

    Raises ``ValueError`` on an unknown class so a typo in the store fails the build rather
    than silently dropping the row.
    """
    out: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return out
    with open(p, newline="") as fh:
        reader = csv.DictReader((ln for ln in fh if not ln.startswith("#")), delimiter="\t")
        for row in reader:
            name = _normalize(row.get("name"))
            cls = (row.get("class") or "").strip()
            if not name:
                continue
            if cls not in ENABLER_CLASSES:
                raise ValueError(
                    f"{p}: {name!r} has class {cls!r}, expected one of {sorted(ENABLER_CLASSES)}")
            out[name] = cls
    return out


def enabler_class(label: str | None) -> str | None:
    """The enabler class of a drug label, or ``None`` when it is not an enabler partner.

    Whole-name match on the lowercased label, retried with trailing salt words stripped
    (``avibactam sodium`` → ``avibactam``). Whole-name on purpose: ``amoxicillin and
    clavulanate potassium`` is the *product*, whose indication is right.
    """
    key = _normalize(label)
    if not key:
        return None
    table = load_enabler_partners()
    if key in table:
        return table[key]
    for variant in salt_variants(key):
        if variant.string in table:
            return table[variant.string]
    return None


def is_enabler_partner(label: str | None) -> bool:
    return enabler_class(label) is not None

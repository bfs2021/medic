"""A vaccine does not treat the disease it immunises against (#99).

All 12,448 indication edges in the 2026-10-13 export carry `biolink:treats`, including
`pertussis vaccine -> pertussis` and the 23 pneumococcal capsular polysaccharides. A graph
asked "what treats pertussis?" answers with the vaccine. Biolink has
`preventative_for_condition` for exactly this.

Scoped deliberately to vaccines and toxoids. **Immune globulins are excluded**, even though
the issue groups them in: for `Rho(D) immune globulin -> ectopic pregnancy` the object is
the exposure *setting*, not a condition the product prevents, so relabelling the predicate
would turn one wrong statement into a different wrong statement. That object is #89's
error class and has to be fixed there.

ATC J07 would be the principled selector but is unpopulated for every one of these drugs
(0 of 8,085 pairs), so this goes on the product name, with the token list measured against
the corpus rather than guessed: all 37 matches are genuine vaccine products and no
non-vaccine drug name in the indication corpus matches.
"""

from __future__ import annotations

import pytest

from medic.export.kgx import biolink as bl
from medic.export.kgx.edges import _indication_predicate


@pytest.mark.parametrize("name", [
    "pertussis vaccine",
    "Hepatitis B Vaccine",
    "ROTAVIRUS VACCINE, LIVE",
    "meningococcal group B vaccine",
    "diphtheria toxoid",
    "Streptococcus pneumoniae type 10A capsular polysaccharide antigen",
    "Vibrio cholerae CVD 103-HGR strain live antigen",
    "tick-borne encephalitis purified antigen",
])
def test_a_vaccine_prevents_rather_than_treats(name):
    assert _indication_predicate("APPROVED", name) == bl.PREVENTION_PREDICATE


@pytest.mark.parametrize("name", [
    "acetylsalicylic acid",
    "voriconazole",
    "insulin glargine",
    "tobramycin",
    # Not a vaccine despite the shared stem; must not be caught by a loose token.
    "vancomycin",
])
def test_an_ordinary_drug_still_treats(name):
    assert _indication_predicate("APPROVED", name) == bl.INDICATION_PREDICATE


@pytest.mark.parametrize("name", [
    "rho (d) immune globulin",
    "hepatitis b immune globulin",
])
def test_an_immune_globulin_is_left_alone(name):
    """Its object is usually the exposure setting (#89), so changing the predicate would
    assert something different and equally wrong."""
    assert _indication_predicate("APPROVED", name) == bl.INDICATION_PREDICATE


def test_an_unapproved_vaccine_keeps_the_grouping_predicate():
    """The approval-status rule still wins: Biolink reserves an asserted predicate for
    approved use, and that is orthogonal to treats-vs-prevents."""
    assert _indication_predicate("INVESTIGATIONAL", "pertussis vaccine") == \
        bl.INDICATION_UNAPPROVED_PREDICATE


def test_the_prevention_predicate_is_valid_biolink():
    assert bl.is_predicate(bl.PREVENTION_PREDICATE)


def test_the_predicate_has_a_knowledge_level():
    """Every emitted predicate needs its knowledge-level/agent-type mapping, or the
    conformance gate reports an unmapped predicate."""
    assert bl.PREVENTION_PREDICATE in bl.KNOWLEDGE_LEVELS

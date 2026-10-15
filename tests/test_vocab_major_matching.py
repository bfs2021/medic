"""A substance vocabulary beats a clinical-drug vocabulary (#94).

`VOCAB_ORDER["drugs"]` already puts CHEBI before DRON, but `_vocab_pick` was consulted
*within* a field while the loop ran field-major: label across all vocabs, then exactSynonym
across all vocabs. So a DRON clinical-drug form matching on its **label** was reached before
a CHEBI substance matching on an **exact synonym**, and `Albendazole Tablets` grounded to
`DRON:00000500` rather than to albendazole.

That matters beyond tidiness: 69 of the 717 drug ids unknown to the SRI Node Normalizer are
DRON, and a DRON clinical-drug id is the wrong *kind* of node for a drug-disease edge — it
denotes a dose form, not a substance, so no other resource in the graph joins to it.

Making the loop vocab-major expresses the preference that is already configured.
"""

from __future__ import annotations

from medic.grounding.lexical.index import VOCAB_ORDER
from medic.grounding.lexical.matcher import Matcher


class _FakeIndex:
    """Enough of LexicalIndex to drive the matcher's ordering."""

    entity_type = "drugs"

    def __init__(self, rows_by_field):
        self.vocab_order = VOCAB_ORDER["drugs"]
        self._rows = rows_by_field

    def lookup_raw(self, value, field):
        return list(self._rows.get(field, []))

    def lookup_norm(self, value, field):
        return list(self._rows.get(field, []))

    def lookup_norm_many(self, values, field):
        return list(self._rows.get(field, []))


class _Row:
    def __init__(self, object_id, source_prefix, match_field, label="x"):
        self.object_id = object_id
        self.object_label = label
        self.source_prefix = source_prefix
        self.match_field = match_field
        self.norm_value = "albendazole tablets"
        self.synonym_scope = "exact"


def _matcher(rows_by_field):
    return Matcher(_FakeIndex(rows_by_field), store=None)


def test_a_chebi_synonym_beats_a_dron_label():
    """The Albendazole Tablets case."""
    m = _matcher({
        "label": [_Row("DRON:00000500", "DRON", "label")],
        "exactSynonym": [_Row("CHEBI:16664", "CHEBI", "exactSynonym")],
    })
    d = m._try_base("Albendazole Tablets", "albendazole tablets", "norm", 1.0)
    assert d is not None
    assert d.object_id == "CHEBI:16664"


def test_dron_still_wins_when_chebi_has_nothing():
    """The preference is an ordering, not an exclusion — DRON remains a valid fallback."""
    m = _matcher({"label": [_Row("DRON:00000500", "DRON", "label")]})
    d = m._try_base("Albendazole Tablets", "albendazole tablets", "norm", 1.0)
    assert d is not None
    assert d.object_id == "DRON:00000500"


def test_a_chebi_label_still_beats_a_chebi_synonym():
    """Within one vocabulary the field order is unchanged: a label is better evidence."""
    m = _matcher({
        "label": [_Row("CHEBI:1", "CHEBI", "label")],
        "exactSynonym": [_Row("CHEBI:2", "CHEBI", "exactSynonym")],
    })
    d = m._try_base("x", "x", "norm", 1.0)
    assert d.object_id == "CHEBI:1"


def test_confidence_still_reflects_the_field_not_the_vocab():
    """A synonym match must not be recorded as label-strength just because it won."""
    m = _matcher({
        "label": [_Row("DRON:00000500", "DRON", "label")],
        "exactSynonym": [_Row("CHEBI:16664", "CHEBI", "exactSynonym")],
    })
    d = m._try_base("x", "x", "norm", 1.0)
    assert d.confidence < 1.0
    assert d.object_match_field == "oio:hasExactSynonym"


def test_ambiguity_within_the_winning_vocab_still_abstains():
    m = _matcher({"label": [_Row("CHEBI:1", "CHEBI", "label"),
                            _Row("CHEBI:2", "CHEBI", "label")]})
    assert m._try_base("x", "x", "norm", 1.0) is None


def test_the_fuzzy_path_prefers_the_substance_too():
    m = _matcher({
        "label": [_Row("DRON:00000500", "DRON", "label")],
        "exactSynonym": [_Row("CHEBI:16664", "CHEBI", "exactSynonym")],
    })
    d = m._fuzzy("albendazole tablet", "albendazole tablet")
    assert d is not None
    assert d.object_id == "CHEBI:16664"

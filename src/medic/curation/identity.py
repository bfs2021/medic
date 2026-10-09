"""Identity of a combination product: computed from its ingredients, never chosen.

A combination is its set of active ingredients. The id, the display name and the file name
are all derived from that set, so the same ingredients always land in the same file whichever
regulator lists them, in whichever order, under whichever salt name.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from medic.grounding.store import NO_TERM, LiteralMappingStore, is_locked
from medic.mention import MEDICNE_NAMESPACE
from medic.normalization.store import NormalizationMappingStore

DRUG_GROUNDING_STORE = "mappings/drug_grounding.sssom.tsv"
DRUG_NORMALIZATION_STORE = "mappings/drug_normalization.sssom.tsv"

#: Prefix of every combination id, so a combination can never collide with a mention id.
COMBO_ID_PREFIX = "MEDICNE:combo-"


@dataclass(frozen=True)
class Ingredient:
    id: str
    label: str


def combination_id(ingredient_ids: list[str]) -> str:
    """Deterministic id for an ingredient set. Order and duplicates do not matter."""
    key = "combination\t" + "|".join(sorted(set(ingredient_ids)))
    return f"{COMBO_ID_PREFIX}{uuid.uuid5(MEDICNE_NAMESPACE, key)}"


def _sorted_labels(labels: list[str]) -> list[str]:
    return sorted(labels, key=lambda s: s.lower())


def combination_name(labels: list[str]) -> str:
    """Ingredient labels sorted and joined with " / " (the RxNorm multiple-ingredient style)."""
    return " / ".join(_sorted_labels(labels))


def combination_slug(labels: list[str]) -> str:
    """File stem: sorted labels, lowercased, non-alphanumerics collapsed to single hyphens."""
    joined = "-".join(_sorted_labels(labels)).lower()
    return re.sub(r"[^a-z0-9]+", "-", joined).strip("-")


def require_store(path: str) -> None:
    """Fail loudly when a decision store is missing.

    ``LiteralMappingStore.load`` returns quietly on a missing file, which turned a wrong
    working directory into "every ingredient is unresolvable" instead of an error.
    """
    if not Path(path).exists():
        raise FileNotFoundError(f"decision store {path} not found (working directory: "
                                f"{Path.cwd()}); run from the repository root")


class IngredientResolver:
    """Resolve a label's active-ingredient names to canonical CURIEs through the drug stores.

    Uses the decision stores only — the same rows the pipeline reads — so the curated file and
    the pipeline cannot disagree about what an ingredient name means. A name with no usable
    row is reported, not guessed: the fix is a curated row in ``drug_grounding.sssom.tsv``.
    """

    def __init__(self, grounding_path: str = DRUG_GROUNDING_STORE,
                 normalization_path: str = DRUG_NORMALIZATION_STORE):
        require_store(grounding_path)
        require_store(normalization_path)
        self._grounding = LiteralMappingStore(grounding_path, "drug")
        self._grounding.load()
        self._normalization = NormalizationMappingStore(normalization_path, "drug")
        self._normalization.load()

    def resolve(self, name: str) -> tuple[Ingredient | None, str]:
        """``(ingredient, "")`` on success, ``(None, reason)`` otherwise."""
        rows = [r for r in self._grounding.lookup(name) if r.object_id and r.object_id != NO_TERM]
        if not rows:
            return None, f"no drug grounding row maps {name!r} to a term"
        locked = [r for r in rows if is_locked(r)]
        candidates = locked or rows
        objects = {r.object_id for r in candidates}
        if len(objects) > 1:
            return None, f"{name!r} grounds to several terms: {sorted(objects)}"
        row = candidates[0]
        norm = self._normalization.lookup(row.object_id)
        if norm and norm.object_id:
            return Ingredient(norm.object_id, norm.object_label or row.object_label or ""), ""
        return Ingredient(row.object_id, row.object_label or ""), ""

    def drug_ids_for(self, text: str) -> set[str]:
        """Canonical ids the drug grounding store maps ``text`` to (empty when none)."""
        return {self.normalize(r.object_id) for r in self._grounding.lookup(text)
                if r.object_id and r.object_id != NO_TERM}

    def normalize(self, curie: str) -> str:
        norm = self._normalization.lookup(curie)
        return norm.object_id if norm and norm.object_id else curie

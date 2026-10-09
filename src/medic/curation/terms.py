"""Term lookup for curation checks, from a configurable source per ontology.

The checks need id, label, obsolescence, exact synonyms and ``is_a`` ancestry. Where they
come from is configuration, not code (``conf/curation_terms.yaml``, overridable per prefix
with ``MEDIC_TERM_SOURCE_<PREFIX>``):

- ``obo`` (default) — the ontology's own ``.obo`` release, fetched once through its PURL
  into ``cache/ontologies/`` (gitignored), parsed by the small reader below and pickled.
  Needs no services beyond the PURL.
- any OAK selector — ``sqlite:obo:mondo`` (oaklib >= 0.7.2 serves these from the
  semanticsql CDN; older releases point at a dead S3 bucket), ``ols:mondo``, a local
  ``sqlite:/path/mondo.db``, or whatever a development branch of oaklib provides.

Both answer the same questions through :class:`TermSource`, so the validator does not care.

    uv run python -m medic.curation.terms search MONDO "gastric ulcer"
    uv run python -m medic.curation.terms info MONDO:0005068 HP:0001681
"""

from __future__ import annotations

import os
import pickle
import re
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Protocol

import httpx
import yaml

from medic.curie_utils import get_prefix

ONTOLOGY_CACHE_DIR = Path("cache/ontologies")
SOURCES_CONFIG = Path("conf/curation_terms.yaml")

#: CURIE prefix -> (cache name, OBO release URL). ChEBI uses its "lite" release (labels and
#: is_a only, 55 MB) rather than the 270 MB full file.
PREFIX_SOURCES = {
    "MONDO": ("mondo", "http://purl.obolibrary.org/obo/mondo.obo"),
    "HP": ("hp", "http://purl.obolibrary.org/obo/hp.obo"),
    "UBERON": ("uberon", "http://purl.obolibrary.org/obo/uberon.obo"),
    "HsapDv": ("hsapdv", "http://purl.obolibrary.org/obo/hsapdv.obo"),
    "PATO": ("pato", "http://purl.obolibrary.org/obo/pato.obo"),
    "CHEBI": ("chebi_lite", "http://purl.obolibrary.org/obo/chebi/chebi_lite.obo"),
}

#: Bumped when TermInfo or the parser changes, so stale pickles are rebuilt.
_PICKLE_VERSION = 3


@dataclass
class TermInfo:
    id: str
    label: str
    obsolete: bool = False
    replaced_by: str = ""
    exact_synonyms: list[str] = field(default_factory=list)
    parents: list[str] = field(default_factory=list)


def _download(url: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    with httpx.stream("GET", url, follow_redirects=True, timeout=600.0) as resp:
        resp.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in resp.iter_bytes():
                fh.write(chunk)
    tmp.replace(path)


def parse_obo(lines) -> dict[str, TermInfo]:
    """``[Term]`` stanzas -> TermInfo by id. Everything the checks do not need is skipped."""
    terms: dict[str, TermInfo] = {}
    current: TermInfo | None = None
    in_term = False
    for raw in lines:
        line = raw.rstrip("\n")
        if line.startswith("["):
            if current is not None:
                terms[current.id] = current
            current, in_term = None, line == "[Term]"
            continue
        if not in_term or ": " not in line:
            continue
        tag, _, value = line.partition(": ")
        if tag == "id":
            current = TermInfo(id=value.strip(), label="")
        elif current is None:
            continue
        elif tag == "name":
            current.label = value.strip()
        elif tag == "is_obsolete" and value.strip() == "true":
            current.obsolete = True
        elif tag == "replaced_by":
            current.replaced_by = value.strip()
        elif tag == "is_a":
            current.parents.append(value.split("!")[0].split("{")[0].strip())
        elif tag == "synonym" and " EXACT" in value and value.startswith('"'):
            current.exact_synonyms.append(value[1:value.index('"', 1)])
    if current is not None:
        terms[current.id] = current
    return terms


@lru_cache(maxsize=None)
def ontology(prefix: str, cache_dir: str = str(ONTOLOGY_CACHE_DIR)) -> dict[str, TermInfo]:
    if prefix not in PREFIX_SOURCES:
        raise KeyError(f"no ontology configured for prefix {prefix}")
    name, url = PREFIX_SOURCES[prefix]
    obo = Path(cache_dir) / f"{name}.obo"
    pkl = Path(cache_dir) / f"{name}.v{_PICKLE_VERSION}.pkl"
    if not obo.exists():
        _download(url, obo)
    if pkl.exists() and pkl.stat().st_mtime >= obo.stat().st_mtime:
        with pkl.open("rb") as fh:
            return {curie: TermInfo(curie, *fields) for curie, fields in pickle.load(fh).items()}
    with obo.open(encoding="utf-8") as fh:
        parsed = parse_obo(fh)
    # Plain tuples, never TermInfo instances: a pickled class is recorded by module path, and
    # when this module runs as ``python -m medic.curation.terms`` that path is ``__main__``,
    # so a cache written by the CLI could not be read back by the validator.
    plain = {curie: (t.label, t.obsolete, t.replaced_by, t.exact_synonyms, t.parents)
             for curie, t in parsed.items()}
    with pkl.open("wb") as fh:
        pickle.dump(plain, fh, protocol=pickle.HIGHEST_PROTOCOL)
    return parsed


class TermSource(Protocol):
    """What the curation checks ask of an ontology, whatever serves it."""

    def lookup(self, curie: str) -> TermInfo | None: ...

    def ancestors(self, curie: str) -> set[str]: ...

    def exact_matches(self, prefix: str, text: str) -> tuple[str, ...]: ...

    def search(self, prefix: str, query: str, limit: int) -> list[tuple[TermInfo, str]]: ...


class OboSource:
    """The ontology's own OBO release, cached and parsed locally."""

    def lookup(self, curie: str) -> TermInfo | None:
        return ontology(get_prefix(curie)).get(curie)

    def ancestors(self, curie: str) -> set[str]:
        return set(_obo_ancestors(curie))

    def exact_matches(self, prefix: str, text: str) -> tuple[str, ...]:
        return name_index(prefix).get(text.lower(), ())

    def search(self, prefix: str, query: str, limit: int) -> list[tuple[TermInfo, str]]:
        q = query.lower().strip()
        # Whole-word matching: "stable angina" must not find "unstable angina".
        pattern = re.compile(r"(?<![a-z0-9])" + re.escape(q) + r"(?![a-z0-9])")
        hits: list[tuple[int, int, TermInfo, str]] = []
        for info in ontology(prefix).values():
            if info.obsolete or not info.id.startswith(f"{prefix}:"):
                continue
            for rank, text in ((0, info.label), *((1, syn) for syn in info.exact_synonyms)):
                low = text.lower()
                if low == q:
                    hits.append((rank, 0, info, text))
                    break
                if pattern.search(low):
                    hits.append((2, len(text), info, text))
                    break
        hits.sort(key=lambda h: (h[0], h[1], h[2].label))
        return [(info, text) for _, _, info, text in hits[:limit]]


@lru_cache(maxsize=None)
def _obo_ancestors(curie: str) -> frozenset[str]:
    terms = ontology(get_prefix(curie))
    seen: set[str] = set()
    stack = list(terms[curie].parents) if curie in terms else []
    while stack:
        parent = stack.pop()
        if parent in seen:
            continue
        seen.add(parent)
        if parent in terms:
            stack.extend(terms[parent].parents)
    return frozenset(seen)


@lru_cache(maxsize=None)
def name_index(prefix: str) -> dict[str, tuple[str, ...]]:
    """Lowercased label / exact synonym -> ids of the non-obsolete terms carrying it."""
    index: dict[str, list[str]] = {}
    for info in ontology(prefix).values():
        if info.obsolete or not info.id.startswith(f"{prefix}:"):
            continue
        for text in (info.label, *info.exact_synonyms):
            ids = index.setdefault(text.lower(), [])
            if info.id not in ids:
                ids.append(info.id)
    return {k: tuple(v) for k, v in index.items()}


class OakSource:
    """Any OAK adapter: ``sqlite:obo:mondo``, ``ols:mondo``, a local ``sqlite:`` file, ..."""

    def __init__(self, selector: str):
        from oaklib import get_adapter

        self.selector = selector
        self.adapter = get_adapter(selector)

    def lookup(self, curie: str) -> TermInfo | None:
        from oaklib.datamodels.vocabulary import DEPRECATED_PREDICATE, IS_A, TERM_REPLACED_BY

        label = self.adapter.label(curie)
        if label is None:
            return None
        meta = self.adapter.entity_metadata_map(curie) or {}
        aliases = self.adapter.entity_alias_map(curie) or {}
        parents = [o for p, o in self.adapter.outgoing_relationships(curie, predicates=[IS_A])]
        deprecated = meta.get(DEPRECATED_PREDICATE) or []
        return TermInfo(
            id=curie, label=label,
            obsolete=any(str(v).lower() == "true" for v in deprecated),
            replaced_by=(meta.get(TERM_REPLACED_BY) or [""])[0],
            exact_synonyms=list(aliases.get("oio:hasExactSynonym", [])),
            parents=parents,
        )

    def ancestors(self, curie: str) -> set[str]:
        from oaklib.datamodels.vocabulary import IS_A

        return set(self.adapter.ancestors(curie, predicates=[IS_A], reflexive=False))

    def exact_matches(self, prefix: str, text: str) -> tuple[str, ...]:
        from oaklib.datamodels.search import SearchConfiguration
        from oaklib.datamodels.search_datamodel import SearchProperty

        config = SearchConfiguration(
            is_complete=True, properties=[SearchProperty.LABEL, SearchProperty.ALIAS])
        return tuple(c for c in self.adapter.basic_search(text, config=config)
                     if c.startswith(f"{prefix}:"))

    def search(self, prefix: str, query: str, limit: int) -> list[tuple[TermInfo, str]]:
        out = []
        for curie in self.adapter.basic_search(query):
            if not curie.startswith(f"{prefix}:"):
                continue
            info = self.lookup(curie)
            if info and not info.obsolete:
                out.append((info, info.label))
            if len(out) >= limit:
                break
        return out


def source_selector(prefix: str) -> str:
    """The configured source for a prefix: env override, then config file, then ``obo``."""
    env = os.environ.get(f"MEDIC_TERM_SOURCE_{prefix.upper()}")
    if env:
        return env
    if SOURCES_CONFIG.exists():
        conf = yaml.safe_load(SOURCES_CONFIG.read_text()) or {}
        chosen = (conf.get("sources") or {}).get(prefix) or conf.get("default")
        if chosen:
            return chosen
    return "obo"


@lru_cache(maxsize=None)
def source(prefix: str) -> TermSource:
    selector = source_selector(prefix)
    return OboSource() if selector == "obo" else OakSource(selector)


def lookup(curie: str) -> TermInfo | None:
    """The term, or None if the id does not exist. Raises if the source cannot be loaded."""
    return source(get_prefix(curie)).lookup(curie)


def ancestors(curie: str) -> set[str]:
    """Every ``is_a`` ancestor of ``curie`` within its own ontology (not including itself)."""
    return source(get_prefix(curie)).ancestors(curie)


def exact_matches(prefix: str, text: str) -> tuple[str, ...]:
    """Non-obsolete ids whose label or exact synonym equals ``text`` (case-insensitive)."""
    return source(prefix).exact_matches(prefix, text)


def search(prefix: str, query: str, limit: int = 15) -> list[tuple[TermInfo, str]]:
    """Terms whose label or an exact synonym contains ``query`` as whole words (case-insensitive)."""
    return source(prefix).search(prefix, query, limit)


def _print_info(curie: str) -> bool:
    info = lookup(curie)
    if info is None:
        print(f"{curie}: not found")
        return False
    print(f"{info.id}\t{info.label}")
    if info.obsolete:
        print(f"  OBSOLETE{'; replaced by ' + info.replaced_by if info.replaced_by else ''}")
    for syn in info.exact_synonyms:
        print(f"  exact synonym: {syn}")
    for parent in info.parents:
        p = lookup(parent) if ":" in parent else None
        print(f"  is_a: {parent} {p.label if p else ''}")
    return True


def main(argv: list[str] | None = None) -> int:
    """``search <PREFIX> <query>`` or ``info <CURIE> [<CURIE> ...]``.

    Prefixes: MONDO, HP, UBERON, HsapDv, PATO, CHEBI. The source for each is set in
    conf/curation_terms.yaml or MEDIC_TERM_SOURCE_<PREFIX>.
    """
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) >= 3 and argv[0] == "search":
        for info, text in search(argv[1], " ".join(argv[2:])):
            via = "" if text == info.label else f"   (exact synonym: {text})"
            print(f"{info.id}\t{info.label}{via}")
        return 0
    if len(argv) >= 2 and argv[0] == "info":
        found = [_print_info(curie) for curie in argv[1:]]
        return 0 if all(found) else 1
    print(main.__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

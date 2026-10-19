"""The pinned ontology releases behind the lexical index (``conf/grounding_sources.yaml``).

One reader for the three places that need the pins: the download recipe (which URL to
fetch), the index build (which versions to stamp into the index) and the KGX content
metadata (which versions a consumer was grounded against). A pin lives in the config, not
in code, so bumping a release is a one-line diff that the build then carries through.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_CONF = "conf/grounding_sources.yaml"


@dataclass(frozen=True)
class PinnedOntology:
    name: str
    version: str
    url: str


def load_sources(conf_path: str | Path = DEFAULT_CONF) -> dict:
    with open(conf_path) as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{conf_path}: expected a mapping at the top level")
    return data


def pinned_ontologies(conf_path: str | Path = DEFAULT_CONF) -> dict[str, PinnedOntology]:
    """``name -> PinnedOntology`` for every entry under ``ontologies``.

    Every entry must carry a non-empty ``version`` and ``url``: a pin with either missing
    is the unpinned state this exists to prevent, so it is an error, not a default.
    """
    raw = load_sources(conf_path).get("ontologies") or {}
    out: dict[str, PinnedOntology] = {}
    for name, entry in raw.items():
        entry = entry or {}
        version = str(entry.get("version") or "").strip()
        url = str(entry.get("url") or "").strip()
        if not version or not url:
            raise ValueError(f"ontology {name!r} is not pinned: version and url are required")
        out[name] = PinnedOntology(name=name, version=version, url=url)
    return out


def ontology_for_source(entry: dict) -> str | None:
    """The pinned-ontology name a source entry refers to (``ontology:``), if any."""
    name = entry.get("ontology")
    return str(name) if name else None


def main(argv: list[str] | None = None) -> None:
    """Print ``<name> <url>`` per pinned ontology, for the download recipe to loop over."""
    import argparse

    parser = argparse.ArgumentParser(description="list pinned grounding ontologies")
    parser.add_argument("--conf", default=DEFAULT_CONF)
    args = parser.parse_args(argv)
    for pin in pinned_ontologies(args.conf).values():
        print(f"{pin.name} {pin.url}")


if __name__ == "__main__":
    main()

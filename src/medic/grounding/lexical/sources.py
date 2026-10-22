"""The pinned ontology releases behind the lexical index (``conf/grounding_sources.yaml``).

One reader for the three places that need the pins: the download recipe (which URL to
fetch), the index build (which versions to stamp into the index) and the KGX content
metadata (which versions a consumer was grounded against). A pin lives in the config, not
in code, so bumping a release is a one-line diff that the build then carries through.

:func:`release_in_file` exists so the pin is *checked* rather than merely declared. A pin
that names a release the build did not actually read is a false reproducibility claim —
strictly worse than admitting the version is unknown — and the way to get one is ordinary:
bump the pin, forget to re-download, and the stamped metadata silently disagrees with the
file on disk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_CONF = "conf/grounding_sources.yaml"

#: The graph-level ``"version"`` of an OBO Graph JSON, which is a version IRI:
#: ``.../obo/mondo/releases/2026-07-06/mondo.owl`` or ``.../obo/chebi/253/chebi.owl``.
#: Matched against the raw bytes rather than a parsed document: chebi.json is 534MB and
#: the key sits in the first few KB, so a bounded scan answers in milliseconds.
_VERSION_IRI = re.compile(rb'"version"\s*:\s*"([^"]{1,300})"')
_SCAN_BYTES = 1 << 22


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


def release_in_file(path: str | Path, scan_bytes: int = _SCAN_BYTES) -> str | None:
    """The release token of an OBO Graph JSON on disk, or ``None`` if it declares none.

    The token is the path segment before the filename of the graph's version IRI, which is
    what the pins name: ``2026-07-06`` for mondo, ``253`` for ChEBI. ``None`` means the file
    carries no version at all — unverifiable, not a mismatch, so the caller decides.
    """
    p = Path(path)
    if not p.exists():
        return None
    with open(p, "rb") as fh:
        head = fh.read(scan_bytes)
    m = _VERSION_IRI.search(head)
    if not m:
        return None
    parts = [seg for seg in m.group(1).decode("utf-8", "replace").split("/") if seg]
    return parts[-2] if len(parts) >= 2 else None


def check_pin(pin: PinnedOntology, path: str | Path) -> None:
    """Raise if the file at ``path`` is not the release ``pin`` names.

    A file with no declared version passes: ChEBI's archive JSON and a robot-converted OWL
    are both legitimately version-less, and refusing them would stop the build over
    something the pin cannot settle either way.
    """
    found = release_in_file(path)
    if found is None or found == pin.version:
        return
    raise ValueError(
        f"{path} is {pin.name} release {found!r}, but conf pins {pin.version!r}. "
        f"Either re-download it (`just download-grounding-ontologies`) or correct the pin "
        f"— do not stamp a release the index was not built from (#94)."
    )


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

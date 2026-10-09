"""One anchor for every path the curation tools read or write.

The tools used to resolve ``mappings/``, ``kb/combinations/``, ``history/`` and their caches
against the working directory. Run from anywhere else, the decision stores loaded empty,
every ingredient looked unresolvable, history looked missing, and labels were downloaded into
a stray directory — all without an error. The command-line entry points now resolve the
arguments the user typed, then move to the repository root, so relative paths below always
mean the same thing.
"""

from __future__ import annotations

import os
from pathlib import Path

#: src/medic/curation/paths.py -> repository root.
REPO_ROOT = Path(__file__).resolve().parents[3]


def enter_repo_root(paths: list[str] | None = None) -> list[Path]:
    """Resolve ``paths`` against the caller's directory, then chdir to the repository root.

    Returns the paths relative to the root when they lie inside it (so findings and history
    targets read ``kb/combinations/x.yaml``), absolute otherwise.
    """
    resolved = [Path(p).resolve() for p in paths or []]
    os.chdir(REPO_ROOT)
    out = []
    for p in resolved:
        try:
            out.append(p.relative_to(REPO_ROOT))
        except ValueError:
            out.append(p)
    return out

"""Simple JSON-file cache for enrichment results. GitHub-diffable."""

import atexit
import json
import logging
import weakref
from pathlib import Path

logger = logging.getLogger(__name__)

#: Every live cache, so a caller can flush them all without naming any of them.
#:
#: `put` only mutates an in-memory dict — persistence happens in `flush` — so a cache
#: nobody flushes is silently re-queried on the next build. The DailyMed teardown flushed
#: two of its three by name, and the third re-ran 2,484 non-deterministic LLM calls on
#: every build for as long as nobody noticed (#57). Flushing one more by name would have
#: left the same trap set for the fourth, so registration is automatic instead.
#:
#: Weak, so a cache that goes out of scope — every cache a test builds — is not
#: resurrected and written by some later flush elsewhere in the process.
_REGISTRY: weakref.WeakSet = weakref.WeakSet()


def live_caches() -> list["EnrichmentCache"]:
    """Every cache still alive, for tests and diagnostics."""
    return list(_REGISTRY)


def flush_all() -> None:
    """Persist every live cache. Untouched caches write nothing, as `flush` no-ops."""
    for cache in list(_REGISTRY):
        cache.flush()


def _flush_all_at_exit() -> None:
    """Last-resort flush, so answers that were paid for are never discarded.

    `put` is in-memory and the ingester checkpoints only every 100 new entries, so until
    this existed every exit path that was not a clean `main()` teardown threw away up to 99
    LLM answers — and a script that called the extractors directly, as the prompt smoke
    tests do, lost every one of them and paid again on the next run.

    Best-effort by construction: raising inside an `atexit` handler prints a bare traceback
    over whatever the run was reporting, and a failed flush must not turn a finished build
    into a failed one. A partial write is still strictly better than none, because entries
    are independent of each other.
    """
    try:
        flush_all()
    except Exception:  # pragma: no cover - shutdown path
        logger.warning("could not flush enrichment caches at exit", exc_info=True)


atexit.register(_flush_all_at_exit)


class EnrichmentCache:
    """Sorted, deterministic JSON cache for enrichment results."""

    def __init__(self, cache_path: Path):
        self._path = cache_path
        self._data: dict | None = None
        _REGISTRY.add(self)

    def _load(self) -> dict:
        if self._data is not None:
            return self._data
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text())
            except (json.JSONDecodeError, OSError):
                self._data = {}
        else:
            self._data = {}
        return self._data

    def get(self, key: str) -> dict | None:
        return self._load().get(key)

    def put(self, key: str, value: dict) -> None:
        self._load()[key] = value

    def flush(self) -> None:
        if self._data is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Sorted keys, no timestamps, deterministic output
        self._path.write_text(
            json.dumps(self._data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        )

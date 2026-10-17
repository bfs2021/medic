"""Paid LLM answers survive any exit path, not just a clean teardown.

`EnrichmentCache.put` is in-memory; the DailyMed ingester checkpoints every 100 new
entries. So before the `atexit` hook, three things threw away answers that had already
been paid for: a crash or interrupt between checkpoints (up to 99), any exit path that
skipped `main()`'s teardown, and any script calling the extractors directly — the prompt
smoke tests lost all six labels' answers and would have paid again.

The registry is weak on purpose, so this also checks the hook does not resurrect a cache
that went out of scope and overwrite a real file with a dead test's contents.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

from medic.enrichment.cache import EnrichmentCache, _flush_all_at_exit, flush_all


def test_a_put_is_persisted_by_the_exit_hook(tmp_path):
    """The hook is what the real run relies on, so call the hook, not `flush`."""
    path = tmp_path / "answers.json"
    cache = EnrichmentCache(path)
    cache.put("k", {"raw": "target: porphyria :: porphyria"})
    assert not path.exists(), "put must stay in memory until a flush"

    _flush_all_at_exit()

    assert json.loads(path.read_text())["k"]["raw"].startswith("target: porphyria")


def test_the_hook_runs_on_real_interpreter_exit(tmp_path):
    """End to end in a child process: no explicit flush anywhere, and the process exits
    normally. This is the path a smoke script takes."""
    path = tmp_path / "child.json"
    script = textwrap.dedent(f"""
        from pathlib import Path
        from medic.enrichment.cache import EnrichmentCache
        c = EnrichmentCache(Path({str(path)!r}))
        c.put("paid", {{"raw": "expensive"}})
    """)
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(path.read_text()) == {"paid": {"raw": "expensive"}}


def test_the_hook_does_not_raise_when_a_path_is_unwritable(tmp_path):
    """A failed flush must not turn a finished build into a failed one, and must not print
    a bare traceback over the run's own report."""
    unwritable = tmp_path / "nope"
    unwritable.write_text("i am a file, not a directory")
    cache = EnrichmentCache(unwritable / "sub" / "c.json")
    cache.put("k", {"raw": "v"})

    _flush_all_at_exit()  # must not raise


def test_an_untouched_cache_writes_nothing(tmp_path):
    """`flush` no-ops when nothing was loaded, so the hook cannot create empty files for
    every cache the process happened to construct."""
    path = tmp_path / "untouched.json"
    EnrichmentCache(path)
    flush_all()
    assert not path.exists()

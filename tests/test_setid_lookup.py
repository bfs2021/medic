"""The legacy-path setid lookup picks the most recent SPL by *date*, not by string.

`lookup_setid` decides which DailyMed document an FDA evidence item deep-links to. It sorted
the endpoint's `published_date` as a raw string, and the endpoint formats it "Oct 14, 2026" —
so the sort ran alphabetically by month name and "Sep 3, 2026" ranked above "Oct 14, 2026",
"Apr" above everything and "Dec" below "Jan" of the same year.

Found by reading #98's review of the acquirer, not by testing this. There the same bug was
latent, because every hit was kept regardless of order; here one entry wins and the rest are
discarded, so it was live and silently wrong for any drug whose labels span months that sort
the wrong way.
"""

from __future__ import annotations

import httpx
import pytest

from medic.enrichment.cache import EnrichmentCache
from medic.ingest.dailymed import setid_lookup as sl


@pytest.fixture
def _cache(tmp_path, monkeypatch):
    cache = EnrichmentCache(tmp_path / "setids.json")
    monkeypatch.setattr(sl, "_cache", cache)
    return cache


def _answer(monkeypatch, data: list[dict]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": data})

    real_client = httpx.Client

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(sl.httpx, "Client", client)


def test_the_newest_label_wins_across_a_month_boundary(_cache, monkeypatch):
    """September must not beat October. Under the old string sort it did."""
    _answer(monkeypatch, [
        {"setid": "sep", "title": "A", "published_date": "Sep 3, 2026"},
        {"setid": "oct", "title": "B", "published_date": "Oct 14, 2026"},
    ])
    assert sl.lookup_setid("widgetol") == "oct"


def test_the_newest_label_wins_across_a_year_boundary(_cache, monkeypatch):
    """"Dec 1, 2025" sorts above "Jan 2, 2026" as a string, and below it as a date."""
    _answer(monkeypatch, [
        {"setid": "dec", "title": "A", "published_date": "Dec 1, 2025"},
        {"setid": "jan", "title": "B", "published_date": "Jan 2, 2026"},
    ])
    assert sl.lookup_setid("widgetol") == "jan"


def test_an_unparseable_date_loses_rather_than_wins(_cache, monkeypatch):
    """A hit with no usable date ranks last (`date.min`), so a garbled field cannot
    displace a real label — the failure mode of defaulting the other way."""
    _answer(monkeypatch, [
        {"setid": "junk", "title": "A", "published_date": "whenever"},
        {"setid": "real", "title": "B", "published_date": "Jan 2, 2020"},
    ])
    assert sl.lookup_setid("widgetol") == "real"


def test_no_results_is_cached_as_such(_cache, monkeypatch):
    _answer(monkeypatch, [])
    assert sl.lookup_setid("widgetol") is None
    assert _cache.get("widgetol")["match"] == "no_results"

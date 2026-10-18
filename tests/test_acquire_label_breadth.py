"""Acquisition keeps every SPL per drug, not just the newest (Phase 4 recall).

`_most_recent_setid` queried the SPL list endpoint with ``pagesize=20``, sorted by published
date and returned **one** setid, discarding the other 19. A drug's indications are split
across manufacturers and formulations, so that kept whichever subset the newest label
happened to state:

* `lansoprazole` had 2 SPLs on disk and asserted exactly one indication, *Helicobacter pylori
  infection*. GERD, duodenal ulcer and erosive esophagitis were never in any text fetched.
* propranolol, chlorpromazine, varenicline and dalteparin had **one label each**.

The external label audit lists all of those as correct edges MeDIC no longer asserts, and
this is why — not extraction, not grounding.

Cost was measured before changing it: 12 drugs, 221 SPLs, 118 distinct indication sections,
so ~9.8 extraction calls per drug rather than 1. Roughly half of a drug's SPLs repeat
another's indication text verbatim and extraction is cached on the section-text hash, so it
is ~10x rather than the 20x the page size implies.
"""

from __future__ import annotations

import json

import httpx

from medic.ingest.dailymed import acquire as aq


def _client(payload: dict) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


_THREE = {"data": [
    {"setid": "old", "published_date": "2020-01-01"},
    {"setid": "newest", "published_date": "2026-01-01"},
    {"setid": "middle", "published_date": "2023-01-01"},
]}


def test_every_setid_is_returned_newest_first():
    with _client(_THREE) as c:
        assert aq._setids_for_drug(c, "whatever", keep=20) == ["newest", "middle", "old"]


def test_keep_limits_the_list():
    with _client(_THREE) as c:
        assert aq._setids_for_drug(c, "whatever", keep=2) == ["newest", "middle"]


def test_the_single_setid_helper_still_returns_the_newest():
    """Kept so a caller that genuinely wants one label is not forced to slice a list."""
    with _client(_THREE) as c:
        assert aq._most_recent_setid(c, "whatever") == "newest"


def test_duplicate_setids_are_collapsed():
    payload = {"data": [{"setid": "a", "published_date": "2026-01-01"},
                        {"setid": "a", "published_date": "2025-01-01"}]}
    with _client(payload) as c:
        assert aq._setids_for_drug(c, "whatever", keep=20) == ["a"]


def test_no_results_yields_an_empty_list():
    with _client({"data": []}) as c:
        assert aq._setids_for_drug(c, "whatever", keep=20) == []


def test_a_network_error_yields_an_empty_list_not_an_exception():
    """Acquisition runs over thousands of names; one bad lookup must not end the run."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        assert aq._setids_for_drug(c, "whatever", keep=20) == []


def test_the_default_keeps_more_than_one():
    """Pins the decision itself: this was 1 by construction, and that was the recall bug."""
    assert aq.DEFAULT_LABELS_PER_DRUG > 1


def test_an_old_single_setid_cache_entry_is_re_resolved(tmp_path, monkeypatch):
    """The setid cache was written one-per-drug. An entry in that shape cannot answer "what
    are this drug's other labels", so it must be re-resolved rather than trusted — otherwise
    every drug already cached silently keeps its single label and the change does nothing.
    """
    from medic.enrichment.cache import EnrichmentCache

    cache = EnrichmentCache(tmp_path / "setids.json")
    cache.put("aspirin", {"setid": "only-one"})
    monkeypatch.setattr(aq, "_get_cache", lambda: cache)
    monkeypatch.setattr(aq, "_usa_drug_names", lambda *a, **k: ["aspirin"])
    monkeypatch.setattr(aq, "_setids_for_drug",
                        lambda *a, **k: ["newest", "middle", "old"])
    monkeypatch.setattr(aq, "_download_spl_xml",
                        lambda client, setid, out_path: out_path.write_bytes(b"x") or True)

    counts = aq.acquire(data_dir=tmp_path / "raw", delay=0, labels_per_drug=20)

    assert counts["resolved"] == 3, counts
    assert json.loads((tmp_path / "setids.json").read_text())["aspirin"]["setids"] == [
        "newest", "middle", "old"]


def test_a_single_label_run_still_trusts_the_old_cache(tmp_path, monkeypatch):
    """With labels_per_drug=1 there is nothing to gain from re-resolving, so the existing
    cache is still a hit and the run costs no API calls."""
    from medic.enrichment.cache import EnrichmentCache

    cache = EnrichmentCache(tmp_path / "setids.json")
    cache.put("aspirin", {"setid": "only-one"})
    monkeypatch.setattr(aq, "_get_cache", lambda: cache)
    monkeypatch.setattr(aq, "_usa_drug_names", lambda *a, **k: ["aspirin"])

    def _boom(*a, **k):
        raise AssertionError("should not have re-resolved")

    monkeypatch.setattr(aq, "_setids_for_drug", _boom)
    monkeypatch.setattr(aq, "_download_spl_xml",
                        lambda client, setid, out_path: out_path.write_bytes(b"x") or True)

    counts = aq.acquire(data_dir=tmp_path / "raw", delay=0, labels_per_drug=1)
    assert counts["cached_hit"] == 1
    assert counts["resolved"] == 1


# ---------------------------------------------------------------------------
# Query names: every candidate, generic first
# ---------------------------------------------------------------------------
def test_both_the_generic_label_and_the_source_strings_are_queried(tmp_path):
    """The drug list holds product-style strings that DailyMed's `drug_name` index does not
    match — "Lansoprazole Enteric-Coated Capsules" returns nothing where "lansoprazole"
    returns 20 setids. Taking the first `source_ingredients` string and stopping meant 1,074
    of 2,591 names (41%) resolved to no SPL at all, and lansoprazole's labels were never
    requested. So broadening to 20 setids per *name* recovered its indications on paper and
    not in fact.

    The generic label is emitted first because it is what DailyMed indexes; the source
    strings follow, because a brand or salt spelling occasionally hits where the generic
    misses.
    """
    import yaml

    from medic.ingest.dailymed.acquire import _usa_drug_names

    path = tmp_path / "drug_list.yaml"
    path.write_text(yaml.dump({"drugs": [{
        # `drug_label` reads identity.resolved_label, not a flat `label`.
        "identity": {"resolved_id": "CHEBI:6375", "resolved_label": "lansoprazole"},
        "source_ingredients": ["Lansoprazole Enteric-Coated Capsules"],
        "approvals": [{"authority": "FDA", "approved": True}],
    }]}))

    names = _usa_drug_names(path)
    assert "lansoprazole" in names, names
    assert "Lansoprazole Enteric-Coated Capsules" in names, names
    assert names.index("lansoprazole") < names.index(
        "Lansoprazole Enteric-Coated Capsules"), "generic name must be tried first"


def test_a_name_is_not_queried_twice(tmp_path):
    """Each extra name is an extra HTTP request, so duplicates must collapse — including
    across drugs, and case-insensitively, since the query is case-insensitive anyway."""
    import yaml

    from medic.ingest.dailymed.acquire import _usa_drug_names

    path = tmp_path / "drug_list.yaml"
    approved = [{"authority": "FDA", "approved": True}]
    path.write_text(yaml.dump({"drugs": [
        {"identity": {"resolved_id": "CHEBI:1", "resolved_label": "aspirin"},
         # Differs from the label only in case: the same query, so it must not be emitted.
         "source_ingredients": ["ASPIRIN"], "approvals": approved},
        {"identity": {"resolved_id": "CHEBI:2", "resolved_label": "ibuprofen"},
         # Shared with the first drug in spirit — a repeat across drugs must also collapse.
         "source_ingredients": ["aspirin", "IBUPROFEN SODIUM"], "approvals": approved},
    ]}))
    names = _usa_drug_names(path)
    assert sorted(n.lower() for n in names) == [
        "aspirin", "ibuprofen", "ibuprofen sodium"], names

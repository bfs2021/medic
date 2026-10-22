"""Acquisition keeps many SPLs per drug and queries every name, not just the best one.

Companion to `test_dailymed_acquire.py`, which covers *which* label is chosen (#98's
ranking). This file covers *how many* and *under how many names* — the coverage half, which
ranking does not address and which is easy to lose when adopting a selection fix:

* `lansoprazole` had 2 SPLs on disk and asserted exactly one indication, *Helicobacter
  pylori infection*. GERD, duodenal ulcer and erosive esophagitis were never in any text
  fetched.
* propranolol, chlorpromazine, varenicline and dalteparin had **one label each**.
* separately, 1,074 of 2,591 names (41%) resolved to no SPL at all, because the drug list
  holds product-style strings like "Lansoprazole Enteric-Coated Capsules" that DailyMed's
  `drug_name` index does not match.

The external label audit lists all of those as correct edges MeDIC no longer asserts, and
this is why — not extraction, not grounding.

Cost was measured before changing it: 12 drugs, 221 SPLs, 118 distinct indication sections,
so ~9.8 extraction calls per drug rather than 1. Roughly half of a drug's SPLs repeat
another's indication text verbatim and extraction is cached on the section-text hash, so it
is ~10x rather than the 20x the page size implies.
"""

from __future__ import annotations

import httpx
import yaml

from medic.ingest.dailymed import acquire as aq


def _spl(setid: str, with_indications: bool = True) -> bytes:
    section = (b'<section><code code="34067-9"/><text>indicated for epilepsy</text></section>'
               if with_indications else b"")
    return (b'<?xml version="1.0"?><document><setId root="' + setid.encode() + b'"/>'
            + section + b"</document>")


def _hit(setid: str, title: str, published: str = "Jan 01, 2026") -> dict:
    return {"setid": setid, "title": title, "published_date": published, "spl_version": 1}


def _one_drug_list(tmp_path, label="Widgetol", ingredients=()):
    path = tmp_path / "drug_list.yaml"
    path.write_text(yaml.safe_dump({"drugs": [{
        "identity": {"resolved_id": "CHEBI:1", "resolved_label": label},
        "source_ingredients": list(ingredients),
        "approvals": [{"authority": "FDA", "approved": True}],
    }]}))
    return path


def _serving(hits: list[dict]):
    """A transport that answers every list query with *hits* and every XML with a label."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/spls.json"):
            return httpx.Response(200, json={"data": hits})
        setid = request.url.path.rsplit("/", 1)[-1].removesuffix(".xml")
        return httpx.Response(200, content=_spl(setid))
    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# How many labels
# ---------------------------------------------------------------------------
def test_the_default_keeps_many_labels_not_one_or_three():
    """Pins the decision itself.

    This was 1 by construction, which was the recall bug. #98 proposed 3, which fixes the
    *selection* fault (one good label beats one arbitrary one) but not the coverage one: a
    drug's indications are split across labels rather than ranked among them, so three of a
    well-ranked pool still misses lansoprazole's GERD.
    """
    assert aq.DEFAULT_LABELS_PER_DRUG >= 20


def test_every_distinct_label_is_kept_up_to_the_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(aq, "_cache", aq.EnrichmentCache(tmp_path / "c.json"))
    hits = [_hit("tab", "WIDGETOL TABLET [A]"),
            _hit("cap", "WIDGETOL CAPSULE [B]"),
            _hit("inj", "WIDGETOL INJECTION [C]")]

    aq.acquire(drug_list_path=_one_drug_list(tmp_path), data_dir=tmp_path / "raw",
               delay=0, labels_per_drug=20, transport=_serving(hits))

    # Sorted: all three are systemic forms published on the same day, so the ranking's last
    # tie-break (setid) decides their order, and which of three equals comes first is not
    # what this test is about.
    assert sorted(aq.cached_setids(aq._cache.get("widgetol"))) == ["cap", "inj", "tab"]


def test_the_limit_is_honoured(tmp_path, monkeypatch):
    monkeypatch.setattr(aq, "_cache", aq.EnrichmentCache(tmp_path / "c.json"))
    hits = [_hit("tab", "WIDGETOL TABLET [A]"),
            _hit("cap", "WIDGETOL CAPSULE [B]"),
            _hit("inj", "WIDGETOL INJECTION [C]")]

    aq.acquire(drug_list_path=_one_drug_list(tmp_path), data_dir=tmp_path / "raw",
               delay=0, labels_per_drug=2, transport=_serving(hits))

    assert len(aq.cached_setids(aq._cache.get("widgetol"))) == 2


def test_duplicate_setids_are_collapsed(tmp_path, monkeypatch):
    """The same label comes back under both doctype queries and under several names."""
    monkeypatch.setattr(aq, "_cache", aq.EnrichmentCache(tmp_path / "c.json"))
    hits = [_hit("a", "WIDGETOL TABLET [A]"), _hit("a", "WIDGETOL TABLET [A]")]

    aq.acquire(drug_list_path=_one_drug_list(tmp_path, ingredients=["widgetol sodium"]),
               data_dir=tmp_path / "raw", delay=0, transport=_serving(hits))

    assert aq.cached_setids(aq._cache.get("widgetol")) == ["a"]


def test_no_results_is_recorded_rather_than_retried_forever(tmp_path, monkeypatch):
    monkeypatch.setattr(aq, "_cache", aq.EnrichmentCache(tmp_path / "c.json"))
    counts = aq.acquire(drug_list_path=_one_drug_list(tmp_path),
                        data_dir=tmp_path / "raw", delay=0, transport=_serving([]))
    assert counts["no_spl"] == 1
    assert aq._cache.get("widgetol")["match"] == "no_results"


def test_a_network_error_is_not_an_exception(tmp_path, monkeypatch):
    """Acquisition runs over thousands of names; one bad lookup must not end the run."""
    monkeypatch.setattr(aq, "_cache", aq.EnrichmentCache(tmp_path / "c.json"))

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    counts = aq.acquire(drug_list_path=_one_drug_list(tmp_path),
                        data_dir=tmp_path / "raw", delay=0,
                        transport=httpx.MockTransport(handler))
    assert counts["no_spl"] == 1


# ---------------------------------------------------------------------------
# How many names
# ---------------------------------------------------------------------------
def test_both_the_generic_label_and_the_source_strings_are_queried(tmp_path):
    """The drug list holds product-style strings that DailyMed's `drug_name` index does not
    match — "Lansoprazole Enteric-Coated Capsules" returns nothing where "lansoprazole"
    returns labels. Taking the first `source_ingredients` string and stopping meant 1,074 of
    2,591 names (41%) resolved to no SPL at all, and lansoprazole's labels were never
    requested. So broadening the per-name keep count recovered its indications on paper and
    not in fact.

    The generic label comes first because it is what DailyMed indexes, and because it is
    then the drug's cache key; the source strings follow, because a brand or salt spelling
    occasionally hits where the generic misses.
    """
    names = aq._usa_drug_names(_one_drug_list(
        tmp_path, label="lansoprazole",
        ingredients=["Lansoprazole Enteric-Coated Capsules"]))

    assert names == [["lansoprazole", "Lansoprazole Enteric-Coated Capsules"]], names


def test_a_name_is_not_queried_twice(tmp_path):
    """Each extra name is two extra HTTP requests, so duplicates must collapse — including
    across drugs, and case-insensitively, since the query is case-insensitive anyway."""
    path = tmp_path / "drug_list.yaml"
    approved = [{"authority": "FDA", "approved": True}]
    path.write_text(yaml.safe_dump({"drugs": [
        {"identity": {"resolved_id": "CHEBI:1", "resolved_label": "aspirin"},
         # Differs from the label only in case: the same query, so it must not be emitted.
         "source_ingredients": ["ASPIRIN"], "approvals": approved},
        {"identity": {"resolved_id": "CHEBI:2", "resolved_label": "ibuprofen"},
         # Shared with the first drug — a repeat across drugs must also collapse.
         "source_ingredients": ["aspirin", "IBUPROFEN SODIUM"], "approvals": approved},
    ]}))

    assert aq._usa_drug_names(path) == [["aspirin"], ["ibuprofen", "IBUPROFEN SODIUM"]]


def test_a_stale_single_setid_cache_entry_is_re_resolved(tmp_path, monkeypatch):
    """The cache was written one-setid-per-drug, and later as a flat `setids` list. Neither
    shape can answer "what are this drug's other labels, chosen from a human-only pool", so
    both are re-resolved rather than trusted — otherwise every drug already cached silently
    keeps its old selection and the change does nothing.

    This is now unconditional. An earlier version trusted the old entry when
    `labels_per_drug == 1`, on the grounds that there was nothing to gain from re-resolving;
    that stopped being true once the pool itself changed, since a single old setid may be
    the veterinary label the doctype filter now excludes.
    """
    cache = aq.EnrichmentCache(tmp_path / "c.json")
    cache.put("widgetol", {"setid": "vet-old"})
    cache.put("otherol", {"setids": ["also-old"]})
    monkeypatch.setattr(aq, "_cache", cache)

    counts = aq.acquire(drug_list_path=_one_drug_list(tmp_path), data_dir=tmp_path / "raw",
                        delay=0, transport=_serving([_hit("tab", "WIDGETOL TABLET [A]")]))

    assert counts["stale_entry"] == 1
    assert aq.cached_setids(aq._cache.get("widgetol")) == ["tab"]
    assert not (tmp_path / "raw" / "vet-old.xml").exists()

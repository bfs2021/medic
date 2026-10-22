"""DailyMed label selection (#98).

The acquirer used to keep the single most recently published SPL among 20
hits, which picked extended-release hydromorphone (no renal colic), liposomal
doxorubicin (no bone sarcoma), an ipratropium/albuterol combination, a
veterinary ivermectin label and flurbiprofen eye drops. These tests pin the
ranking on synthetic list-endpoint hits (``title`` / ``published_date`` /
``spl_version`` / ``setid`` plus the ``doctype`` the acquirer attaches) with
no network, and the cache shapes the acquirer reads and writes.
"""

from __future__ import annotations

import json
from datetime import date

import httpx
import yaml

from medic.ingest.dailymed import acquire as acq
from medic.ingest.dailymed.acquire import (
    DOCTYPE_HUMAN_OTC,
    DOCTYPE_HUMAN_RX,
    ROUTE_LOCAL,
    ROUTE_SYSTEMIC,
    cached_setids,
    has_indications_section,
    is_ranked_entry,
    label_features,
    published_date,
    rank_label_candidates,
    select_labels,
)

RX = DOCTYPE_HUMAN_RX
OTC = DOCTYPE_HUMAN_OTC
ANIMAL = "50577-6"


def hit(setid: str, title: str, published: str, doctype: str | None = RX, version: int = 1) -> dict:
    h = {"setid": setid, "title": title, "published_date": published, "spl_version": version}
    if doctype is not None:
        h["doctype"] = doctype
    return h


def setids(hits: list[dict]) -> list[str]:
    return [h["setid"] for h in hits]


def assert_order_independent(hits: list[dict], drug: str) -> list[dict]:
    """The ranking must not depend on the order the API returned the hits in."""
    forward = rank_label_candidates(hits, drug)
    assert setids(rank_label_candidates(list(reversed(hits)), drug)) == setids(forward)
    return forward


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #

def test_hydromorphone_keeps_ir_and_er_with_ir_first():
    hits = [
        hit("er", "HYDROMORPHONE HYDROCHLORIDE TABLET, FILM COATED, EXTENDED RELEASE [X]", "Aug 20, 2026"),
        hit("ir", "HYDROMORPHONE HYDROCHLORIDE TABLET [SPECGX LLC]", "Jul 06, 2026"),
    ]
    ranked = assert_order_independent(hits, "hydromorphone hydrochloride")
    assert setids(ranked) == ["ir", "er"]  # both kept; plain formulation first despite being older


def test_doxorubicin_conventional_before_liposomal():
    lipo = hit("lipo", "DOXORUBICIN HYDROCHLORIDE INJECTION, SUSPENSION, LIPOSOMAL [DR. REDDY'S]", "Sep 24, 2026")
    hits = [
        lipo,
        hit("doxil", "DOXIL (DOXORUBICIN HYDROCHLORIDE) INJECTION, SUSPENSION, LIPOSOMAL [BAXTER]", "Jun 03, 2026"),
        hit("conv", "DOXORUBICIN HYDROCHLORIDE INJECTION, SOLUTION [PFIZER]", "May 07, 2026"),
    ]
    assert label_features(lipo, "doxorubicin")["modified"] is True
    ranked = assert_order_independent(hits, "doxorubicin hydrochloride")
    assert setids(ranked)[0] == "conv"
    assert set(setids(ranked)) == {"conv", "lipo", "doxil"}


def test_ivermectin_veterinary_excluded():
    hits = [
        hit("vet-typed", "IVOMEC (IVERMECTIN) INJECTION, SOLUTION [BOEHRINGER ANIMAL HEALTH]", "Oct 14, 2026", doctype=ANIMAL),
        hit("vet-title", "ECOMECTIN CATTLE POUR-ON (IVERMECTIN) SOLUTION [HUVEPHARMA]", "Sep 30, 2026", doctype=None),
        hit("human", "IVERMECTIN TABLET [EPIC PHARMA LLC]", "Jul 24, 2026"),
        hit("stromectol", "STROMECTOL (IVERMECTIN) TABLET [MERCK]", "Apr 24, 2025"),
        hit("untyped", "IVERMECTIN TABLET [ZYDUS]", "Mar 02, 2026", doctype=None),
    ]
    ranked = assert_order_independent(hits, "ivermectin")
    assert "vet-typed" not in setids(ranked)
    assert "vet-title" not in setids(ranked)
    assert setids(ranked) == ["human", "untyped", "stromectol"]


def test_all_hits_veterinary_ranks_to_nothing():
    hits = [hit("v", "IVERMECTIN PASTE [VETR LLC]", "Oct 05, 2026", doctype=ANIMAL)]
    assert rank_label_candidates(hits, "ivermectin") == []


def test_albuterol_single_ingredient_before_combination():
    hits = [
        hit("combo", "IPRATROPIUM BROMIDE AND ALBUTEROL SULFATE SOLUTION [X]", "Oct 12, 2026"),
        hit("combo-paren", "COMBIVENT (IPRATROPIUM BROMIDE, ALBUTEROL SULFATE) AEROSOL [Y]", "Oct 11, 2026"),
        hit("single", "ALBUTEROL SULFATE TABLET [Z]", "Jan 02, 2024"),
    ]
    ranked = assert_order_independent(hits, "albuterol sulfate")
    assert setids(ranked)[0] == "single"
    assert label_features(hits[0], "albuterol")["combination"] is True
    assert label_features(hits[1], "albuterol")["combination"] is True
    assert label_features(hits[2], "albuterol")["combination"] is False


def test_combination_drug_prefers_combination_label():
    hits = [
        hit("single", "ABACAVIR SULFATE TABLET [X]", "Oct 12, 2026"),
        hit("combo", "ABACAVIR SULFATE AND LAMIVUDINE TABLET, FILM COATED [Y]", "Jan 02, 2024"),
    ]
    ranked = assert_order_independent(hits, "abacavir sulfate; lamivudine")
    assert setids(ranked)[0] == "combo"


def test_flurbiprofen_oral_before_ophthalmic():
    hits = [
        hit("drops", "FLURBIPROFEN SODIUM SOLUTION/ DROPS [AMICI PHARMA]", "May 07, 2026"),
        hit("tablet", "FLURBIPROFEN TABLET [GENUS LIFESCIENCES]", "Feb 11, 2026"),
    ]
    ranked = assert_order_independent(hits, "flurbiprofen")
    assert setids(ranked) == ["tablet", "drops"]
    assert label_features(hits[0], "flurbiprofen")["route_tier"] == ROUTE_LOCAL
    assert label_features(hits[1], "flurbiprofen")["route_tier"] == ROUTE_SYSTEMIC


def test_prescription_before_otc_and_topical():
    hits = [
        hit("otc-patch", "SALONPAS LIDOCAINE 4% (LIDOCAINE) PATCH [HISAMITSU]", "Oct 16, 2026", doctype=OTC),
        hit("rx-cream", "LIDOCAINE AND PRILOCAINE CREAM [X]", "Oct 06, 2026"),
        hit("rx-inj", "LIDOCAINE HYDROCHLORIDE INJECTION, SOLUTION [SANDOZ]", "Jan 13, 2020"),
        hit("rx-epi", "LIDOCAINE HYDROCHLORIDE AND EPINEPHRINE INJECTION, SOLUTION [HOSPIRA]", "Sep 29, 2026"),
    ]
    ranked = assert_order_independent(hits, "lidocaine")
    assert setids(ranked) == ["rx-inj", "otc-patch", "rx-epi", "rx-cream"]


def test_most_recent_wins_among_equals_by_real_date_not_string():
    # The old code sorted the raw "Mon DD, YYYY" string, so "Sep" beat "Jun 2024".
    hits = [
        hit("old", "WIDGETOL TABLET [A]", "Sep 1, 2019"),
        hit("new", "WIDGETOL TABLET [B]", "Jun 12, 2024"),
    ]
    assert setids(rank_label_candidates(hits, "widgetol")) == ["new", "old"]
    assert published_date(hits[1]) == date(2024, 6, 12)
    assert published_date({"published_date": "garbage"}) == date.min


def test_ties_break_deterministically_on_version_then_setid():
    hits = [
        hit("b", "WIDGETOL TABLET [A]", "Jun 12, 2024", version=2),
        hit("a", "WIDGETOL TABLET [B]", "Jun 12, 2024", version=2),
        hit("c", "WIDGETOL TABLET [C]", "Jun 12, 2024", version=5),
    ]
    assert setids(assert_order_independent(hits, "widgetol")) == ["c", "a", "b"]


def test_empty_and_malformed_hits():
    assert rank_label_candidates([], "anything") == []
    assert rank_label_candidates([{"title": "NO SETID TABLET"}, "junk", None], "x") == []


def test_select_labels_prefers_distinct_dosage_forms():
    hits = [
        hit("inj1", "HYDROMORPHONE HYDROCHLORIDE INJECTION, SOLUTION [A]", "Aug 31, 2026"),
        hit("inj2", "HYDROMORPHONE HYDROCHLORIDE INJECTION [B]", "Aug 27, 2026"),
        hit("inj3", "HYDROMORPHONE HYDROCHLORIDE INJECTION, SOLUTION [C]", "Aug 25, 2026"),
        hit("tab", "HYDROMORPHONE HYDROCHLORIDE TABLET [D]", "Jul 06, 2026"),
        hit("soln", "HYDROMORPHONE HYDROCHLORIDE SOLUTION [E]", "May 20, 2026"),
    ]
    ranked = rank_label_candidates(hits, "hydromorphone")
    assert setids(ranked) == ["inj1", "inj2", "inj3", "tab", "soln"]
    assert setids(select_labels(ranked, "hydromorphone", 3)) == ["inj1", "tab", "soln"]
    # Fewer distinct forms than slots: fill in rank order.
    assert setids(select_labels(ranked, "hydromorphone", 5)) == ["inj1", "tab", "soln", "inj2", "inj3"]
    assert select_labels(ranked, "hydromorphone", 0) == []


# --------------------------------------------------------------------------- #
# Cache format compatibility
# --------------------------------------------------------------------------- #

def test_cache_entry_shapes_read():
    assert cached_setids({"setid": "abc"}) == ["abc"]  # the original single-setid shape
    assert cached_setids({"setid": "", "match": "no_results"}) == []
    assert cached_setids({"setid": "top", "alternate_setids": ["alt1", "alt2", "top"]}) == ["top", "alt1", "alt2"]
    assert cached_setids(None) == []
    assert is_ranked_entry({"setid": "abc"}) is False
    assert is_ranked_entry({"setid": "abc", "alternate_setids": []}) is True


def test_has_indications_section():
    assert has_indications_section(b'<section><code code="34067-9" codeSystem="2.16.840.1.113883.6.1"/>')
    assert not has_indications_section(b'<section><code code="34070-3" codeSystem="x"/>')


# --------------------------------------------------------------------------- #
# End to end against a mock transport (no network)
# --------------------------------------------------------------------------- #

def _spl(setid: str, with_indications: bool = True) -> bytes:
    code = '<code code="34067-9"/>' if with_indications else '<code code="34070-3"/>'
    return f'<document xmlns="urn:hl7-org:v3"><setId root="{setid}"/><section>{code}</section></document>'.encode()


def test_acquire_reranks_stale_entries_and_writes_ranked_shape(tmp_path, monkeypatch):
    drug_list = tmp_path / "drug_list.yaml"
    drug_list.write_text(yaml.safe_dump({"drugs": [
        {"identity": {"resolved_label": "Ivermectin"}, "approvals": [{"authority": "FDA"}],
         "source_ingredients": ["ivermectin"]},
        {"identity": {"resolved_label": "Nothingol"}, "approvals": [{"authority": "FDA"}]},
        {"identity": {"resolved_label": "Europol"}, "approvals": [{"authority": "EMA"}]},
    ]}))
    cache_path = tmp_path / "dailymed_acquire.json"
    # A stale entry in the original shape: it must be re-resolved, not trusted.
    cache_path.write_text(json.dumps({"ivermectin": {"setid": "vet-old"}}))
    monkeypatch.setattr(acq, "_cache", acq.EnrichmentCache(cache_path))

    # What the list endpoint returns per doctype query (it attaches no doctype itself).
    listing = {
        RX: [
            {"setid": "tab", "title": "IVERMECTIN TABLET [EPIC]", "published_date": "Jul 24, 2026", "spl_version": 1},
            {"setid": "cream-noind", "title": "IVERMECTIN CREAM [X]", "published_date": "Nov 17, 2025", "spl_version": 1},
            {"setid": "tab2", "title": "IVERMECTIN TABLET [ZYDUS]", "published_date": "Mar 02, 2026", "spl_version": 1},
        ],
        OTC: [
            {"setid": "lotion", "title": "IVERMECTIN LOTION [BELMORA]", "published_date": "Oct 12, 2026", "spl_version": 3},
        ],
    }
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path.endswith("/spls.json"):
            params = dict(request.url.params)
            # Case-insensitively, as DailyMed's own drug_name index is: the query now leads
            # with `curie_label` ("Ivermectin"), not the source-ingredient spelling.
            if params.get("drug_name", "").lower() != "ivermectin":
                return httpx.Response(200, json={"data": []})
            assert params["pagesize"] == "100"
            return httpx.Response(200, json={"data": listing[params["doctype"]]})
        setid = request.url.path.rsplit("/", 1)[-1].removesuffix(".xml")
        return httpx.Response(200, content=_spl(setid, with_indications=setid != "cream-noind"))

    data_dir = tmp_path / "raw"
    counts = acq.acquire(
        drug_list_path=drug_list, data_dir=data_dir, delay=0,
        labels_per_drug=2, transport=httpx.MockTransport(handler),
    )

    entry = json.loads(cache_path.read_text())["ivermectin"]
    assert entry["setid"] == "tab"
    # Diversity put the cream next, but it has no Indications section, so the
    # next distinct human label (the OTC lotion) fills the second slot.
    assert entry["alternate_setids"] == ["lotion"]
    assert cached_setids(entry) == ["tab", "lotion"]
    assert entry["queried_name"] == "Ivermectin"
    assert [lab["title"] for lab in entry["labels"]] == ["IVERMECTIN TABLET [EPIC]", "IVERMECTIN LOTION [BELMORA]"]
    assert (data_dir / "tab.xml").exists() and (data_dir / "lotion.xml").exists()
    assert counts["stale_entry"] == 1 and counts["no_indications"] == 1 and counts["labels"] == 2
    assert counts["requested"] == 2 and counts["no_spl"] == 1  # Nothingol: no hits; Europol: not FDA
    assert json.loads(cache_path.read_text())["nothingol"]["match"] == "no_results"
    # Both human doctypes were queried for the stale drug; the old setid was not downloaded.
    assert sum("doctype=34391-3" in u for u in requests) == 2
    assert not (data_dir / "vet-old.xml").exists()

    # Second run: the ranked entry is reused without re-querying.
    requests.clear()
    counts2 = acq.acquire(
        drug_list_path=drug_list, data_dir=data_dir, delay=0,
        labels_per_drug=2, transport=httpx.MockTransport(handler),
    )
    assert counts2["cached_hit"] == 1 and counts2["already_on_disk"] == 2
    assert not any("spls.json" in u and "drug_name=ivermectin" in u for u in requests)


def test_acquire_keeps_labels_from_every_name_not_just_the_first_productive_one(
        tmp_path, monkeypatch):
    """Pooling, not fall-through.

    A drug's indications are split across its *spellings* as well as its manufacturers:
    paracetamol and acetaminophen labels are different documents stating different things.
    Stopping at the first name that returns anything keeps whichever subset that spelling
    carries, so every candidate is queried and the hits pooled. Here the generic name finds
    the tablet and the salt spelling finds a capsule that nothing else would have reached.
    """
    drug_list = tmp_path / "drug_list.yaml"
    drug_list.write_text(yaml.safe_dump({"drugs": [
        {"identity": {"resolved_label": "Widgetol"}, "approvals": [{"authority": "FDA"}],
         "source_ingredients": ["widgetol sodium", "nothingatall"]},
    ]}))
    monkeypatch.setattr(acq, "_cache", acq.EnrichmentCache(tmp_path / "c.json"))
    per_name = {
        "widgetol": [{"setid": "tab", "title": "WIDGETOL TABLET [A]",
                      "published_date": "Jan 01, 2026", "spl_version": 1}],
        "widgetol sodium": [{"setid": "cap", "title": "WIDGETOL SODIUM CAPSULE [B]",
                             "published_date": "Jan 01, 2026", "spl_version": 1}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/spls.json"):
            name = dict(request.url.params)["drug_name"].lower()
            return httpx.Response(200, json={"data": per_name.get(name, [])})
        setid = request.url.path.rsplit("/", 1)[-1].removesuffix(".xml")
        return httpx.Response(200, content=_spl(setid))

    acq.acquire(drug_list_path=drug_list, data_dir=tmp_path / "raw", delay=0,
                transport=httpx.MockTransport(handler))

    # Keyed on the generic spelling, which is the stable name across runs.
    entry = acq._cache.get("widgetol")
    assert entry["queried_name"] == "Widgetol"
    assert cached_setids(entry) == ["tab", "cap"], entry
    assert (tmp_path / "raw" / "cap.xml").exists(), "a later spelling's label must be kept"


def test_a_name_that_returns_nothing_does_not_stop_the_later_ones(tmp_path, monkeypatch):
    """The fall-through case, which pooling subsumes: the generic spelling misses and a
    source-ingredient spelling hits. 1,074 of 2,591 product-style names returned no SPL at
    all, so this is the common direction, not an edge case."""
    drug_list = tmp_path / "drug_list.yaml"
    drug_list.write_text(yaml.safe_dump({"drugs": [
        {"identity": {"resolved_label": "Nothingol"}, "approvals": [{"authority": "FDA"}],
         "source_ingredients": ["widgetol sodium"]},
    ]}))
    monkeypatch.setattr(acq, "_cache", acq.EnrichmentCache(tmp_path / "c.json"))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/spls.json"):
            if dict(request.url.params)["drug_name"].lower() != "widgetol sodium":
                return httpx.Response(200, json={"data": []})
            return httpx.Response(200, json={"data": [
                {"setid": "w", "title": "WIDGETOL SODIUM TABLET [A]",
                 "published_date": "Jan 01, 2026", "spl_version": 1}]})
        return httpx.Response(200, content=_spl("w"))

    acq.acquire(drug_list_path=drug_list, data_dir=tmp_path / "raw", delay=0,
                transport=httpx.MockTransport(handler))
    entry = acq._cache.get("nothingol")
    assert entry["setid"] == "w" and entry["queried_name"] == "widgetol sodium"

"""Search URL tool: scan read-only, then ad final URLs /cook/ -> /cooks/ in place, counted only
after a read-back from Google; campaign names untouched; own Sheet, own quota count."""
import importlib
import inspect
import re
import tempfile
import time
import types

import pytest
from google.ads.googleads.client import GoogleAdsClient

from surl import config, db
from surl.ads import quota_kind
from surl.main import Worker

NS = types.SimpleNamespace
CID = "5550001111"


class SearchAccount:
    def __init__(self, n=5, extra=()):
        self.ads = {}
        for i in range(n):
            aga = f"customers/{CID}/adGroupAds/{100 + i}~{900 + i}"
            self.ads[aga] = {"camp": f"Dish {i}", "cid": i, "ag": 100 + i, "ad": 900 + i,
                             "urls": [f"https://ponly.com/cook/dish-{i}/"], "mob": []}
        for aga, d in extra:
            self.ads[aga] = d
        self.mutations = 0
        self.ignore = False
        self.customers = set()
        self.raise_on_search = None

    def search_stream(self, customer_id=None, query=None, **kw):
        if self.raise_on_search:
            raise self.raise_on_search
        self.customers.add(customer_id)
        frm = re.search(r"FROM (\w+)", query).group(1)
        rows = []
        if frm == "ad_group_ad":
            inlist = set(re.findall(r"'([^']*)'", query)) if " IN (" in query else None
            for aga, d in self.ads.items():
                if inlist is not None and aga not in inlist:
                    continue
                rows.append(NS(campaign=NS(id=d["cid"], name=d["camp"]), ad_group=NS(id=d["ag"]),
                               ad_group_ad=NS(resource_name=aga, ad=NS(
                                   id=d["ad"], type_=NS(name="RESPONSIVE_SEARCH_AD"),
                                   final_urls=list(d["urls"]), final_mobile_urls=list(d["mob"])))))
        elif frm == "ad_group_criterion":
            rows = [NS(ad_group_criterion=NS(final_urls=["https://ponly.com/cook/kw/"],
                                             final_mobile_urls=[]))]
        elif frm == "asset":
            rows = [NS(asset=NS(type_=NS(name="SITELINK"), final_urls=["https://ponly.com/"],
                                final_mobile_urls=[]))]
        elif frm == "campaign":
            rows = [NS(campaign=NS(tracking_url_template="", final_url_suffix="")) for _ in self.ads]
        elif frm == "ad_group":
            rows = [NS(ad_group=NS(tracking_url_template="", final_url_suffix="")) for _ in self.ads]
        return [NS(results=rows)]

    def mutate_ads(self, request):
        assert request["customer_id"] == CID and request["partial_failure"] is True
        self.mutations += 1
        for op in request["operations"]:
            ad_id = int(op.update.resource_name.rsplit("/", 1)[1])
            d = next(v for v in self.ads.values() if v["ad"] == ad_id)
            paths = set(op.update_mask.paths)
            assert "final_urls" in paths and paths <= {"final_urls", "final_mobile_urls"}
            if not self.ignore:
                d["urls"] = list(op.update.final_urls)
                if "final_mobile_urls" in paths:
                    d["mob"] = list(op.update.final_mobile_urls)
        return NS(results=[], partial_failure_error=None)


class Strict:
    """Checks every call against the real v26 client's signature."""
    def __init__(self, g, service):
        mod = importlib.import_module(f"google.ads.googleads.{config.API_VERSION}.services."
                                      "services." + re.sub(r"(?<!^)(?=[A-Z])", "_",
                                                           service).lower())
        self.real, self.g = getattr(mod, service + "Client"), g

    def __getattr__(self, name):
        sig = inspect.signature(getattr(self.real, name))

        def call(*a, **k):
            sig.bind(None, *a, **k)
            return getattr(self.g, name)(*a, **k)
        return call


class FakeSheet:
    def __init__(self, settings):
        self.settings, self.tabs = settings, {}

    def read_settings(self):
        return dict(self.settings)

    def write(self, tab, rows):
        self.tabs[tab] = rows


@pytest.fixture
def tmp():
    with tempfile.TemporaryDirectory() as d:
        yield d


def worker(tmp, g, settings):
    client = GoogleAdsClient(credentials=None, developer_token="x", use_proto_plus=True,
                             version=config.API_VERSION)
    client.get_service = lambda name, **k: Strict(g, name)
    sheet = FakeSheet({"Pause": False, "Daily API quota cap": 14000, **settings})
    w = Worker(db.connect(f"{tmp}/t.sqlite"), sheet, client)
    w.logs = []
    w.log = w.logs.append
    return w


def test_scan_is_read_only_and_counts(tmp):
    g = SearchAccount()
    w = worker(tmp, g, {"Account": "555-000-1111", "Run scan": True})
    w.tick()
    res = db.kv_get(w.con, "scan")
    assert res["ads"] == 5 and res["ads_to_update"] == 5 and g.mutations == 0
    assert res["campaigns_with_not_exactly_1_ad"] == 0
    assert res["keywords_with_old_url_not_changed"] == 1
    assert g.customers == {CID}
    assert "Scan" in w.sheet.tabs


def test_nothing_without_a_valid_account(tmp):
    g = SearchAccount()
    w = worker(tmp, g, {"Account": "12345", "Run scan": True, "Run update": True})
    w.tick()
    assert g.customers == set() and g.mutations == 0


def test_update_changes_only_the_url_and_confirms_by_read_back(tmp):
    g = SearchAccount()
    w = worker(tmp, g, {"Account": CID, "Run scan": "TRUE", "Run update": "TRUE"})
    names = [d["camp"] for d in g.ads.values()]
    w.tick()
    w.tick()
    assert all(d["urls"] == [f"https://ponly.com/cooks/dish-{d['cid']}/"] for d in g.ads.values())
    assert [d["camp"] for d in g.ads.values()] == names
    assert db.kv_get(w.con, "update").get("done")
    rep = dict((r[0], r[1]) for r in w.sheet.tabs["Update"] if len(r) == 2)
    assert rep["Ads updated (read back from Google Ads)"] == 5
    n = g.mutations
    w.tick()
    assert g.mutations == n                        # finished: nothing more sent


def test_google_ignoring_the_change_is_never_counted(tmp):
    g = SearchAccount()
    g.ignore = True
    w = worker(tmp, g, {"Account": CID, "Run scan": True, "Run update": True})
    for _ in range(5):
        w.tick()
    rows = w.con.execute("SELECT status, attempts FROM search_ads").fetchall()
    assert all(r["status"] == "todo" and r["attempts"] == 3 for r in rows)
    assert g.mutations == 3


def test_old_path_in_another_form_is_reported_never_changed(tmp):
    aga = f"customers/{CID}/adGroupAds/7~77"
    g = SearchAccount(n=1, extra=[(aga, {"camp": "Odd", "cid": 9, "ag": 7, "ad": 77,
                                         "urls": ["http://www.ponly.com/cook/odd/"], "mob": []})])
    w = worker(tmp, g, {"Account": CID, "Run scan": True, "Run update": True})
    w.tick()
    res = db.kv_get(w.con, "scan")
    assert res["ads_to_update"] == 1
    assert res["ads_with_old_path_in_another_form_not_changed"] == 1
    assert g.ads[aga]["urls"] == ["http://www.ponly.com/cook/odd/"]


def test_mobile_url_changed_only_when_it_has_the_old_path(tmp):
    aga = f"customers/{CID}/adGroupAds/8~88"
    g = SearchAccount(n=0, extra=[(aga, {"camp": "M", "cid": 1, "ag": 8, "ad": 88,
                                         "urls": ["https://ponly.com/cook/m/"],
                                         "mob": ["https://ponly.com/cook/m/"]})])
    w = worker(tmp, g, {"Account": CID, "Run scan": True, "Run update": True})
    w.tick()
    assert g.ads[aga]["mob"] == ["https://ponly.com/cooks/m/"]


def test_update_needs_the_scan_first(tmp):
    g = SearchAccount()
    w = worker(tmp, g, {"Account": CID, "Run update": True})
    w.tick()
    assert g.mutations == 0 and not db.kv_get(w.con, "update")


def test_update_is_sized_to_the_quota_left(tmp):
    g = SearchAccount(n=40)
    w = worker(tmp, g, {"Account": CID, "Run scan": True, "Run update": True,
                        "Daily API quota cap": 385})   # 385 - 300 reserve - 5 scan - 60 = 20
    w.tick()
    done = w.con.execute("SELECT COUNT(*) FROM search_ads WHERE status='done'").fetchone()[0]
    assert done == 20


def test_pause_sends_nothing(tmp):
    g = SearchAccount()
    w = worker(tmp, g, {"Account": CID, "Run scan": True, "Pause": True})
    w.tick()
    assert g.customers == set()


def test_daily_quota_error_waits_an_hour(tmp):
    from google.api_core.exceptions import ResourceExhausted
    g = SearchAccount()
    g.raise_on_search = ResourceExhausted("quota")
    w = worker(tmp, g, {"Account": CID, "Run scan": True})
    w.tick()
    assert db.kv_get(w.con, "quota_wait_until") > time.time() + 3000
    assert any("daily quota" in x for x in w.logs)
    g.raise_on_search = None
    w.tick()
    assert g.customers == set()                    # still waiting


def test_quota_kind():
    from google.api_core.exceptions import ResourceExhausted
    assert quota_kind(ResourceExhausted("x")) == "daily"
    assert quota_kind(ResourceExhausted("RESOURCE_TEMPORARILY_EXHAUSTED")) == "temporary"
    assert quota_kind(ValueError("x")) is None


def test_rewrite_url():
    assert config.rewrite_url("https://ponly.com/cook/a-b/") == "https://ponly.com/cooks/a-b/"
    assert config.rewrite_url("https://ponly.com/cooks/a/") == "https://ponly.com/cooks/a/"


def test_whole_request_refused_counts_as_a_try_and_stops_after_3(tmp):
    g = SearchAccount()
    calls = []

    def refuse(request):
        calls.append(1)
        raise RuntimeError("INVALID_ARGUMENT")
    g.mutate_ads = refuse
    w = worker(tmp, g, {"Account": CID, "Run scan": True, "Run update": True})
    for _ in range(6):
        w.tick()
    assert len(calls) == 3
    assert all(r["attempts"] == 3 for r in w.con.execute("SELECT attempts FROM search_ads"))

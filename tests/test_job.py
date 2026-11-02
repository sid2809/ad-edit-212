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
    def __init__(self, n=5, extra=(), assets=()):
        self.ads = {}
        for i in range(n):
            aga = f"customers/{CID}/adGroupAds/{100 + i}~{900 + i}"
            self.ads[aga] = {"camp": f"Dish {i}", "cid": i, "ag": 100 + i, "ad": 900 + i,
                             "urls": [f"https://ponly.com/cook/dish-{i}/"], "mob": []}
        for aga, d in extra:
            self.ads[aga] = d
        self.assets = {a["res"]: a for a in assets}
        self.mutations = 0
        self.asset_mutations = 0
        self.ignore = False
        self.customers = set()
        self.raise_on_search = None

    @staticmethod
    def _ad(d):
        return NS(id=d["ad"], type_=NS(name=d.get("type", "RESPONSIVE_SEARCH_AD")),
                  final_urls=list(d["urls"]), final_mobile_urls=list(d["mob"]),
                  tracking_url_template=d.get("tpl", ""), final_url_suffix="")

    def search_stream(self, customer_id=None, query=None, **kw):
        if self.raise_on_search:
            raise self.raise_on_search
        self.customers.add(customer_id)
        frm = re.search(r"FROM (\w+)", query).group(1)
        inlist = set(re.findall(r"'([^']*)'", query)) if " IN (" in query else None
        rows = []
        if frm == "ad_group_ad":
            for aga, d in self.ads.items():
                if inlist is not None and aga not in inlist:
                    continue
                rows.append(NS(campaign=NS(id=d["cid"], name=d["camp"],
                                           advertising_channel_type=NS(
                                               name=d.get("channel", "SEARCH"))),
                               ad_group=NS(id=d["ag"]),
                               ad_group_ad=NS(resource_name=aga, ad=self._ad(d))))
        elif frm == "ad_group_criterion":
            rows = [NS(ad_group_criterion=NS(final_urls=["https://ponly.com/cook/kw/"],
                                             final_mobile_urls=[], tracking_url_template="",
                                             final_url_suffix=""))]
        elif frm == "asset":
            for r, a in self.assets.items():
                if inlist is not None and r not in inlist:
                    continue
                rows.append(NS(asset=NS(resource_name=r, type_=NS(name="SITELINK"), name="",
                                        final_urls=list(a["urls"]), final_mobile_urls=[],
                                        tracking_url_template="", final_url_suffix="")))
        elif frm == "campaign":
            seen = {}
            for d in self.ads.values():
                seen[d["cid"]] = d
            rows = [NS(campaign=NS(id=c, name=d["camp"], advertising_channel_type=NS(
                name=d.get("channel", "SEARCH")), tracking_url_template="", final_url_suffix=""))
                for c, d in seen.items()]
        elif frm == "ad_group":
            rows = [NS(ad_group=NS(tracking_url_template="", final_url_suffix="")) for _ in self.ads]
        elif frm == "customer":
            rows = [NS(customer=NS(tracking_url_template="", final_url_suffix=""))]
        return [NS(results=rows)]

    def mutate_ads(self, request):
        assert request["customer_id"] == CID and request["partial_failure"] is True
        self.mutations += 1
        for op in request["operations"]:
            ad_id = int(op.update.resource_name.rsplit("/", 1)[1])
            d = next(v for v in self.ads.values() if v["ad"] == ad_id)
            paths = set(op.update_mask.paths)
            assert paths and paths <= {"final_urls", "final_mobile_urls",
                                       "tracking_url_template", "final_url_suffix"}
            if not self.ignore:
                if "final_urls" in paths:
                    d["urls"] = list(op.update.final_urls)
                if "final_mobile_urls" in paths:
                    d["mob"] = list(op.update.final_mobile_urls)
                if "tracking_url_template" in paths:
                    d["tpl"] = op.update.tracking_url_template
        return NS(results=[], partial_failure_error=None)

    def mutate_assets(self, request):
        assert request["customer_id"] == CID and request["partial_failure"] is True
        self.asset_mutations += 1
        for op in request["operations"]:
            assert list(op.update_mask.paths) == ["final_urls"]
            if not self.ignore:
                self.assets[op.update.resource_name]["urls"] = list(op.update.final_urls)
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
    res = db.kv_get(w.con, "scan_v2")
    assert res["ads"] == 5 and res["ads_to_update"] == 5 and g.mutations == 0
    assert res["campaigns_with_more_than_1_ad"] == []
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
    assert db.kv_get(w.con, "update_v2").get("done")
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
    rows = w.con.execute("SELECT status, attempts FROM items").fetchall()
    assert all(r["status"] == "todo" and r["attempts"] == 3 for r in rows)
    assert g.mutations == 3


def test_every_form_and_every_ad_type_and_sitelinks_are_changed(tmp):
    ads = [(f"customers/{CID}/adGroupAds/7~77", {"camp": "FNAF TRIVIA Responsive", "cid": 9,
                                                  "ag": 7, "ad": 77, "channel": "DISPLAY",
                                                  "type": "RESPONSIVE_DISPLAY_AD",
                                                  "urls": ["http://ponly.com/cook/odd"],
                                                  "mob": [],
                                                  "tpl": "{lpurl}?x=ponly.com/cook/y"}),
           (f"customers/{CID}/adGroupAds/6~66", {"camp": "Already new", "cid": 8, "ag": 6,
                                                  "ad": 66, "urls": ["https://ponly.com/cooks/a/"],
                                                  "mob": []})]
    assets = [{"res": f"customers/{CID}/assets/{i}", "urls": [f"https://www.ponly.com/cook/s{i}/"]}
              for i in range(3)] + [{"res": f"customers/{CID}/assets/9",
                                     "urls": ["https://ponly.com/"]}]
    g = SearchAccount(n=1, extra=ads, assets=assets)
    w = worker(tmp, g, {"Account": CID, "Run scan": True, "Run update": True})
    for _ in range(4):
        w.tick()
    res = db.kv_get(w.con, "scan_v2")
    assert res["ads_to_update"] == 2 and res["assets_to_update"] == 3
    assert res["ads_to_update_by_campaign_type"] == {"SEARCH": 1, "DISPLAY": 1}
    odd = g.ads[f"customers/{CID}/adGroupAds/7~77"]
    assert odd["urls"] == ["http://ponly.com/cooks/odd"]
    assert odd["tpl"] == "{lpurl}?x=ponly.com/cooks/y"
    assert g.ads[f"customers/{CID}/adGroupAds/6~66"]["urls"] == ["https://ponly.com/cooks/a/"]
    assert [g.assets[f"customers/{CID}/assets/{i}"]["urls"] for i in range(3)] == [
        [f"https://www.ponly.com/cooks/s{i}/"] for i in range(3)]
    assert g.assets[f"customers/{CID}/assets/9"]["urls"] == ["https://ponly.com/"]
    assert db.kv_get(w.con, "update_v2").get("done")
    assert [d["camp"] for d in g.ads.values()][0] == "Dish 0"


def test_campaigns_with_more_than_one_ad_are_listed(tmp):
    extra = [(f"customers/{CID}/adGroupAds/100~950", {"camp": "Dish 0", "cid": 0, "ag": 100,
                                                       "ad": 950, "urls": ["https://x.com/"],
                                                       "mob": []})]
    g = SearchAccount(n=2, extra=extra)
    w = worker(tmp, g, {"Account": CID, "Run scan": True})
    w.tick()
    assert db.kv_get(w.con, "scan_v2")["campaigns_with_more_than_1_ad"] == [["Dish 0", 2]]


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
    assert g.mutations == 0 and not db.kv_get(w.con, "update_v2")


def test_update_is_sized_to_the_quota_left(tmp):
    g = SearchAccount(n=40)
    w = worker(tmp, g, {"Account": CID, "Run scan": True, "Run update": True,
                        "Daily API quota cap": 386})   # 386 - 300 reserve - 6 scan - 60 = 20
    w.tick()
    done = w.con.execute("SELECT COUNT(*) FROM items WHERE status='done'").fetchone()[0]
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
    r = config.rewrite_url
    assert r("https://ponly.com/cook/a-b/") == "https://ponly.com/cooks/a-b/"
    assert r("https://ponly.com/cooks/a/") == "https://ponly.com/cooks/a/"
    assert r("http://www.ponly.com/cook/a?b=1") == "http://www.ponly.com/cooks/a?b=1"
    assert r("https://ponly.com/cook") == "https://ponly.com/cooks"
    assert r("https://ponly.com/cookies/a") == "https://ponly.com/cookies/a"
    assert r("https://notponly.com/cook/a").endswith("/cooks/a")   # contains ponly.com/cook
    assert not config.has_old("https://ponly.com/cooks/a/")


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
    assert all(r["attempts"] == 3 for r in w.con.execute("SELECT attempts FROM items"))

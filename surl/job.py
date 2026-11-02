"""Search campaigns: ad final URLs ponly.com/cook/<slug> -> ponly.com/cooks/<slug>
(Siddharth, 2 Nov 2026). Nothing else changes; campaign names stay.

1. Scan (read-only, once, when 'Run scan' is TRUE): every ad, keyword, asset and tracking
   template whose URL contains ponly.com/cook/, written to the 'Scan' tab. Ads with the old
   path in another form (e.g. http://www.) are listed and never changed.
2. Update (when 'Run update' is TRUE, after the scan): each ad's final URL (and final mobile
   URL if it has the old path) is changed in place (AdService; the update mask names only
   those fields), then the ad is read back from Google: only a read-back showing the new URL
   counts. Up to 3 tries per ad; sized to this tool's daily cap; continues on later loops.
   Keywords / assets / templates are reported by the scan but not changed."""
import json
import re
import time

from . import config, db
from .ads import QuotaExhausted, partial_failures, quota_kind
from .log import short


def account(settings):
    v = re.sub(r"\D", "", str((settings or {}).get("Account", "")))
    return v if len(v) == 10 else None


def _q(s):
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _chunks(seq, n=200):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _has_old(urls):
    return any(config.OLD_PATH in (u or "") for u in urls)


def _search(w, cid, query):
    db.log_ops(w.con, 1, "search")
    rows = []
    try:
        for batch in w.client.get_service("GoogleAdsService").search_stream(
                customer_id=cid, query=query):
            rows.extend(batch.results)
    except Exception as e:
        if quota_kind(e):
            raise QuotaExhausted(str(e)[:300]) from e
        raise
    return rows


def scan(w, cid):
    con = w.con
    ads = _search(w, cid, "SELECT campaign.id, campaign.name, ad_group.id, "
                          "ad_group_ad.resource_name, ad_group_ad.ad.id, ad_group_ad.ad.type, "
                          "ad_group_ad.ad.final_urls, ad_group_ad.ad.final_mobile_urls FROM "
                          "ad_group_ad WHERE ad_group_ad.status != 'REMOVED' AND "
                          "campaign.status != 'REMOVED'")
    kws = _search(w, cid, "SELECT ad_group_criterion.final_urls, "
                          "ad_group_criterion.final_mobile_urls FROM ad_group_criterion WHERE "
                          "ad_group_criterion.status != 'REMOVED' AND "
                          "ad_group_criterion.type = 'KEYWORD'")
    assets = _search(w, cid, "SELECT asset.type, asset.final_urls, asset.final_mobile_urls "
                             "FROM asset")
    camps = _search(w, cid, "SELECT campaign.tracking_url_template, campaign.final_url_suffix "
                            "FROM campaign WHERE campaign.status != 'REMOVED'")
    groups = _search(w, cid, "SELECT ad_group.tracking_url_template, ad_group.final_url_suffix "
                             "FROM ad_group WHERE ad_group.status != 'REMOVED'")
    per_camp, per_ag, types, multi = {}, {}, {}, 0
    queue, odd = [], []
    for x in ads:
        aga = x.ad_group_ad
        per_camp[x.campaign.id] = per_camp.get(x.campaign.id, 0) + 1
        per_ag[x.ad_group.id] = per_ag.get(x.ad_group.id, 0) + 1
        t = aga.ad.type_.name
        types[t] = types.get(t, 0) + 1
        urls, mob = list(aga.ad.final_urls), list(aga.ad.final_mobile_urls)
        if len(urls) > 1:
            multi += 1
        if not (_has_old(urls) or _has_old(mob)):
            continue
        if any(config.rewrite_url(u) == u for u in urls + mob if config.OLD_PATH in (u or "")):
            odd.append([x.campaign.name, urls + mob])
            continue
        queue.append((aga.resource_name, f"customers/{cid}/ads/{aga.ad.id}", x.campaign.name,
                      urls, mob))
    kw_old = sum(1 for x in kws if _has_old(x.ad_group_criterion.final_urls) or
                 _has_old(x.ad_group_criterion.final_mobile_urls))
    asset_old = {}
    for x in assets:
        if _has_old(x.asset.final_urls) or _has_old(x.asset.final_mobile_urls):
            asset_old[x.asset.type_.name] = asset_old.get(x.asset.type_.name, 0) + 1
    tpl_old = sum(1 for x in camps if config.OLD_PATH in (x.campaign.tracking_url_template or "")
                  + (x.campaign.final_url_suffix or ""))
    tpl_old += sum(1 for x in groups if config.OLD_PATH in
                   (x.ad_group.tracking_url_template or "") + (x.ad_group.final_url_suffix or ""))
    with db.tx(con):
        con.execute("DELETE FROM search_ads WHERE status!='done'")
        for aga, ad, name, urls, mob in queue:
            con.execute("INSERT OR IGNORE INTO search_ads(ad_group_ad, ad, campaign, urls, mobile)"
                        " VALUES(?,?,?,?,?)", (aga, ad, name, json.dumps(urls), json.dumps(mob)))
        res = {"time": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
               "campaigns": len(camps), "ad_groups": len(groups), "ads": len(ads),
               "ad_types": types,
               "campaigns_with_not_exactly_1_ad": sum(1 for v in per_camp.values() if v != 1),
               "ad_groups_with_not_exactly_1_ad": sum(1 for v in per_ag.values() if v != 1),
               "ads_with_more_than_1_final_url": multi,
               "ads_to_update": len(queue),
               "ads_with_old_path_in_another_form_not_changed": len(odd),
               "keywords_with_old_url_not_changed": kw_old,
               "assets_with_old_url_not_changed": asset_old,
               "tracking_templates_or_suffixes_with_old_url_not_changed": tpl_old,
               "sample_ads_to_update": [[n, u] for _, _, n, u, _ in queue[:5]],
               "sample_other_form": odd[:5]}
        db.kv_set(con, "scan", res)
    w.write("Scan", [["Item", "Value"]] + [[k, v if isinstance(v, (int, str)) else json.dumps(v)]
                                           for k, v in res.items()])
    w.log(f"scan (read-only): {json.dumps(res)[:1500]}")
    return res


def update(w, cid):
    con = w.con
    st = db.kv_get(con, "update", {})
    if st.get("done"):
        return
    if not st.get("started"):
        st = {"started": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())}
        db.kv_set(con, "update", st)
        w.log("update started")
    todo = [dict(r) for r in con.execute(
        "SELECT * FROM search_ads WHERE status='todo' AND attempts<3 ORDER BY ad_group_ad")]
    if not todo:
        st["done"] = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
        db.kv_set(con, "update", st)
        report(w)
        w.log("update finished")
        return
    left = w.quota_cap() - db.ops_last_24h(con) - config.QUOTA_RESERVE
    part = todo[:max(0, min(left - 60, 1000))]           # 1 op per ad + read-backs
    if not part:
        report(w)
        return
    ops = []
    for r in part:
        urls, mob = json.loads(r["urls"]), json.loads(r["mobile"])
        op = w.client.get_type("AdOperation")
        op.update.resource_name = r["ad"]
        op.update.final_urls.extend([config.rewrite_url(u) for u in urls])
        op.update_mask.paths.append("final_urls")
        if _has_old(mob):
            op.update.final_mobile_urls.extend([config.rewrite_url(u) for u in mob])
            op.update_mask.paths.append("final_mobile_urls")
        ops.append(op)
    db.log_ops(con, len(ops), "update")
    try:
        resp = w.client.get_service("AdService").mutate_ads(request={
            "customer_id": cid, "operations": ops, "partial_failure": True})
    except Exception as e:
        if quota_kind(e):
            raise
        with db.tx(con):                          # whole request refused: counts as a try
            for r in part:
                con.execute("UPDATE search_ads SET attempts=attempts+1, error=? WHERE "
                            "ad_group_ad=?", (short(e, 500), r["ad_group_ad"]))
        report(w)
        raise
    failed = partial_failures(w.client, resp)
    now = {}
    for chunk in _chunks([r["ad_group_ad"] for r in part]):
        q = ",".join(_q(x) for x in chunk)
        for x in _search(w, cid, "SELECT ad_group_ad.resource_name, ad_group_ad.ad.final_urls, "
                                 "ad_group_ad.ad.final_mobile_urls FROM ad_group_ad WHERE "
                                 f"ad_group_ad.resource_name IN ({q})"):
            now[x.ad_group_ad.resource_name] = (list(x.ad_group_ad.ad.final_urls),
                                                list(x.ad_group_ad.ad.final_mobile_urls))
    ok = bad = 0
    fails = []
    with db.tx(con):
        for j, r in enumerate(part):
            urls, mob = json.loads(r["urls"]), json.loads(r["mobile"])
            want_u = [config.rewrite_url(u) for u in urls]
            want_m = [config.rewrite_url(u) for u in mob] if _has_old(mob) else None
            got = now.get(r["ad_group_ad"])
            if got is not None and got[0] == want_u and (want_m is None or got[1] == want_m):
                ok += 1
                con.execute("UPDATE search_ads SET status='done', error=NULL WHERE ad_group_ad=?",
                            (r["ad_group_ad"],))
            else:
                bad += 1
                err = failed.get(j) or failed.get(None) or f"Google Ads still shows {got}"
                con.execute("UPDATE search_ads SET attempts=attempts+1, error=? WHERE "
                            "ad_group_ad=?", (str(err)[:500], r["ad_group_ad"]))
                fails.append(f"{short(r['campaign'], 80)}: {short(err, 200)}")
    for f in fails[:20]:
        w.log(f"update FAILED: {f}")
    w.log(f"update: {ok} ads now on the new URL (read back from Google Ads), {bad} not; "
          f"{len(todo) - len(part)} left for later loops")
    report(w)


def report(w):
    con = w.con
    c = {r[0]: r[1] for r in con.execute("SELECT status, COUNT(*) FROM search_ads GROUP BY status")}
    failed = con.execute("SELECT COUNT(*) FROM search_ads WHERE status='todo' AND attempts>=3"
                         ).fetchone()[0]
    st = db.kv_get(con, "update", {})
    rows = [["Item", "Value"], ["Started", st.get("started", "")],
            ["Finished", st.get("done", "not yet")],
            ["Ads updated (read back from Google Ads)", c.get("done", 0)],
            ["Ads still to do", c.get("todo", 0) - failed],
            ["Ads failed 3 times", failed],
            ["Operations sent in the last 24 h (this tool)", db.ops_last_24h(con)],
            ["", ""], ["Failed ad (campaign)", "Google's message"]]
    rows += [[r["campaign"], r["error"]] for r in con.execute(
        "SELECT campaign, error FROM search_ads WHERE status='todo' AND attempts>=3 LIMIT 500")]
    w.write("Update", rows)


def run(w):
    cid = account(w.settings)
    if not cid:
        return
    if w.s_bool("Run scan") and not db.kv_get(w.con, "scan"):
        scan(w, cid)
    if w.s_bool("Run update") and db.kv_get(w.con, "scan"):
        update(w, cid)

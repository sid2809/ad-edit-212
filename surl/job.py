"""ponly.com/cook -> ponly.com/cooks wherever it appears (Siddharth, 2 Nov 2026:
"Change wherever the ponly.com/cook comes to ponly.com/cooks"). Campaign names are not changed
(his earlier instruction); the scan counts them.

1. Scan (read-only, once, when 'Run scan' is TRUE): every ad (all types) and every asset (e.g.
   sitelinks) whose final URL, final mobile URL, tracking template or final URL suffix contains
   ponly.com/cook (any scheme, www, letter case). Also counts campaigns / ad groups / keywords /
   the account itself with it in their URL fields or templates, and campaign names containing it.
   Result in the 'Scan' tab.
2. Update (when 'Run update' is TRUE, after the scan): each queued ad (AdService) and asset
   (AssetService) is changed in place; the update mask names only the fields that had the old
   path. Then it is read back from Google: only a read-back showing the new values counts. Up to
   3 tries each; sized to this tool's daily cap; continues on later loops."""
import json
import re
import time

from . import config, db
from .ads import QuotaExhausted, partial_failures, quota_kind
from .log import short

SCAN_KEY, UPDATE_KEY = "scan_v2", "update_v2"
# (proto field, GAQL field suffix); lists are repeated fields
AD_FIELDS = (("final_urls", True), ("final_mobile_urls", True),
             ("tracking_url_template", False), ("final_url_suffix", False))


def account(settings):
    v = re.sub(r"\D", "", str((settings or {}).get("Account", "")))
    return v if len(v) == 10 else None


def _q(s):
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _chunks(seq, n=200):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _search(w, cid, query):
    db.log_ops(w.con, 1, "search")
    rows = []
    try:
        # keep the service object alive while the stream is read; if it is garbage-collected
        # mid-stream, gRPC cancels the call ("Stream removed (Channel deallocated!)")
        svc = w.client.get_service("GoogleAdsService")
        stream = svc.search_stream(customer_id=cid, query=query)
        for batch in stream:
            rows.extend(batch.results)
        del stream, svc
    except Exception as e:
        if quota_kind(e):
            raise QuotaExhausted(str(e)[:300]) from e
        raise
    return rows


def _values(obj):
    """{field: current value} for the URL fields of an ad or asset."""
    out = {}
    for f, rep in AD_FIELDS:
        v = getattr(obj, f, None)
        out[f] = list(v or []) if rep else (v or "")
    return out


def _changes(vals):
    """{field: new value} only for fields that contain the old path."""
    out = {}
    for f, rep in AD_FIELDS:
        v = vals.get(f)
        if rep and any(config.has_old(u) for u in v or []):
            out[f] = [config.rewrite_url(u) for u in v]
        elif not rep and config.has_old(v):
            out[f] = config.rewrite_url(v)
    return out


def _sel(prefix):
    return ", ".join(f"{prefix}.{f}" for f, _ in AD_FIELDS)


def _count(d, k):
    d[k] = d.get(k, 0) + 1


def scan(w, cid):
    con = w.con
    ads = _search(w, cid, "SELECT campaign.id, campaign.name, campaign.advertising_channel_type, "
                          "ad_group.id, ad_group_ad.resource_name, ad_group_ad.ad.id, "
                          f"ad_group_ad.ad.type, {_sel('ad_group_ad.ad')} FROM ad_group_ad "
                          "WHERE ad_group_ad.status != 'REMOVED' AND campaign.status != 'REMOVED'")
    assets = _search(w, cid, f"SELECT asset.resource_name, asset.type, asset.name, "
                             f"{_sel('asset')} FROM asset")
    kws = _search(w, cid, "SELECT ad_group_criterion.final_urls, "
                          "ad_group_criterion.final_mobile_urls, "
                          "ad_group_criterion.tracking_url_template, "
                          "ad_group_criterion.final_url_suffix FROM ad_group_criterion WHERE "
                          "ad_group_criterion.status != 'REMOVED' AND "
                          "ad_group_criterion.type = 'KEYWORD'")
    camps = _search(w, cid, "SELECT campaign.id, campaign.name, campaign.advertising_channel_type, "
                            "campaign.tracking_url_template, campaign.final_url_suffix "
                            "FROM campaign WHERE campaign.status != 'REMOVED'")
    groups = _search(w, cid, "SELECT ad_group.tracking_url_template, ad_group.final_url_suffix "
                             "FROM ad_group WHERE ad_group.status != 'REMOVED'")
    cust = _search(w, cid, "SELECT customer.tracking_url_template, customer.final_url_suffix "
                           "FROM customer")

    per_camp, names, types, q_types, q_channels = {}, {}, {}, {}, {}
    queue = []
    for x in ads:
        aga = x.ad_group_ad
        names[x.campaign.id] = x.campaign.name
        per_camp[x.campaign.id] = per_camp.get(x.campaign.id, 0) + 1
        _count(types, aga.ad.type_.name)
        vals = _values(aga.ad)
        ch = _changes(vals)
        if not ch:
            continue
        _count(q_types, aga.ad.type_.name)
        _count(q_channels, x.campaign.advertising_channel_type.name)
        queue.append((f"customers/{cid}/ads/{aga.ad.id}", "ad", aga.resource_name,
                      x.campaign.name, {f: [vals[f], ch[f]] for f in ch}))
    a_types = {}
    for x in assets:
        vals = _values(x.asset)
        ch = _changes(vals)
        if not ch:
            continue
        _count(a_types, x.asset.type_.name)
        queue.append((x.asset.resource_name, "asset", x.asset.resource_name,
                      f"{x.asset.type_.name} {x.asset.name}".strip(),
                      {f: [vals[f], ch[f]] for f in ch}))
    camp_channels = {}
    for x in camps:
        _count(camp_channels, x.campaign.advertising_channel_type.name)
        names.setdefault(x.campaign.id, x.campaign.name)

    def tpl_hits(rows, attr):
        n = 0
        for r in rows:
            o = getattr(r, attr)
            if any(config.has_old(getattr(o, f, "") or "") for f in
                   ("tracking_url_template", "final_url_suffix")):
                n += 1
        return n
    kw_old = sum(1 for x in kws if _changes(_values(x.ad_group_criterion)))
    not_one = sorted(((names.get(c, str(c)), n) for c, n in per_camp.items() if n != 1),
                     key=lambda t: -t[1])
    no_ads = [x.campaign.name for x in camps if x.campaign.id not in per_camp]
    with db.tx(con):
        con.execute("DELETE FROM items WHERE status!='done'")
        for res, kind, check, label, fields in queue:
            con.execute("INSERT OR IGNORE INTO items(res, kind, check_res, label, fields) "
                        "VALUES(?,?,?,?,?)", (res, kind, check, label, json.dumps(fields)))
        res = {"time": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
               "campaigns": len(camps), "campaigns_by_type": camp_channels,
               "ad_groups": len(groups), "ads": len(ads), "ad_types": types,
               "ads_to_update": sum(1 for q in queue if q[1] == "ad"),
               "ads_to_update_by_ad_type": q_types,
               "ads_to_update_by_campaign_type": q_channels,
               "assets_to_update": sum(1 for q in queue if q[1] == "asset"),
               "assets_to_update_by_type": a_types,
               "campaigns_with_more_than_1_ad": [[n, c] for n, c in not_one][:50],
               "campaigns_with_no_ad": len(no_ads),
               "campaign_names_with_old_path_not_changed": sum(
                   1 for x in camps if config.has_old(x.campaign.name)),
               "keywords_with_old_url_not_changed": kw_old,
               "campaign_templates_with_old_url_not_changed": tpl_hits(camps, "campaign"),
               "ad_group_templates_with_old_url_not_changed": tpl_hits(groups, "ad_group"),
               "account_template_with_old_url_not_changed": tpl_hits(cust, "customer"),
               "sample_changes": [[q[3], q[4]] for q in queue[:3]] +
                                 [[q[3], q[4]] for q in queue if q[1] == "asset"][:3]}
        db.kv_set(con, SCAN_KEY, res)
    w.write("Scan", [["Item", "Value"]] + [[k, v if isinstance(v, (int, str)) else json.dumps(v)]
                                           for k, v in res.items()])
    w.log(f"scan (read-only): {json.dumps(res)[:3000]}")
    return res


def _op(w, kind, res, fields):
    op = w.client.get_type("AdOperation" if kind == "ad" else "AssetOperation")
    op.update.resource_name = res
    for f, (_, new) in fields.items():
        if isinstance(new, list):
            getattr(op.update, f).extend(new)
        else:
            setattr(op.update, f, new)
        op.update_mask.paths.append(f)
    return op


def _read_back(w, cid, kind, checks):
    out = {}
    for chunk in _chunks(checks):
        q = ",".join(_q(x) for x in chunk)
        if kind == "ad":
            rows = _search(w, cid, f"SELECT ad_group_ad.resource_name, {_sel('ad_group_ad.ad')} "
                                   f"FROM ad_group_ad WHERE ad_group_ad.resource_name IN ({q})")
            for x in rows:
                out[x.ad_group_ad.resource_name] = _values(x.ad_group_ad.ad)
        else:
            rows = _search(w, cid, f"SELECT asset.resource_name, {_sel('asset')} FROM asset "
                                   f"WHERE asset.resource_name IN ({q})")
            for x in rows:
                out[x.asset.resource_name] = _values(x.asset)
    return out


def _update_kind(w, cid, kind, budget):
    con = w.con
    part = [dict(r) for r in con.execute(
        "SELECT * FROM items WHERE kind=? AND status='todo' AND attempts<3 ORDER BY res LIMIT ?",
        (kind, max(0, min(budget, 1000))))]
    if not part:
        return 0
    ops = [_op(w, kind, r["res"], json.loads(r["fields"])) for r in part]
    db.log_ops(con, len(ops), "update")
    try:
        if kind == "ad":
            svc = w.client.get_service("AdService")
            resp = svc.mutate_ads(request={"customer_id": cid, "operations": ops,
                                           "partial_failure": True})
        else:
            svc = w.client.get_service("AssetService")
            resp = svc.mutate_assets(request={"customer_id": cid, "operations": ops,
                                              "partial_failure": True})
    except Exception as e:
        if quota_kind(e):
            raise
        with db.tx(con):                          # whole request refused: counts as a try
            for r in part:
                con.execute("UPDATE items SET attempts=attempts+1, error=? WHERE res=?",
                            (short(e, 500), r["res"]))
        raise
    failed = partial_failures(w.client, resp)
    try:
        now = _read_back(w, cid, kind, [r["check_res"] for r in part])
    except Exception as e:
        if not quota_kind(e):                     # read-back failed: counts as a try, so the
            with db.tx(con):                      # same batch is not re-sent forever
                for r in part:
                    con.execute("UPDATE items SET attempts=attempts+1, error=? WHERE res=?",
                                ("read-back failed: " + short(e, 480), r["res"]))
        raise
    ok = bad = 0
    fails = []
    with db.tx(con):
        for j, r in enumerate(part):
            fields = json.loads(r["fields"])
            got = now.get(r["check_res"])
            if got is not None and all(got.get(f) == new for f, (_, new) in fields.items()):
                ok += 1
                con.execute("UPDATE items SET status='done', error=NULL WHERE res=?", (r["res"],))
            else:
                bad += 1
                err = failed.get(j) or failed.get(None) or (
                    "not found when read back" if got is None else
                    f"Google Ads still shows {json.dumps({f: got.get(f) for f in fields})}")
                con.execute("UPDATE items SET attempts=attempts+1, error=? WHERE res=?",
                            (str(err)[:500], r["res"]))
                fails.append(f"{short(r['label'], 80)}: {short(err, 200)}")
    for f in fails[:20]:
        w.log(f"update FAILED ({kind}): {f}")
    w.log(f"update ({kind}s): {ok} now on the new URL (read back from Google Ads), {bad} not")
    return len(part)


def update(w, cid):
    con = w.con
    st = db.kv_get(con, UPDATE_KEY, {})
    if st.get("done"):
        return
    if not st.get("started"):
        st = {"started": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())}
        db.kv_set(con, UPDATE_KEY, st)
        w.log("update started")
    left = con.execute("SELECT COUNT(*) FROM items WHERE status='todo' AND attempts<3"
                       ).fetchone()[0]
    if not left:
        st["done"] = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
        db.kv_set(con, UPDATE_KEY, st)
        report(w)
        w.log("update finished")
        return
    try:
        for kind in ("ad", "asset"):
            budget = w.quota_cap() - db.ops_last_24h(con) - config.QUOTA_RESERVE - 60
            if budget <= 0:
                break
            if _update_kind(w, cid, kind, budget):
                break                              # one batch per loop
    finally:
        report(w)


def report(w):
    con = w.con
    st = db.kv_get(con, UPDATE_KEY, {})
    rows = [["Item", "Value"], ["Started", st.get("started", "")],
            ["Finished", st.get("done", "not yet")]]
    for kind in ("ad", "asset"):
        c = {r[0]: r[1] for r in con.execute(
            "SELECT status, COUNT(*) FROM items WHERE kind=? GROUP BY status", (kind,))}
        failed = con.execute("SELECT COUNT(*) FROM items WHERE kind=? AND status='todo' AND "
                             "attempts>=3", (kind,)).fetchone()[0]
        rows += [[f"{kind.title()}s updated (read back from Google Ads)", c.get("done", 0)],
                 [f"{kind.title()}s still to do", c.get("todo", 0) - failed],
                 [f"{kind.title()}s failed 3 times", failed]]
    rows += [["Operations sent in the last 24 h (this tool)", db.ops_last_24h(con)],
             ["", ""], ["Failed (campaign / asset)", "Google's message"]]
    rows += [[r["label"], r["error"]] for r in con.execute(
        "SELECT label, error FROM items WHERE status='todo' AND attempts>=3 LIMIT 500")]
    w.write("Update", rows)


MAX_SCAN_FAILURES = 3


def run(w):
    cid = account(w.settings)
    if not cid:
        return
    con = w.con
    if not w.s_bool("Run scan"):
        db.kv_set(con, "scan_failures", 0)        # untick + tick again = 3 more tries
    elif not db.kv_get(con, SCAN_KEY):
        n = db.kv_get(con, "scan_failures", 0)
        if n >= MAX_SCAN_FAILURES:
            return
        try:
            scan(w, cid)
        except Exception as e:
            if not quota_kind(e):
                db.kv_set(con, "scan_failures", n + 1)
                if n + 1 >= MAX_SCAN_FAILURES:
                    w.write("Scan", [["Item", "Value"],
                                     ["Scan failed 3 times; stopped", short(e, 400)],
                                     ["To retry", "Set Run scan to FALSE, then TRUE"]])
                    w.log("scan failed 3 times; stopped until Run scan is set FALSE then TRUE")
            raise
    if w.s_bool("Run update") and db.kv_get(w.con, SCAN_KEY):
        update(w, cid)

"""State on the Railway volume: which ads are done, the scan result, operations sent."""
import json
import os
import sqlite3
import time
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS quota_log (ts REAL, ops INTEGER, kind TEXT);
CREATE TABLE IF NOT EXISTS search_ads (ad_group_ad TEXT PRIMARY KEY, ad TEXT, campaign TEXT,
  urls TEXT, mobile TEXT, status TEXT DEFAULT 'todo', attempts INTEGER DEFAULT 0, error TEXT);
CREATE TABLE IF NOT EXISTS items (res TEXT PRIMARY KEY, kind TEXT, check_res TEXT, label TEXT,
  fields TEXT, status TEXT DEFAULT 'todo', attempts INTEGER DEFAULT 0, error TEXT);
"""


def connect(path=None):
    path = path or config.DB_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


@contextmanager
def tx(con):
    con.execute("BEGIN IMMEDIATE")
    try:
        yield
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


def kv_get(con, k, default=None):
    r = con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return json.loads(r["v"]) if r else default


def kv_set(con, k, v):
    con.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, json.dumps(v)))


def log_ops(con, n, kind):
    if n:
        con.execute("INSERT INTO quota_log VALUES(?,?,?)", (time.time(), int(n), kind))


def ops_last_24h(con):
    return int(con.execute("SELECT COALESCE(SUM(ops),0) FROM quota_log WHERE ts>?",
                           (time.time() - 86400,)).fetchone()[0])

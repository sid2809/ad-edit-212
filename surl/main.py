"""Worker loop (Railway). Every minute: read Settings, run the scan / update step, report."""
import os
import signal
import threading
import time
import traceback

from . import config, db, job
from .ads import make_client, quota_kind
from .log import log, short


class Worker:
    def __init__(self, con, sheet, client):
        self.con, self.sheet, self.client = con, sheet, client
        self.settings = {}
        self.stop = threading.Event()
        self.log = log

    def s_bool(self, key):
        v = (self.settings or {}).get(key, False)
        return v is True or str(v).strip().upper() == "TRUE"

    def quota_cap(self):
        try:
            return int(str(self.settings.get("Daily API quota cap",
                                             config.DEFAULT_QUOTA_CAP)).replace(",", ""))
        except ValueError:
            return config.DEFAULT_QUOTA_CAP

    def write(self, tab, rows):
        try:
            self.sheet.write(tab, rows)
        except Exception as e:
            self.log(f"{tab}: report not written: {short(e, 200)}")

    def tick(self):
        try:
            self.settings = self.sheet.read_settings()
        except Exception as e:
            self.log(f"Settings could not be read (nothing sent): {short(e, 200)}")
            return
        if self.s_bool("Pause"):
            return
        if time.time() < db.kv_get(self.con, "quota_wait_until", 0):
            return
        try:
            job.run(self)
        except Exception as e:
            kind = quota_kind(e)
            if kind:
                wait = 300 if kind == "temporary" else 3600
                db.kv_set(self.con, "quota_wait_until", time.time() + wait)
                self.log(f"Google Ads {'rate limit' if kind == 'temporary' else 'daily quota'} hit; "
                    f"waiting {wait // 60} min: {short(e, 300)}")
            else:
                self.log("step failed (retried next loop):\n" + traceback.format_exc())


def missing_vars():
    miss = [v for v in config.REQUIRED_VARS if not os.environ.get(v)]
    if os.environ.get("GOOGLE_ADS_USE_SERVICE_ACCOUNT") != "1":
        miss += [v for v in ("GOOGLE_ADS_CLIENT_ID", "GOOGLE_ADS_CLIENT_SECRET",
                             "GOOGLE_ADS_REFRESH_TOKEN") if not os.environ.get(v)]
    return miss


def volume_ok():
    d = os.path.realpath(config.DATA_DIR)
    return os.path.ismount(d) or os.path.realpath(
        os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "/nonexistent")) == d


def main():
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    signal.signal(signal.SIGINT, lambda *a: stop.set())
    while missing_vars() and not stop.is_set():
        log("Waiting for Railway variables: " + ", ".join(missing_vars()))
        stop.wait(300)
    while not volume_ok() and not stop.is_set():
        log(f"{config.DATA_DIR} is not a mounted volume; refusing to start (state would be "
            f"lost). Waiting.")
        stop.wait(300)
    from .sheet import Sheet
    sheet = Sheet()
    sheet.ensure_settings()
    w = Worker(db.connect(), sheet, make_client())
    w.stop = stop
    log("search URL tool started")
    while not stop.is_set():
        try:
            w.tick()
        except Exception:
            log("loop error:\n" + traceback.format_exc())
        stop.wait(config.LOOP_SLEEP_S)
    log("stopped cleanly")


if __name__ == "__main__":
    main()

"""The tool's own Google Sheet (not the display pipeline's): Settings + report tabs."""
import json
import time

from . import config

SETTINGS = [
    ["Setting", "Value", "Notes"],
    ["Account", "", "Search campaigns account (10 digits, e.g. 123-456-7890)."],
    ["Run scan", False, "TRUE: read-only scan (runs once). Results in the 'Scan' tab."],
    ["Run update", False, "TRUE after checking the scan: ad final URLs /cook/ -> /cooks/. "
                          "Progress in the 'Update' tab."],
    ["Daily API quota cap", config.DEFAULT_QUOTA_CAP,
     "Operations per 24 h this tool may use. The 15,000 Basic limit is shared with every tool "
     "on the same Cloud project (the display pipeline too)."],
    ["Pause", False, "TRUE = nothing new is sent to Google Ads."],
]


class Sheet:
    def __init__(self):
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        creds = service_account.Credentials.from_service_account_info(
            json.loads(config.SA_JSON), scopes=["https://www.googleapis.com/auth/spreadsheets"])
        self.svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self.sid = config.SHEET_ID

    def _call(self, req):
        for attempt in range(5):
            try:
                return req.execute()
            except Exception as e:
                code = getattr(getattr(e, "resp", None), "status", None)
                if code in (429, 500, 502, 503, 504) and attempt < 4:
                    time.sleep(2 ** attempt * 5)
                    continue
                raise

    def ensure_tab(self, title):
        meta = self._call(self.svc.spreadsheets().get(spreadsheetId=self.sid))
        if title not in {s["properties"]["title"] for s in meta["sheets"]}:
            self._call(self.svc.spreadsheets().batchUpdate(spreadsheetId=self.sid, body={
                "requests": [{"addSheet": {"properties": {"title": title}}}]}))

    def read(self, tab):
        r = self._call(self.svc.spreadsheets().values().get(
            spreadsheetId=self.sid, range=f"'{tab}'", valueRenderOption="UNFORMATTED_VALUE"))
        return r.get("values", [])

    def ensure_settings(self):
        self.ensure_tab("Settings")
        have = {str(r[0]).strip() for r in self.read("Settings") if r}
        missing = [r for r in SETTINGS if r[0] not in have]
        if missing:
            self._call(self.svc.spreadsheets().values().append(
                spreadsheetId=self.sid, range="'Settings'!A1", valueInputOption="RAW",
                insertDataOption="INSERT_ROWS", body={"values": missing}))

    def read_settings(self):
        out = {}
        for row in self.read("Settings")[1:]:
            if row:
                out[str(row[0]).strip()] = row[1] if len(row) > 1 else ""
        return out

    def write(self, tab, rows):
        self.ensure_tab(tab)
        self._call(self.svc.spreadsheets().values().update(
            spreadsheetId=self.sid, range=f"'{tab}'!A1", valueInputOption="RAW",
            body={"values": rows}))
        try:
            self._call(self.svc.spreadsheets().values().clear(
                spreadsheetId=self.sid, range=f"'{tab}'!A{len(rows) + 1}:ZZ"))
        except Exception as e:
            if "exceeds grid limits" not in str(e):
                raise

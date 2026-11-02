# search-urls

Changes `ponly.com/cook` to `ponly.com/cooks` wherever it appears in a Google Ads account
(any scheme, `www`, letter case; the slug and the rest of the URL stay the same):
final URLs, final mobile URLs, tracking templates and final URL suffixes of **every ad (all
types)** and **every asset** (e.g. sitelinks). Campaign names are not changed. It runs on its own
Railway service with its own Google Sheet, separate from the display pipeline.

## How it works
1. **Scan** (read-only, runs once when `Run scan` is TRUE): counts what has the old path, by ad
   type, campaign type and asset type; lists campaigns with more than 1 ad; counts keywords,
   campaign / ad group / account templates and campaign names with it. Results go to the **Scan**
   tab.
2. **Update** (when `Run update` is TRUE, only after the scan): changes each ad and asset in place
   (the update mask names only the fields that had the old path), then reads it back from Google
   Ads. It counts as done only when the read-back shows the new values. Up to 3 tries each.
   Progress and failures go to the **Update** tab.

Each loop sends at most what fits under `Daily API quota cap` (minus a reserve of 300), counted by
this tool only. The 15,000/day Basic access limit is shared by every tool on the same Cloud project,
so set the cap with the display pipeline's own cap in mind. On a quota error it waits (5 min for a
short-term rate limit, 1 hour for the daily limit).

## Setup
1. New Google Sheet; share it (Editor) with the same service account email the display pipeline
   uses.
2. Railway: new service from this repo; add a **volume mounted at `/data`** (the tool refuses to
   start without it).
3. Variables (copy from the display service; only `SHEET_ID` differs = the new Sheet's ID):
   `SHEET_ID`, `GOOGLE_SERVICE_ACCOUNT_JSON`, `GOOGLE_ADS_DEVELOPER_TOKEN`,
   `GOOGLE_ADS_LOGIN_CUSTOMER_ID`, and either `GOOGLE_ADS_CLIENT_ID` / `GOOGLE_ADS_CLIENT_SECRET` /
   `GOOGLE_ADS_REFRESH_TOKEN` or `GOOGLE_ADS_USE_SERVICE_ACCOUNT=1`.
4. On start it creates the **Settings** tab. Fill `Account`, tick `Run scan`, read the **Scan**
   tab, then tick `Run update`. `Pause` = TRUE stops sending.

Account IDs go only in the Sheet, never in this repo.

## Tests
`pip install -r requirements-dev.txt && python -m pytest -q`

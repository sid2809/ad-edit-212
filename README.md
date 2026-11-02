# search-urls

Changes the final URL of every ad in a Google Ads **Search** account from
`https://ponly.com/cook/<slug>` to `https://ponly.com/cooks/<slug>`. Nothing else changes
(campaign names, keywords, bids, status are untouched). It runs on its own Railway service with its
own Google Sheet, separate from the display pipeline.

## How it works
1. **Scan** (read-only, runs once when `Run scan` is TRUE): counts campaigns, ad groups, ads and
   ads with the old URL, and lists old URLs found in keywords, assets and tracking templates (these
   are reported only, never changed). Results go to the **Scan** tab.
2. **Update** (when `Run update` is TRUE, only after the scan): changes each ad's final URL in place
   (and its final mobile URL if that has the old path), then reads the ad back from Google Ads.
   An ad counts as done only when the read-back shows the new URL. Up to 3 tries per ad.
   Progress and failures go to the **Update** tab.
   Ads whose URL has the old path in another form (e.g. `http://www.`) are reported, not changed.

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

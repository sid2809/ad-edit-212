"""Settings. Secrets and IDs come only from environment variables (Railway Variables) and the
tool's own Google Sheet. The repository is public: no account ID is ever written in the code."""
import os
import re

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "search_urls.sqlite")

SHEET_ID = os.environ.get("SHEET_ID", "")
SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
LOGIN_CUSTOMER_ID = os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "").replace("-", "")
DEVELOPER_TOKEN = os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN", "")
OAUTH_CLIENT_ID = os.environ.get("GOOGLE_ADS_CLIENT_ID", "")
OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_ADS_CLIENT_SECRET", "")
OAUTH_REFRESH_TOKEN = os.environ.get("GOOGLE_ADS_REFRESH_TOKEN", "")
ADS_USE_SERVICE_ACCOUNT = os.environ.get("GOOGLE_ADS_USE_SERVICE_ACCOUNT", "0") == "1"
REQUIRED_VARS = ["SHEET_ID", "GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_ADS_DEVELOPER_TOKEN"]

API_VERSION = "v26"
LOOP_SLEEP_S = 60
DEFAULT_QUOTA_CAP = 14000          # Basic access: 15,000 per Cloud project (shared!)
QUOTA_RESERVE = 300

# Siddharth 2 Nov 2026: "Change wherever the ponly.com/cook comes to ponly.com/cooks".
# Any scheme / www / letter case; the slug and everything after it stay the same.
# "ponly.com/cooks" is never matched again (the lookahead stops at a word character or "-").
OLD_RE = re.compile(r"(ponly\.com/cook)(?![\w-])", re.IGNORECASE)


def has_old(url):
    return bool(url) and bool(OLD_RE.search(url))


def rewrite_url(url):
    return OLD_RE.sub(lambda m: m.group(1) + "s", url) if url else url

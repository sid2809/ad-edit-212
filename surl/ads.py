"""Google Ads client and error helpers (same API version and library as the display pipeline)."""
import os
import tempfile

from . import config


class QuotaExhausted(Exception):
    pass


def make_client():
    from google.ads.googleads.client import GoogleAdsClient
    cfg = {"use_proto_plus": True, "developer_token": config.DEVELOPER_TOKEN}
    if config.LOGIN_CUSTOMER_ID:
        cfg["login_customer_id"] = config.LOGIN_CUSTOMER_ID
    if config.ADS_USE_SERVICE_ACCOUNT:
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            f.write(config.SA_JSON)
        os.chmod(path, 0o600)
        cfg["json_key_file_path"] = path
    else:
        cfg.update(client_id=config.OAUTH_CLIENT_ID, client_secret=config.OAUTH_CLIENT_SECRET,
                   refresh_token=config.OAUTH_REFRESH_TOKEN)
    return GoogleAdsClient.load_from_dict(cfg, version=config.API_VERSION)


def _failures(e):
    """GoogleAdsFailure objects from a GoogleAdsException (.failure) or from a raw
    api_core / gRPC error (its details carry the serialized failure)."""
    out = []
    if getattr(e, "failure", None) is not None:
        out.append(e.failure)
    for d in getattr(e, "details", None) or []:
        if str(getattr(d, "type_url", "")).endswith("GoogleAdsFailure"):
            try:
                from google.ads.googleads.v26.errors.types import errors
                out.append(errors.GoogleAdsFailure.deserialize(d.value))
            except Exception:
                pass
    return out


def error_codes(e):
    out = []
    for err in [x for f in _failures(e) for x in (getattr(f, "errors", None) or [])]:
        try:
            f = err.error_code._pb.WhichOneof("error_code")
            out.append(getattr(err.error_code, f).name if f else "")
        except Exception:
            pass
    return out


def quota_kind(e):
    """'daily', 'temporary' (short-term rate limit; same gRPC status) or None."""
    if isinstance(e, QuotaExhausted) and e.__cause__ is not None:
        e = e.__cause__
    try:
        from google.api_core.exceptions import ResourceExhausted
        raw = isinstance(e, ResourceExhausted)
    except ImportError:
        raw = False
    text = str(e) + str(getattr(e, "failure", ""))
    grpc_name = ""
    try:
        grpc_name = e.code().name if callable(getattr(e, "code", None)) else ""
    except Exception:
        pass
    try:
        grpc_name = grpc_name or e.error.code().name
    except Exception:
        pass
    if not (raw or "RESOURCE_EXHAUSTED" in grpc_name + text or "exhausted" in text.lower()):
        return None
    codes = error_codes(e)
    if any(c in ("RESOURCE_TEMPORARILY_EXHAUSTED",
                 "EXCESSIVE_SHORT_TERM_QUERY_RESOURCE_CONSUMPTION") for c in codes) or \
            "TEMPORARILY" in text or "SHORT_TERM" in text:
        return "temporary"
    return "daily"


def partial_failures(client, response):
    """{operation index: message} from a partial_failure_error."""
    out = {}
    pfe = getattr(response, "partial_failure_error", None)
    if not pfe or not getattr(pfe, "code", 0):
        return out
    for d in pfe.details:
        try:
            f = client.get_type("GoogleAdsFailure")
            failure = type(f).deserialize(d.value)
        except Exception:
            continue
        for e in failure.errors:
            idx = None
            for el in e.location.field_path_elements:
                if el.field_name == "operations":
                    idx = el.index
                    break
            out[idx] = (out.get(idx, "") + " " + e.message).strip()
    if not out:
        out[None] = pfe.message
    return out

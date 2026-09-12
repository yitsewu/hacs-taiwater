"""執行於 HA executor 的查詢客戶端；每次查詢獨立 Cookie 工作階段。"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import re
import secrets
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from .detail import DetailClient
from .portal import QueryRejected, SiteSchemaChanged, TaiWaterError, normalize_water_id



def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class QueryError(Exception):
    """只攜帶固定錯誤碼，不包含原始頁面、身分或例外字串。"""

    def __init__(self, code: str, *, ocr_status="not_run", verification_status="not_run"):
        super().__init__(code)
        self.code = code
        self.ocr_status = ocr_status
        self.verification_status = verification_status


class MemoryImage:
    """相容既有表單客戶端的記憶體圖片容器。"""
    parent = None

    def __init__(self):
        self.parent = self
        self.data = b""

    def mkdir(self, **kwargs):
        pass

    def write_bytes(self, value):
        if len(value) > 2_000_000:
            raise SiteSchemaChanged("圖片過大")
        self.data = value


@dataclass
class ManualChallenge:
    details: DetailClient = field(repr=False)
    image: bytes = field(repr=False)
    content_type: str
    nonce: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    created_at: float = field(default_factory=time.monotonic)
    used: bool = False

    @property
    def image_data_url(self):
        return f"data:{self.content_type};base64,{base64.b64encode(self.image).decode()}"


class _SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _official_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _official_url(url):
    target = urllib.parse.urlsplit(url)
    try:
        port = target.port
    except ValueError:
        raise SiteSchemaChanged("原站目的地不符預期") from None
    if target.scheme != "https" or target.hostname != "www.water.gov.tw" or port not in (None, 443) or target.username is not None:
        raise SiteSchemaChanged("原站目的地不符預期")


class _OfficialHTTPSHandler(urllib.request.HTTPSHandler):
    """Only tolerate missing SKI on the official site's legacy certificate chain.

    Python 3.13 enabled strict RFC 5280 checks. Error 86 is missing subject
    key identifier, not an expired, untrusted, or wrong-host certificate.
    The retry keeps CA, signature, validity and hostname verification enabled.
    No application request has been sent when the TLS handshake fails.
    """

    def https_open(self, request):
        _official_url(request.full_url)
        try:
            return super().https_open(request)
        except urllib.error.URLError as error:
            reason = error.reason
            if not isinstance(reason, ssl.SSLCertVerificationError) or getattr(reason, "verify_code", None) != 86:
                raise
            context = ssl.create_default_context()
            context.verify_flags &= ~ssl.VERIFY_X509_STRICT
            return urllib.request.HTTPSHandler(context=context).https_open(request)


class BoundedClient(DetailClient):
    def __init__(self):
        super().__init__(timeout=30)
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar), _SameOriginRedirect(), _OfficialHTTPSHandler()
        )

    def _request(self, url, **kwargs):
        _official_url(url)
        return super()._request(url, **kwargs)


def prepare_manual() -> ManualChallenge:
    client = BoundedClient()
    image = MemoryImage()
    try:
        challenge = client.fetch_challenge(image)
    except SiteSchemaChanged:
        raise QueryError("site_changed") from None
    except TaiWaterError:
        raise QueryError("cannot_connect") from None
    if challenge.captcha_content_type not in {"image/png", "image/gif", "image/jpeg"}:
        raise QueryError("site_changed")
    return ManualChallenge(client, image.data, challenge.captcha_content_type)


def recognize(image: bytes, ocr_url: str = "") -> str:
    """遠端選項僅送驗證碼圖片；水號戶名仍由 HA 直接送官方網站。"""
    if ocr_url:
        parsed = urllib.parse.urlsplit(ocr_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise QueryError("invalid_ocr_url", ocr_status="unavailable")
        request = urllib.request.Request(
            ocr_url.rstrip("/") + "/recognize", data=image,
            headers={"Content-Type": "application/octet-stream"}, method="POST"
        )
        try:
            # 不跟隨 OCR 服務重新導向，避免圖片被轉交第三方。
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *args, **kwargs):
                    return None
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
                result = json.loads(response.read(4096))
                code = result.get("code", "")
        except urllib.error.HTTPError as error:
            if error.code == 422:
                raise QueryError("ocr_failed", ocr_status="failed") from None
            raise QueryError("ocr_unavailable", ocr_status="unavailable") from None
        except Exception:
            raise QueryError("ocr_unavailable", ocr_status="unavailable") from None
    else:
        from .native_ocr import ENGINE, OCRFailed, OCRUnavailable

        try:
            code = ENGINE.recognize(image)
        except OCRUnavailable:
            raise QueryError("ocr_unavailable", ocr_status="unavailable") from None
        except OCRFailed:
            raise QueryError("ocr_failed", ocr_status="failed") from None
    if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9]{4,8}", code):
        raise QueryError("ocr_failed", ocr_status="failed")
    return code


def _month(code):
    return f"{int(code[1:4]) + 1911:04d}-{code[4:]}"


def _bill(detail):
    from .normalize import safe_fields
    return {"month": _month(detail["selected_month"]), "fields": safe_fields(detail["fields"]), "fetched_at": now()}


def validate_month(month):
    """帳期使用西元 YYYY-MM，限原站三位民國年可表示範圍。"""
    if not isinstance(month, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise QueryError("invalid_month")
    year = int(month[:4]) - 1911
    if not 1 <= year <= 999:
        raise QueryError("invalid_month")
    return f"T{year:03d}{month[5:]}"


def submit_manual(challenge, water_id, customer_name, code, *, history_limit=0, known_months=(), refresh_history=False, ocr_status="not_needed", requested_month=None):
    requested = validate_month(requested_month) if requested_month is not None else None
    if challenge.used or time.monotonic() - challenge.created_at > 180:
        raise QueryError("captcha_expired", ocr_status=ocr_status)
    challenge.used = True
    client = challenge.details
    verified = "not_run"
    try:
        client.submit_query(normalize_water_id(water_id), code)
        verified = "accepted"
        detail = client.open_details(customer_name, water_id)
        available = sorted(detail["months"], key=lambda item: item["value"], reverse=True)
        latest = requested or available[0]["value"]
        if latest not in {item["value"] for item in available}:
            raise QueryError("month_unavailable", ocr_status=ocr_status, verification_status=verified)
        if detail["selected_month"] != latest:
            detail = client.select_month(latest)
        if detail["selected_month"] != latest:
            raise SiteSchemaChanged("Unexpected selected month")
        bills = [_bill(detail)]
    except QueryRejected as exc:
        rejected = "rejected" if "驗證碼" in str(exc) else "unknown"
        raise QueryError("invalid_auth" if verified == "accepted" else "upstream_rejected", ocr_status=ocr_status, verification_status=verified if verified == "accepted" else rejected) from None
    except SiteSchemaChanged:
        raise QueryError("site_changed", ocr_status=ocr_status, verification_status=verified) from None
    except TaiWaterError:
        raise QueryError("cannot_connect", ocr_status=ocr_status, verification_status=verified) from None
    except (ValueError, IndexError, KeyError):
        raise QueryError("invalid_response", ocr_status=ocr_status, verification_status=verified) from None
    result = {"bills": bills, "available_months": [_month(i["value"]) for i in available],
              "ocr_status": ocr_status, "verification_status": verified, "error_code": None,
              "status": "success", "finished_at": now()}
    if history_limit is not None and requested is None:
        selected = available if history_limit == 0 else available[:history_limit]
        for item in selected:
            month = _month(item["value"])
            if item["value"] == latest or (month in known_months and not refresh_history):
                continue
            try:
                detail = client.select_month(item["value"])
                bills.append(_bill(detail))
            except Exception:
                result.update(status="partial", error_code="history_incomplete")
                break
            time.sleep(0.5)
    result["finished_at"] = now()
    return result


def query(water_id, customer_name, *, ocr_url="", history_limit=None, known_months=(), refresh_history=False, requested_month=None):
    if requested_month is not None:
        validate_month(requested_month)
    challenge = prepare_manual()
    code = recognize(challenge.image, ocr_url)
    result = submit_manual(challenge, water_id, customer_name, code, history_limit=history_limit,
                         known_months=known_months, refresh_history=refresh_history, ocr_status="success", requested_month=requested_month)
    result["ocr_backend"] = "external" if ocr_url else "builtin"
    return result


def validate_credentials(water_id, customer_name, ocr_url=""):
    return query(water_id, customer_name, ocr_url=ocr_url)

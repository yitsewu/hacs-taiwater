#!/usr/bin/env python3
"""台灣自來水公司表單解析與同工作階段查詢。

上層負責 OCR／人工驗證；不持久保存 Cookie、token、驗證碼或原始頁面。
"""

from __future__ import annotations

import http.cookiejar
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable


ENTRY_URL = "https://www.water.gov.tw/ch/EQuery/WaterFeeQuery?nodeId=753"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
)
SENSITIVE_TITLES = ("水號", "地址", "戶名", "姓名", "電話", "身分", "證號")


class TaiWaterError(RuntimeError):
    """Base error that is safe to display without response or session data."""


class SiteSchemaChanged(TaiWaterError):
    """Raised when the authoritative page no longer matches the known contract."""


class QueryRejected(TaiWaterError):
    """Raised when the site rejects a query or returns no bill rows."""


@dataclass(frozen=True)
class Control:
    tag: str
    name: str = ""
    control_type: str = ""
    value: str = ""
    element_id: str = ""
    placeholder: str = ""
    required_message: str = ""
    aria_label: str = ""
    text: str = ""

    @property
    def semantic_text(self) -> str:
        return " ".join(
            part
            for part in (
                self.name,
                self.element_id,
                self.placeholder,
                self.required_message,
                self.aria_label,
                self.text,
            )
            if part
        )


@dataclass(frozen=True)
class ImageRef:
    src: str
    element_id: str = ""
    alt: str = ""


@dataclass
class FormSnapshot:
    action: str
    method: str
    controls: list[Control] = field(default_factory=list)
    images: list[ImageRef] = field(default_factory=list)

    def hidden_payload(self) -> dict[str, str]:
        return {
            control.name: control.value
            for control in self.controls
            if control.name and control.control_type.lower() == "hidden"
        }


@dataclass(frozen=True)
class LinkRef:
    href: str
    text: str


@dataclass
class DocumentSnapshot:
    forms: list[FormSnapshot] = field(default_factory=list)
    rows: list[dict[str, str]] = field(default_factory=list)
    links: list[LinkRef] = field(default_factory=list)
    visible_text: str = ""
    images: list[ImageRef] = field(default_factory=list)


@dataclass
class QueryChallenge:
    submit_url: str
    referer_url: str
    model_index: str
    captcha_path: Path
    captcha_content_type: str
    captcha_size: int
    payload: dict[str, str] = field(repr=False)


@dataclass
class QueryResult:
    rows: list[dict[str, str]]
    detail_url: str | None = field(default=None, repr=False)

    def safe_rows(self) -> list[dict[str, str]]:
        return [
            {
                key: value
                for key, value in row.items()
                if value and not any(marker in key for marker in SENSITIVE_TITLES)
            }
            for row in self.rows
        ]


def _attrs(items: Iterable[tuple[str, str | None]]) -> dict[str, str]:
    return {key.lower(): value or "" for key, value in items}


def _clean_text(parts: Iterable[str]) -> str:
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


class _TaiWaterHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.document = DocumentSnapshot()
        self._form: FormSnapshot | None = None
        self._button: Control | None = None
        self._button_parts: list[str] = []
        self._link_href: str | None = None
        self._link_parts: list[str] = []
        self._row: dict[str, str] | None = None
        self._cell_title: str | None = None
        self._cell_parts: list[str] = []
        self._visible_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        values = _attrs(attrs)
        if tag == "form":
            self._form = FormSnapshot(
                action=values.get("action", ""),
                method=values.get("method", "get").lower(),
            )
            self.document.forms.append(self._form)
        elif tag == "input" and self._form is not None:
            self._form.controls.append(
                Control(
                    tag="input",
                    name=values.get("name", ""),
                    control_type=values.get("type", "text"),
                    value=values.get("value", ""),
                    element_id=values.get("id", ""),
                    placeholder=values.get("placeholder", ""),
                    required_message=values.get("data-val-required", ""),
                    aria_label=values.get("aria-label", ""),
                )
            )
        elif tag == "button" and self._form is not None:
            self._button = Control(
                tag="button",
                name=values.get("name", ""),
                control_type=values.get("type", "submit"),
                value=values.get("value", ""),
                element_id=values.get("id", ""),
                aria_label=values.get("aria-label", ""),
            )
            self._button_parts = []
        elif tag == "img":
            image = ImageRef(
                src=values.get("src", ""),
                element_id=values.get("id", ""),
                alt=values.get("alt", ""),
            )
            self.document.images.append(image)
            if self._form is not None:
                self._form.images.append(image)
        elif tag == "a":
            self._link_href = values.get("href", "")
            self._link_parts = []
        elif tag == "tr":
            self._row = {}
        elif tag == "td" and self._row is not None and values.get("data-title"):
            self._cell_title = _clean_text([values["data-title"]])
            self._cell_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "form":
            self._form = None
        elif tag == "button" and self._form is not None and self._button is not None:
            self._form.controls.append(
                Control(**{**self._button.__dict__, "text": _clean_text(self._button_parts)})
            )
            self._button = None
            self._button_parts = []
        elif tag == "a" and self._link_href is not None:
            self.document.links.append(LinkRef(self._link_href, _clean_text(self._link_parts)))
            self._link_href = None
            self._link_parts = []
        elif tag == "td" and self._row is not None and self._cell_title is not None:
            self._row[self._cell_title] = _clean_text(self._cell_parts)
            self._cell_title = None
            self._cell_parts = []
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.document.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if data.strip():
            self._visible_parts.append(data)
            if self._button is not None:
                self._button_parts.append(data)
            if self._link_href is not None:
                self._link_parts.append(data)
            if self._cell_title is not None:
                self._cell_parts.append(data)

    def close(self) -> None:
        super().close()
        self.document.visible_text = _clean_text(self._visible_parts)


def parse_document(html: str) -> DocumentSnapshot:
    parser = _TaiWaterHTMLParser()
    parser.feed(html)
    parser.close()
    return parser.document


def normalize_water_id(value: str) -> str:
    normalized = re.sub(r"[-\s]", "", value).upper()
    if not re.fullmatch(r"[A-Z0-9]{11}", normalized):
        raise ValueError("水號必須是 11 碼英數字；可包含顯示用連字號。")
    return normalized


class TaiWaterClient:
    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar)
        )
        self.challenge: QueryChallenge | None = None
        self.last_result: QueryResult | None = None

    def _request(
        self,
        url: str,
        *,
        data: dict[str, str] | None = None,
        referer: str | None = None,
    ) -> tuple[bytes, str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/*;q=0.8",
        }
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["Origin"] = "https://www.water.gov.tw"
        if referer:
            headers["Referer"] = referer
        request = urllib.request.Request(url, data=body, headers=headers)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return (
                    response.read(),
                    response.headers.get_content_type(),
                    response.geturl(),
                )
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            raise TaiWaterError("台水網站連線失敗；未保存回應或工作階段資料。") from exc

    @staticmethod
    def _decode(body: bytes) -> str:
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SiteSchemaChanged("台水頁面不是預期的 UTF-8 內容。") from exc

    def fetch_challenge(self, captcha_path: Path) -> QueryChallenge:
        body, content_type, final_url = self._request(ENTRY_URL)
        if content_type != "text/html":
            raise SiteSchemaChanged("台水入口沒有回傳 HTML 表單。")
        document = parse_document(self._decode(body))
        forms = [
            form
            for form in document.forms
            if "WaterFeeQuerySearch" in form.action and form.method == "post"
        ]
        if len(forms) != 1:
            raise SiteSchemaChanged("找不到唯一的台水水費 POST 表單。")
        form = forms[0]
        payload = form.hidden_payload()
        model_index = payload.get("model.Index", "")
        if not model_index or "__RequestVerificationToken" not in payload:
            raise SiteSchemaChanged("台水表單缺少動態索引或防偽 token。")

        captcha_images = [
            image
            for image in (form.images or document.images)
            if image.src
            and (
                image.element_id.lower() == "verificationcodeimg"
                or "驗證碼" in image.alt
                or "VerificationCode" in image.src
            )
        ]
        if len(captcha_images) != 1:
            raise SiteSchemaChanged("找不到唯一的台水驗證碼圖片。")
        captcha_url = urllib.parse.urljoin(final_url, captcha_images[0].src)
        image_body, image_type, _ = self._request(captcha_url, referer=final_url)
        if not image_type.startswith("image/") or not image_body:
            raise SiteSchemaChanged("台水驗證碼端點沒有回傳圖片。")
        captcha_path.parent.mkdir(parents=True, exist_ok=True)
        captcha_path.write_bytes(image_body)

        self.challenge = QueryChallenge(
            submit_url=urllib.parse.urljoin(final_url, form.action),
            referer_url=final_url,
            model_index=model_index,
            captcha_path=captcha_path,
            captcha_content_type=image_type,
            captcha_size=len(image_body),
            payload=payload,
        )
        return self.challenge

    def submit_query(self, water_id: str, verification_code: str) -> QueryResult:
        if self.challenge is None:
            raise TaiWaterError("必須先取得同一工作階段的驗證碼。")
        normalized = normalize_water_id(water_id)
        code = verification_code.strip()
        if not re.fullmatch(r"[A-Za-z0-9]{4,8}", code):
            raise ValueError("驗證碼格式不正確。")

        index = self.challenge.model_index
        payload = dict(self.challenge.payload)
        payload.update(
            {
                f"model[{index}].SiteNo": normalized[:2],
                f"model[{index}].UserNo": normalized[2:-1],
                f"model[{index}].CheckNo": normalized[-1],
                "VerificationCode": code,
            }
        )
        body, content_type, final_url = self._request(
            self.challenge.submit_url,
            data=payload,
            referer=self.challenge.referer_url,
        )
        if content_type != "text/html":
            raise SiteSchemaChanged("台水查詢沒有回傳 HTML。")
        document = parse_document(self._decode(body))
        rows = [row for row in document.rows if any(row.values())]
        if not rows:
            text = document.visible_text
            if "驗證碼" in text and any(word in text for word in ("錯誤", "不正確", "逾時")):
                raise QueryRejected("驗證碼錯誤或已過期，請重新取得。")
            if any(word in text for word in ("查無", "無資料", "水號錯誤")):
                raise QueryRejected("台水網站查無可用帳單資料。")
            raise QueryRejected("台水網站未回傳可解析的帳單資料；可能是版面已變更。")

        detail_links = [
            urllib.parse.urljoin(final_url, link.href)
            for link in document.links
            if link.href and not link.href.lower().startswith("javascript:") and "檢視詳細" in link.text
        ]
        detail_url = detail_links[0] if len(detail_links) == 1 else None
        self.last_result = QueryResult(rows=rows, detail_url=detail_url)
        return self.last_result

    def submit_name_detail(self, customer_name: str) -> QueryResult:
        if self.last_result is None or not self.last_result.detail_url:
            raise SiteSchemaChanged("結果頁沒有可安全辨識的唯一『檢視詳細』連結。")
        name = customer_name.strip()
        if not name:
            raise ValueError("姓名不可為空白。")

        body, content_type, final_url = self._request(
            self.last_result.detail_url,
            referer=self.challenge.submit_url if self.challenge else ENTRY_URL,
        )
        if content_type != "text/html":
            raise SiteSchemaChanged("詳細資料入口沒有回傳 HTML。")
        document = parse_document(self._decode(body))
        candidates: list[tuple[FormSnapshot, Control]] = []
        for form in document.forms:
            for control in form.controls:
                semantic = control.semantic_text
                name_like = bool(re.search(r"(?:customer|cust|user).*name", control.name, re.I))
                if control.tag == "input" and control.control_type.lower() in ("text", ""):
                    if "姓名" in semantic or name_like:
                        candidates.append((form, control))
        if len(candidates) != 1:
            raise SiteSchemaChanged("無法安全辨識唯一的姓名驗證欄位，未送出姓名。")
        form, name_control = candidates[0]
        payload = form.hidden_payload()
        payload[name_control.name] = name
        submit_url = urllib.parse.urljoin(final_url, form.action or final_url)
        if form.method == "get":
            submit_url = f"{submit_url}?{urllib.parse.urlencode(payload)}"
            detail_body, detail_type, _ = self._request(submit_url, referer=final_url)
        elif form.method == "post":
            detail_body, detail_type, _ = self._request(
                submit_url, data=payload, referer=final_url
            )
        else:
            raise SiteSchemaChanged("姓名驗證表單使用未知方法，未送出姓名。")
        if detail_type != "text/html":
            raise SiteSchemaChanged("姓名驗證沒有回傳 HTML。")
        detail_document = parse_document(self._decode(detail_body))
        detail_rows = [row for row in detail_document.rows if any(row.values())]
        if not detail_rows:
            raise QueryRejected("姓名驗證後沒有可解析的詳細帳單資料。")
        self.last_result = QueryResult(rows=detail_rows)
        return self.last_result


"""將原站欄位映射至固定單位；原始私人欄位不進入儲存區。"""
from datetime import date, timedelta
from decimal import Decimal
import re

from .models import Bill

FEE_LABELS = (
    "水費項目小計", "代徵費用小計", "基本費", "清除處理費", "用水費", "加退清除處理費",
    "加退水費", "補助代徵費用", "保護區優惠水費(含加退)", "水源保育與回饋費(含加退)",
    "國防部或退輔會補助金額", "污水下水道使用費", "補收遲延繳付費", "加退污水下水道使用費",
    "固定補助費", "工程改善費", "電子帳單回饋金", "加退工程改善費", "操作維護費", "加退操作維護費",
    "應繳總金額", "未補助前應繳總金額",
)
DATE_LABELS = ("本期預定扣繳日", "本期繳費起始日", "本期繳費期限", "本期抄表日期", "下期繳費起始日", "下期抄表日")
OTHER_LABELS = ("本期計費用水期間", "用水種別", "水表口徑", "本期指針數", "上期指針數", "副表本期指針數",
                "副表上期指針數", "分攤/副表度數", "本期實用度數", "總表度數", "公共用水分攤度數/戶數",
                "用水度數", "繳費狀態", "繳費情形", "繳費狀況", "碳排放量", "用水碳排放量", "用水碳排量")
SAFE_LABELS = set(FEE_LABELS + DATE_LABELS + OTHER_LABELS)


def number(value, *, allow_negative=False):
    if value is None or str(value).strip() in {"", "-", "--", "無", "尚無資料"}:
        return None
    text = str(value).strip().replace(",", "").replace("NT$", "").replace("$", "").replace("元", "")
    text = re.sub(r"\s*(?:kg\s*CO2e|kgCO₂e|公斤|度|m³|m3)\s*$", "", text, flags=re.I).strip()
    if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text):
        raise ValueError("invalid_number")
    value = Decimal(text)
    if not value.is_finite() or (value < 0 and not allow_negative):
        raise ValueError("invalid_number")
    return value


def roc_date(value):
    if not value or str(value).strip() in {"-", "--", "無"}:
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d{7}", text):
        return date(int(text[:3]) + 1911, int(text[3:5]), int(text[5:7]))
    if re.fullmatch(r"\d{8}", text):
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    match = re.fullmatch(r"(\d{3,4})[年/.-](\d{1,2})[月/.-](\d{1,2})日?", text)
    if not match:
        raise ValueError("invalid_date")
    year, month, day = map(int, match.groups())
    return date(year + 1911 if year < 1000 else year, month, day)


def safe_fields(fields):
    return {key: str(value)[:256] for key, value in fields.items() if key in SAFE_LABELS}


def normalize_bill(raw):
    fields = safe_fields(raw["fields"])
    usage = number(fields.get("用水度數"))
    total = number(fields.get("應繳總金額"))
    if usage is None or total is None:
        raise ValueError("missing_bill_totals")
    normalized = {**fields, "usage_m3": usage, "total_twd": total}
    fees = {}
    for label in FEE_LABELS:
        if label in fields:
            parsed = number(fields[label], allow_negative=True)
            if parsed is not None:
                # 貸項／加退款保留帶正負號的明細，不另加到原站總額。
                fees[label] = parsed
    normalized["fee_breakdown"] = fees
    period = fields.get("本期計費用水期間", "")
    if period:
        pair = re.split(r"\s*[～~至]\s*", period)
        if len(pair) != 2:
            raise ValueError("invalid_period")
        first, last = (roc_date(item) for item in pair)
        if first is None or last is None or last < first or (last - first).days > 400:
            raise ValueError("invalid_period")
        # 原站「計費用水期間」包含起訖日；模型使用 (start,end] 區間。
        normalized["period_start"] = first - timedelta(days=1)
        normalized["period_end"] = last
        normalized["billing_start"] = first.isoformat()
        normalized["billing_end"] = last.isoformat()
    for label in ("用水碳排放量", "用水碳排量", "碳排放量"):
        if fields.get(label):
            normalized["carbon_kg"] = number(fields[label])
            normalized["carbon_source"] = "原站帳單 kgCO2e"
            break
    return Bill.from_fields(raw["month"], normalized, raw["fetched_at"])

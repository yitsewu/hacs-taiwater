"""原站明細的數字、期間與個資邊界。"""
import importlib
from pathlib import Path
import sys
import types
from decimal import Decimal
import unittest

namespace = types.ModuleType("taiwater_normalize_tests")
namespace.__path__ = [str(Path(__file__).resolve().parents[1] / "custom_components" / "taiwater")]
sys.modules[namespace.__name__] = namespace
normalizer = importlib.import_module(namespace.__name__ + ".normalize")


class NormalizeTests(unittest.TestCase):
    def test_real_contract_synthetic_values(self):
        bill = normalizer.normalize_bill({"month": "2026-09", "fetched_at": "2026-09-11T01:00:00+00:00", "fields": {
            "用水度數": "61", "應繳總金額": "NT$1,200元", "本期計費用水期間": "1150701 ～ 1150831",
            "加退水費": "-10元", "基本費": "71.4元", "委託郵局、行庫代繳帳號": "PRIVATE-FIXTURE",
            "用戶名稱": "PRIVATE-FIXTURE", "本期指針數": "1234", "本期繳費期限": "1150915",
        }})
        self.assertEqual(bill.usage_m3, 61)
        self.assertEqual(bill.total_twd, 1200)
        self.assertEqual(bill.period_start.isoformat(), "2026-06-30")
        self.assertEqual((bill.period_end - bill.period_start).days, 62)
        self.assertEqual(bill.fee_breakdown["加退水費"], -10)
        self.assertNotIn("PRIVATE-FIXTURE", bill.to_json())
        self.assertEqual(bill.extra_fields["本期指針數"], "1234")

    def test_roc_calendar_and_dates(self):
        self.assertEqual(normalizer.roc_date("1130229").isoformat(), "2024-02-29")
        with self.assertRaises(ValueError):
            normalizer.roc_date("1150229")

    def test_unknown_is_not_zero_and_ambiguous_numbers_fail(self):
        self.assertIsNone(normalizer.number("--"))
        self.assertEqual(normalizer.number("0元"), Decimal(0))
        for value in ("1/2", "100至200", "NaN", "-1"):
            with self.assertRaises(ValueError):
                normalizer.number(value)

    def test_unknown_missing_total_rejects_bill(self):
        with self.assertRaises(ValueError):
            normalizer.normalize_bill({"month": "2026-09", "fetched_at": "2026-09-11T01:00:00+00:00", "fields": {"用水度數": "10"}})

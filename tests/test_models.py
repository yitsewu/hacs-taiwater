"""models.py 的純標準庫測試，不載入整合套件的 __init__.py。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


MODULE_PATH = Path(__file__).parents[1] / "custom_components" / "taiwater" / "models.py"
SPEC = importlib.util.spec_from_file_location("taiwater_models_for_tests", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
models = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = models
SPEC.loader.exec_module(models)

Bill = models.Bill
allocate_days = models.allocate_days
daily_allocation_list = models.daily_allocation_list
allocate_months = models.allocate_months
next_run = models.next_run


def make_bill(month: str, fetched_at: str = "2026-09-01T00:00:00+00:00", **fields):
    return Bill.from_fields(month, fields, fetched_at)


class BillTests(unittest.TestCase):
    def test_json_contract_preserves_safe_fields_and_filters_private_fields(self):
        bill = make_bill(
            "2026-08",
            usage_m3="12.5",
            total_twd=321,
            carbon_kg="1.25",
            period_start="2026-06-30",
            period_end="2026-08-31",
            fee_breakdown={"基本費": "50", "用水費": Decimal("271")},
            billing_status="paid",
            nested={"rate_code": "A", "account_no": "不得保留"},
            中文巢狀={"費率類別": "一般", "水號": "不得保留"},
            account_no="不得保留",
            service_address="不得保留",
            用戶名稱="不得保留",
            **{"委託郵局、行庫代繳帳號": "不得保留"},
        )

        encoded = json.dumps(bill.to_dict(), ensure_ascii=False)
        decoded = json.loads(encoded)

        self.assertEqual(decoded["usage_m3"], 12.5)
        self.assertEqual(decoded["metadata"]["carbon_source"], "original")
        self.assertEqual(decoded["extra_fields"]["billing_status"], "paid")
        self.assertEqual(decoded["extra_fields"]["nested"], {"rate_code": "A"})
        self.assertEqual(decoded["extra_fields"]["中文巢狀"], {"費率類別": "一般"})
        self.assertNotIn("account_no", decoded["extra_fields"])
        self.assertNotIn("service_address", decoded["extra_fields"])
        self.assertNotIn("用戶名稱", decoded["extra_fields"])
        self.assertNotIn("委託郵局、行庫代繳帳號", decoded["extra_fields"])

    def test_to_dict_from_dict_round_trip_keeps_raw_period_fields(self):
        original = make_bill(
            "2026-08",
            usage_m3="12.5",
            period_start="2026-06-30",
            period_end="2026-08-31",
            fee_breakdown={"基本費": "50"},
            tariff_code="A1",
        )
        serialized = original.to_dict()
        rebuilt = Bill.from_dict(serialized)

        self.assertEqual(serialized["period_start"], "2026-06-30")
        self.assertEqual(serialized["period_end"], "2026-08-31")
        self.assertEqual(rebuilt.to_dict(), serialized)

    def test_invalid_month_date_number_and_partial_period_are_rejected(self):
        invalid_cases = [
            ("2026-13", {"usage_m3": "1"}),
            ("2026-01", {"usage_m3": "NaN"}),
            ("2026-01", {"total_twd": -1}),
            ("2026-01", {"period_start": "2026-01-01"}),
            (
                "2026-01",
                {"period_start": "2026-01-02", "period_end": "2026-01-01"},
            ),
        ]
        for month, fields in invalid_cases:
            with self.subTest(month=month, fields=fields), self.assertRaises(ValueError):
                make_bill(month, **fields)

    def test_fee_breakdown_allows_credits_but_total_remains_nonnegative(self):
        bill = make_bill(
            "2026-01", total_twd=90, fee_breakdown={"用水費": 100, "折抵": -10}
        )
        self.assertEqual(bill.fee_breakdown["折抵"], Decimal("-10"))
        with self.assertRaises(ValueError):
            make_bill("2026-01", total_twd=-1)
        with self.assertRaises(ValueError):
            make_bill("2026-01", total_twd=90, fee_breakdown={"折抵": "-"})

    def test_naive_fetched_at_is_rejected(self):
        with self.assertRaises(ValueError):
            make_bill("2026-01", fetched_at="2026-01-01T00:00:00", usage_m3=1)


class AllocationTests(unittest.TestCase):
    def test_actual_days_conserve_decimal_across_leap_day(self):
        bill = make_bill(
            "2024-03",
            usage_m3="1",
            total_twd="100",
            period_start="2024-01-31",
            period_end="2024-03-01",
        )
        result = allocate_months([bill], mode="days")

        self.assertEqual(list(result), ["2024-02", "2024-03"])
        self.assertEqual(result["2024-02"]["covered_days"], 29)
        self.assertEqual(result["2024-03"]["covered_days"], 1)
        self.assertEqual(sum(item["water"] for item in result.values()), Decimal("1"))
        self.assertEqual(sum(item["cost"] for item in result.values()), Decimal("100"))

    def test_year_crossing_uses_start_exclusive_end_inclusive_days(self):
        bill = make_bill(
            "2024-01",
            usage_m3=31,
            period_start="2023-12-15",
            period_end="2024-01-15",
        )
        result = allocate_months([bill])

        self.assertEqual(result["2023-12"]["water"], Decimal("16"))
        self.assertEqual(result["2024-01"]["water"], Decimal("15"))

    def test_equal_mode_conserves_recurring_decimal(self):
        bill = make_bill(
            "2026-03",
            usage_m3=1,
            period_start="2025-12-31",
            period_end="2026-03-01",
        )
        result = allocate_months([bill], mode="equal")

        self.assertEqual(list(result), ["2026-01", "2026-02", "2026-03"])
        self.assertEqual(sum(item["water"] for item in result.values()), Decimal("1"))

    def test_unknown_values_remain_none_without_zero_filled_months(self):
        bill = make_bill(
            "2026-02",
            period_start="2026-01-31",
            period_end="2026-02-01",
        )
        result = allocate_months([bill])

        self.assertEqual(list(result), ["2026-02"])
        self.assertIsNone(result["2026-02"]["water"])
        self.assertIsNone(result["2026-02"]["cost"])
        self.assertIsNone(result["2026-02"]["carbon"])

    def test_latest_duplicate_bill_replaces_earlier_version(self):
        earlier = make_bill(
            "2026-02",
            fetched_at="2026-03-01T00:00:00+00:00",
            usage_m3=10,
        )
        corrected = make_bill(
            "2026-02",
            fetched_at="2026-03-02T00:00:00+00:00",
            usage_m3=20,
        )
        result = allocate_months([earlier, corrected], mode="equal")

        self.assertEqual(sum(item["water"] for item in result.values()), Decimal("20"))
        self.assertTrue(all(item["bill_count"] == 1 for item in result.values()))

    def test_overlapping_actual_periods_are_rejected_but_adjacent_are_allowed(self):
        first = make_bill(
            "2026-02",
            period_start="2026-01-01",
            period_end="2026-02-01",
        )
        overlap = make_bill(
            "2026-03",
            period_start="2026-01-31",
            period_end="2026-03-01",
        )
        adjacent = make_bill(
            "2026-03",
            period_start="2026-02-01",
            period_end="2026-03-01",
        )

        with self.assertRaises(ValueError):
            allocate_months([first, overlap])
        self.assertEqual(
            sum(item["covered_days"] for item in allocate_months([first, adjacent]).values()),
            59,
        )

    def test_fallback_is_explicit_and_does_not_claim_covered_dates(self):
        bill = make_bill("2026-03", usage_m3=2, total_twd=100)
        equal = allocate_months([bill], mode="equal")
        days = allocate_months([bill], mode="days")

        self.assertEqual(list(equal), ["2026-02", "2026-03"])
        self.assertEqual(equal["2026-02"]["water"], Decimal("1"))
        self.assertTrue(equal["2026-02"]["estimated"])
        self.assertTrue(equal["2026-02"]["period_estimated"])
        self.assertIsNone(equal["2026-02"]["covered_days"])
        self.assertEqual(days["2026-02"]["water"], Decimal("2") * 28 / 59)
        self.assertEqual(sum(item["water"] for item in days.values()), Decimal("2"))

    def test_carbon_has_no_default_and_configured_factor_is_labeled(self):
        bill = make_bill(
            "2026-02",
            usage_m3="10",
            period_start="2026-01-31",
            period_end="2026-02-02",
        )
        without_factor = allocate_months([bill])
        configured = allocate_months(
            [bill], carbon_factor="0.2", carbon_factor_source="owner_config"
        )

        self.assertIsNone(without_factor["2026-02"]["carbon"])
        self.assertEqual(configured["2026-02"]["carbon"], Decimal("2.0"))
        self.assertEqual(configured["2026-02"]["carbon_source"], "configured_estimate")
        self.assertEqual(
            configured["2026-02"]["carbon_factor_sources"], ["owner_config"]
        )

    def test_daily_allocation_only_uses_actual_dates_and_conserves(self):
        actual = make_bill(
            "2026-02",
            usage_m3=1,
            period_start="2026-01-31",
            period_end="2026-02-02",
        )
        estimated = make_bill("2026-04", usage_m3=9)
        result = allocate_days([actual, estimated])

        self.assertEqual(list(result), ["2026-02-01", "2026-02-02"])
        self.assertEqual(sum(item["water"] for item in result.values()), Decimal("1"))
        self.assertTrue(all(item["estimated"] for item in result.values()))
        self.assertTrue(all(not item["period_estimated"] for item in result.values()))
        rows = daily_allocation_list([actual, estimated])
        self.assertEqual([row["date"] for row in rows], ["2026-02-01", "2026-02-02"])

    def test_daily_equal_mode_matches_equal_month_totals(self):
        bill = make_bill(
            "2026-03",
            usage_m3=6,
            period_start="2025-12-31",
            period_end="2026-03-01",
        )
        monthly = allocate_months([bill], mode="equal")
        daily = allocate_days([bill], None, "", mode="equal")

        daily_months = {}
        for day, row in daily.items():
            month = day[:7]
            daily_months[month] = daily_months.get(month, Decimal(0)) + row["water"]
        self.assertEqual(
            daily_months,
            {month: row["water"] for month, row in monthly.items()},
        )


class ScheduleTests(unittest.TestCase):
    def test_monthly_day_31_clips_and_is_strictly_future(self):
        tz = timezone(timedelta(hours=8))
        april = datetime(2026, 4, 30, 8, 0, tzinfo=tz)
        exact = datetime(2026, 4, 30, 9, 0, tzinfo=tz)

        self.assertEqual(
            next_run(april, "monthly", "09:00", monthday=31),
            datetime(2026, 4, 30, 9, 0, tzinfo=tz),
        )
        self.assertEqual(
            next_run(exact, "monthly", "09:00", monthday=31),
            datetime(2026, 5, 31, 9, 0, tzinfo=tz),
        )

    def test_weekly_and_disabled_contract(self):
        now = datetime(2026, 9, 7, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(
            next_run(now, "weekly", "09:00", weekday=0),
            datetime(2026, 9, 14, 9, 0, tzinfo=timezone.utc),
        )
        self.assertIsNone(next_run(now, "daily", "09:00", enabled=False))

    def test_dst_gap_moves_by_timezone_transition(self):
        try:
            tz = ZoneInfo("America/New_York")
        except ZoneInfoNotFoundError:
            self.skipTest("執行環境未安裝 IANA tzdata")
        now = datetime(2026, 3, 7, 12, 0, tzinfo=tz)
        result = next_run(now, "daily", "02:30")

        self.assertEqual(result, datetime(2026, 3, 8, 3, 30, tzinfo=tz))
        self.assertGreater(result.astimezone(timezone.utc), now.astimezone(timezone.utc))

    def test_naive_now_and_invalid_settings_are_rejected(self):
        with self.assertRaises(ValueError):
            next_run(datetime(2026, 1, 1), "daily", "09:00")
        aware = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for arguments in [
            ("hourly", "09:00", {}),
            ("daily", "9:00", {}),
            ("weekly", "09:00", {"weekday": 7}),
            ("monthly", "09:00", {"monthday": 0}),
        ]:
            schedule, run_time, keywords = arguments
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                next_run(aware, schedule, run_time, **keywords)


if __name__ == "__main__":
    unittest.main()

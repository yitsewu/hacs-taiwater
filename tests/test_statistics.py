"""Tests for deterministic TaiWater external statistics publication."""

from __future__ import annotations

import asyncio
from datetime import UTC, timedelta
from decimal import Decimal
from enum import IntEnum
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


def _install_home_assistant_stubs() -> None:
    """Install the small HA surface needed by these unit tests when HA is absent."""
    try:
        importlib.import_module("homeassistant")
        return
    except ModuleNotFoundError:
        pass

    class StatisticMeanType(IntEnum):
        NONE = 0
        ARITHMETIC = 1
        CIRCULAR = 2

    modules = {
        name: ModuleType(name)
        for name in (
            "homeassistant",
            "homeassistant.components",
            "homeassistant.components.recorder",
            "homeassistant.components.recorder.models",
            "homeassistant.components.recorder.statistics",
            "homeassistant.const",
            "homeassistant.core",
            "homeassistant.exceptions",
            "homeassistant.util",
            "homeassistant.util.unit_conversion",
        )
    }
    modules["homeassistant.components.recorder"].get_instance = lambda hass: None
    models = modules["homeassistant.components.recorder.models"]
    models.StatisticData = dict
    models.StatisticMetaData = dict
    models.StatisticMeanType = StatisticMeanType
    modules["homeassistant.components.recorder.statistics"].async_add_external_statistics = (
        lambda hass, metadata, rows: None
    )
    modules["homeassistant.components.recorder.statistics"].get_last_statistics = (
        lambda hass, count, statistic_id, convert_units, types: {}
    )
    modules["homeassistant.const"].UnitOfMass = SimpleNamespace(KILOGRAMS="kg")
    modules["homeassistant.const"].UnitOfVolume = SimpleNamespace(CUBIC_METERS="m³")
    modules["homeassistant.core"].HomeAssistant = object
    modules["homeassistant.exceptions"].HomeAssistantError = RuntimeError
    modules["homeassistant.util.unit_conversion"].MassConverter = SimpleNamespace(UNIT_CLASS="mass")
    modules["homeassistant.util.unit_conversion"].VolumeConverter = SimpleNamespace(UNIT_CLASS="volume")
    sys.modules.update(modules)


_install_home_assistant_stubs()

_PACKAGE_NAME = "_taiwater_statistics_test_package"
_PACKAGE_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "taiwater"
_test_package = ModuleType(_PACKAGE_NAME)
_test_package.__path__ = [str(_PACKAGE_PATH)]
sys.modules[_PACKAGE_NAME] = _test_package
statistics = importlib.import_module(f"{_PACKAGE_NAME}.statistics")


class _FakeRecorder:
    def __init__(self, events: list[tuple], persisted: dict[str, list[dict]]) -> None:
        self.events = events
        self.persisted = persisted

    def async_clear_statistics(self, statistic_ids: list[str]) -> None:
        self.events.append(("clear", statistic_ids))
        for statistic_id in statistic_ids:
            self.persisted.pop(statistic_id, None)

    async def async_block_till_done(self) -> None:
        self.events.append(("barrier",))
        await asyncio.sleep(0)

    async def async_add_executor_job(self, target, *args):
        self.events.append(("readback", args[2]))
        return target(*args)


class _FakeHass:
    """Weak-referenceable stand-in for HomeAssistant."""


class StatisticsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.hass = _FakeHass()
        self.hass.config = SimpleNamespace(currency="TWD")
        self.events: list[tuple] = []
        self.persisted: dict[str, list[dict]] = {}
        self.recorder = _FakeRecorder(self.events, self.persisted)

    def _capture_import(self, hass, metadata, rows) -> None:
        copied_rows = [dict(row) for row in rows]
        self.persisted[metadata["statistic_id"]] = copied_rows
        self.events.append(
            (
                "import",
                dict(metadata),
                copied_rows,
            )
        )

    def _get_last_statistics(self, hass, count, statistic_id, convert_units, types) -> dict[str, list[dict]]:
        rows = self.persisted.get(statistic_id)
        if not rows:
            return {}
        last = dict(rows[-1])
        last["start"] = last["start"].timestamp()
        return {statistic_id: [last]}

    async def _publish(self, daily: dict[str, dict[str, object]]) -> str:
        with (
            patch.object(statistics, "get_instance", return_value=self.recorder),
            patch.object(
                statistics,
                "async_add_external_statistics",
                side_effect=self._capture_import,
            ),
            patch.object(
                statistics,
                "get_last_statistics",
                side_effect=self._get_last_statistics,
            ),
        ):
            return await statistics.async_publish_statistics(self.hass, "01K4VYABC123XYZ", "住家", daily)

    async def test_publish_builds_energy_eligible_hourly_cumulative_series(self) -> None:
        status = await self._publish(
            {
                "not-a-date": {"water": 99},
                "2026-03-02": {
                    "water": Decimal("24"),
                    "cost": "unknown",
                    "carbon": None,
                },
                "2026-03-01": {
                    "water": Decimal("12"),
                    "cost": Decimal("48"),
                    "carbon": Decimal("1.2"),
                },
            }
        )

        self.assertEqual(status, statistics.STATUS_PUBLISHED)
        self.assertEqual(
            self.events[0],
            (
                "clear",
                [
                    "taiwater:01k4vyabc123xyz_water",
                    "taiwater:01k4vyabc123xyz_cost",
                    "taiwater:01k4vyabc123xyz_carbon",
                ],
            ),
        )
        imports = {event[1]["statistic_id"]: event for event in self.events if event[0] == "import"}
        self.assertEqual(self.events[4], ("barrier",))

        water_meta, water_rows = imports["taiwater:01k4vyabc123xyz_water"][1:]
        self.assertEqual(water_meta["mean_type"], statistics.StatisticMeanType.NONE)
        self.assertTrue(water_meta["has_sum"])
        self.assertEqual(water_meta["unit_class"], "volume")
        self.assertEqual(water_meta["unit_of_measurement"], "m³")
        self.assertEqual(water_meta["name"], "住家 台水水量（分攤估算）")
        self.assertEqual(len(water_rows), 49)
        self.assertEqual(water_rows[0]["sum"], 0.0)
        self.assertEqual(water_rows[1]["sum"], 0.5)
        self.assertEqual(water_rows[24]["sum"], 12.0)
        self.assertEqual(water_rows[-1]["sum"], 36.0)
        self.assertEqual(
            water_rows[1]["start"].astimezone(UTC).hour,
            16,
        )
        self.assertEqual(water_rows[1]["start"].utcoffset(), timedelta(hours=8))
        self.assertEqual(water_rows[0]["start"], water_rows[1]["start"] - timedelta(hours=1))

        cost_meta, cost_rows = imports["taiwater:01k4vyabc123xyz_cost"][1:]
        self.assertEqual(cost_meta["unit_class"], None)
        self.assertEqual(cost_meta["unit_of_measurement"], "TWD")
        self.assertEqual(cost_meta["name"], "住家 台水費用（分攤估算）")
        self.assertEqual(len(cost_rows), 25)
        self.assertEqual(cost_rows[-1]["sum"], 48.0)

        carbon_meta, carbon_rows = imports["taiwater:01k4vyabc123xyz_carbon"][1:]
        self.assertEqual(carbon_meta["unit_class"], "mass")
        self.assertEqual(carbon_meta["unit_of_measurement"], "kg")
        self.assertEqual(carbon_meta["name"], "住家 台水碳排 kgCO2e（分攤估算）")
        self.assertEqual(carbon_rows[-1]["sum"], 1.2)

    async def test_cost_unit_is_twd_even_when_home_assistant_currency_is_usd(self) -> None:
        self.hass.config.currency = "USD"
        await self._publish({"2026-03-01": {"cost": Decimal("120")}})
        imported = next(event for event in self.events if event[0] == "import")
        self.assertEqual(imported[1]["unit_of_measurement"], "TWD")

    async def test_publish_fails_when_authoritative_readback_does_not_match(self) -> None:
        def wrong_last_sum(hass, count, statistic_id, convert_units, types):
            return {statistic_id: [{"start": 0.0, "sum": 999.0}]}

        with (
            patch.object(statistics, "get_instance", return_value=self.recorder),
            patch.object(
                statistics,
                "async_add_external_statistics",
                side_effect=self._capture_import,
            ),
            patch.object(
                statistics,
                "get_last_statistics",
                side_effect=wrong_last_sum,
            ),
            self.assertRaises(statistics.HomeAssistantError),
        ):
            await statistics.async_publish_statistics(
                self.hass,
                "01K4VYABC123XYZ",
                "住家",
                {"2026-03-01": {"water": Decimal("1")}},
            )

        self.assertIn(("barrier",), self.events)

    async def test_publish_rejects_same_sum_at_stale_future_timestamp(self) -> None:
        def stale_last_timestamp(hass, count, statistic_id, convert_units, types):
            result = self._get_last_statistics(hass, count, statistic_id, convert_units, types)
            result[statistic_id][-1]["start"] += 3600
            return result

        with (
            patch.object(statistics, "get_instance", return_value=self.recorder),
            patch.object(
                statistics,
                "async_add_external_statistics",
                side_effect=self._capture_import,
            ),
            patch.object(
                statistics,
                "get_last_statistics",
                side_effect=stale_last_timestamp,
            ),
            self.assertRaises(statistics.HomeAssistantError),
        ):
            await statistics.async_publish_statistics(
                self.hass,
                "01K4VYABC123XYZ",
                "住家",
                {"2026-03-01": {"water": Decimal("1")}},
            )

    async def test_full_reimport_is_idempotent_and_drops_stale_dates(self) -> None:
        daily = {
            "2026-03-01": {"water": 10},
            "2026-03-02": {"water": 20},
        }
        await self._publish(daily)
        first_import = next(event for event in self.events if event[0] == "import")

        self.events.clear()
        await self._publish(daily)
        second_import = next(event for event in self.events if event[0] == "import")
        self.assertEqual(second_import, first_import)

        self.events.clear()
        await self._publish({"2026-03-02": {"water": 7}})
        corrected = next(event for event in self.events if event[0] == "import")
        self.assertEqual(len(corrected[2]), 25)
        self.assertEqual(corrected[2][-1]["sum"], 7.0)
        self.assertEqual(self.events[0][0], "clear")

    async def test_monthly_fallback_clears_old_rows_and_returns_partial(self) -> None:
        status = await self._publish(
            {
                "2026-02-30": {"water": 5},
                "2026-03-01": {
                    "water": "unknown",
                    "cost": float("nan"),
                    "carbon": None,
                },
            }
        )

        self.assertEqual(status, statistics.STATUS_PARTIAL)
        self.assertEqual(
            [event[0] for event in self.events],
            ["clear", "barrier", "readback", "readback", "readback"],
        )

    async def test_explicit_clear_is_scoped_to_entry_ids(self) -> None:
        with (
            patch.object(statistics, "get_instance", return_value=self.recorder),
            patch.object(
                statistics,
                "get_last_statistics",
                side_effect=self._get_last_statistics,
            ),
        ):
            status = await statistics.async_clear_statistics(self.hass, "ABC123")

        self.assertEqual(status, statistics.STATUS_CLEARED)
        self.assertEqual(
            self.events,
            [
                (
                    "clear",
                    [
                        "taiwater:abc123_water",
                        "taiwater:abc123_cost",
                        "taiwater:abc123_carbon",
                    ],
                ),
                ("barrier",),
                ("readback", "taiwater:abc123_water"),
                ("readback", "taiwater:abc123_cost"),
                ("readback", "taiwater:abc123_carbon"),
            ],
        )

    async def test_invalid_entry_id_never_reaches_recorder(self) -> None:
        with self.assertRaises(ValueError):
            await statistics.async_publish_statistics(self.hass, "other:entry", "Home", {"2026-03-01": {"water": 1}})
        self.assertEqual(self.events, [])


if __name__ == "__main__":
    unittest.main()

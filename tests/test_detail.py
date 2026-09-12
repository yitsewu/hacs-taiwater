"""Synthetic account identifiers exercise summary/detail identity binding."""
import importlib
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

PACKAGE = "taiwater_detail_tests"
namespace = types.ModuleType(PACKAGE)
namespace.__path__ = [str(Path(__file__).resolve().parents[1] / "custom_components" / "taiwater")]
sys.modules[PACKAGE] = namespace
detail = importlib.import_module(f"{PACKAGE}.detail")


def summary(water_id):
    return f"""<form method="post" action="/ch/EQuery/WaterFeeQueryDetail">
    <input type="hidden" name="__RequestVerificationToken" value="synthetic">
    <input name="Name"><input name="WaterNo"><input name="TBName">
    </form><button onclick="ShowInputName('{water_id}','T11509');">Details</button>"""


class DetailIdentityTests(unittest.TestCase):
    def test_numeric_and_alphanumeric_summary_reaches_matching_account_only(self):
        for account in ("12345678901", "AB12345678Z"):
            with self.subTest(account=account):
                portal = detail.DetailClient()
                url = "https://www.water.gov.tw/ch/EQuery/WaterFeeQuerySearch"
                with patch.object(detail.TaiWaterClient, "_request", return_value=(summary(account).encode(), "text/html", url)):
                    portal._request(url)
                self.assertEqual(portal.summary_months, [(account, "T11509")])
                with patch.object(portal, "_fetch_details", return_value={}) as fetch:
                    portal.open_details("Synthetic Customer", account)
                    self.assertEqual(fetch.call_args.args[0]["WaterNo"], account)
                    self.assertEqual(fetch.call_args.args[0]["TBName"], "T11509")
                    fetch.reset_mock()
                    with self.assertRaises(detail.SiteSchemaChanged):
                        portal.open_details("Synthetic Customer", "ZZ12345678Z")
                    fetch.assert_not_called()

    def test_malformed_or_executable_button_is_not_accepted(self):
        for account in ("AB1234567Z", "AB123456789Z", "ＡB12345678Z", "AB1234567_Z", "AB12345678Z');evil('"):
            with self.subTest(account=account):
                parser = detail.DetailParser()
                parser.feed(summary(account))
                self.assertEqual(parser.buttons, [])


if __name__ == "__main__":
    unittest.main()

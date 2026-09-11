"""不連線原站的工作階段、失敗分類與 OCR 契約測試。"""
import importlib
from pathlib import Path
import sys
import types
from unittest.mock import Mock, patch
import unittest

PACKAGE = "taiwater_client_tests"
namespace = types.ModuleType(PACKAGE)
namespace.__path__ = [str(Path(__file__).resolve().parents[1] / "custom_components" / "taiwater")]
sys.modules[PACKAGE] = namespace
client = importlib.import_module(f"{PACKAGE}.client")


def detail(code="T11509"):
    return {"selected_month": code, "fields": {"用水度數": "20", "應繳總金額": "200元"},
            "months": [{"value": "T11507"}, {"value": "T11509"}]}


class ClientTests(unittest.TestCase):
    def challenge(self):
        portal = Mock()
        portal.open_details.return_value = detail("T11507")
        portal.select_month.side_effect = lambda code: detail(code)
        return client.ManualChallenge(portal, b"fixture", "image/png")

    def test_latest_is_maximum_and_same_session_for_history(self):
        challenge = self.challenge()
        with patch.object(client.time, "sleep"):
            result = client.submit_manual(challenge, "12345678901", "測試", "1234", history_limit=0)
        self.assertEqual([b["month"] for b in result["bills"]], ["2026-09", "2026-07"])
        challenge.details.submit_query.assert_called_once_with("12345678901", "1234")
        self.assertEqual(result["verification_status"], "accepted")
        self.assertEqual(result["ocr_status"], "not_needed")
        self.assertTrue(challenge.used)
        with self.assertRaises(client.QueryError) as error:
            client.submit_manual(challenge, "12345678901", "測試", "1234")
        self.assertEqual(error.exception.code, "captcha_expired")

    def test_history_partial_retains_latest(self):
        challenge = self.challenge()
        challenge.details.open_details.return_value = detail()
        challenge.details.select_month.side_effect = client.TaiWaterError("fixture")
        result = client.submit_manual(challenge, "12345678901", "測試", "1234")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["bills"][0]["month"], "2026-09")

    def test_requested_month_fetches_only_target_in_same_session(self):
        challenge = self.challenge()
        challenge.details.open_details.return_value = detail()
        result = client.submit_manual(challenge, "12345678901", "測試", "1234", requested_month="2026-07")
        self.assertEqual([bill["month"] for bill in result["bills"]], ["2026-07"])
        challenge.details.select_month.assert_called_once_with("T11507")
        self.assertEqual(result["available_months"], ["2026-09", "2026-07"])

    def test_unavailable_month_does_not_return_latest_as_requested(self):
        challenge = self.challenge()
        with self.assertRaises(client.QueryError) as error:
            client.submit_manual(challenge, "12345678901", "測試", "1234", requested_month="2026-06")
        self.assertEqual(error.exception.code, "month_unavailable")
        self.assertEqual(error.exception.verification_status, "accepted")
        challenge.details.select_month.assert_not_called()

    def test_mismatched_selected_month_is_rejected(self):
        challenge = self.challenge()
        challenge.details.select_month.side_effect = lambda code: detail("T11507")
        with self.assertRaises(client.QueryError) as error:
            client.submit_manual(challenge, "12345678901", "測試", "1234", requested_month="2026-09")
        self.assertEqual(error.exception.code, "site_changed")

    def test_invalid_month_does_not_start_network(self):
        for month in ("2026-13", "2026-7", "1911-01", "2911-01", "2026-07\n", None):
            if month is None:
                continue
            with patch.object(client, "prepare_manual") as prepare:
                with self.assertRaises(client.QueryError) as error:
                    client.query("12345678901", "測試", requested_month=month)
            self.assertEqual(error.exception.code, "invalid_month")
            prepare.assert_not_called()

    def test_recognized_code_not_equal_accepted_code(self):
        challenge = self.challenge()
        challenge.details.submit_query.side_effect = client.QueryRejected("驗證碼錯誤或已過期，請重新取得。")
        with self.assertRaises(client.QueryError) as error:
            client.submit_manual(challenge, "12345678901", "測試", "1234", ocr_status="success")
        self.assertEqual(error.exception.ocr_status, "success")
        self.assertEqual(error.exception.verification_status, "rejected")

    def test_name_failure_preserves_verified_captcha(self):
        challenge = self.challenge()
        challenge.details.open_details.side_effect = client.QueryRejected("姓名錯誤")
        with self.assertRaises(client.QueryError) as error:
            client.submit_manual(challenge, "12345678901", "測試", "1234")
        self.assertEqual(error.exception.code, "invalid_auth")
        self.assertEqual(error.exception.verification_status, "accepted")

    def test_known_history_is_not_refetched(self):
        challenge = self.challenge()
        challenge.details.open_details.return_value = detail()
        result = client.submit_manual(challenge, "12345678901", "測試", "1234", known_months={"2026-07"})
        challenge.details.select_month.assert_not_called()
        self.assertEqual(len(result["bills"]), 1)

    def test_destination_is_bounded_before_network(self):
        for url in ["http://www.water.gov.tw/", "https://evil.example/", "https://www.water.gov.tw.evil.example/", "https://www.water.gov.tw:444/"]:
            with self.assertRaises(client.SiteSchemaChanged):
                client._official_url(url)

    def test_native_ocr_unavailable_is_explicit(self):
        with patch.dict(sys.modules, {"ddddocr": None}):
            with self.assertRaises(client.QueryError) as error:
                client.recognize(b"fixture")
        self.assertEqual(error.exception.code, "ocr_unavailable")

    def test_new_query_gets_fresh_challenge(self):
        with patch.object(client, "prepare_manual", side_effect=[self.challenge(), self.challenge()]) as prepare, patch.object(client, "recognize", return_value="1234"):
            client.query("12345678901", "測試")
            client.query("12345678901", "測試")
        self.assertEqual(prepare.call_count, 2)

    def test_ocr_service_rejection_is_failure_not_unavailable(self):
        for status, expected in ((422, "failed"), (503, "unavailable")):
            opener = Mock()
            opener.open.side_effect = client.urllib.error.HTTPError("http://ocr/recognize", status, "fixture", {}, None)
            with patch.object(client.urllib.request, "build_opener", return_value=opener):
                with self.assertRaises(client.QueryError) as error:
                    client.recognize(b"fixture", "http://ocr")
            self.assertEqual(error.exception.ocr_status, expected)

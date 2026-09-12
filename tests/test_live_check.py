import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO

spec = importlib.util.spec_from_file_location("live_check", Path(__file__).resolve().parents[1] / "scripts/live_check.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def success():
    return {"status": "success", "ocr_status": "success", "verification_status": "accepted",
            "ocr_backend": "builtin", "bills": [{"fields": {"用水度數": "0", "應繳總金額": "0元"}}]}


class ProbeTests(unittest.TestCase):
    def test_only_one_bill_no_history_and_no_private_result(self):
        query = Mock(return_value=success())
        result = probe.check(query, "synthetic-id", "synthetic-name")
        query.assert_called_once_with("synthetic-id", "synthetic-name", history_limit=None)
        self.assertEqual(result, {"status": "available", "code": "ok", "attempts": 1})

    def test_http_or_empty_result_is_not_success(self):
        for value in ({"status": "success"}, None, {**success(), "bills": []}):
            self.assertEqual(probe.check(Mock(return_value=value), "id", "name")["status"], "failed")

    def test_bounded_retry(self):
        error = RuntimeError("private response")
        error.code = "cannot_connect"
        query = Mock(side_effect=[error, success()])
        sleep = Mock()
        self.assertEqual(probe.check(query, "id", "name", sleep)["attempts"], 2)
        sleep.assert_called_once_with(15)
        query = Mock(side_effect=error)
        self.assertEqual(probe.check(query, "id", "name", Mock())["code"], "cannot_connect")
        self.assertEqual(query.call_count, 2)

    def test_private_output_and_errors_never_escape(self):
        def query(*args, **kwargs):
            print("private account and bill")
            raise RuntimeError("private upstream HTML")
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            result = probe.check(query, "id", "name")
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(result["code"], "check_error")

    def test_schema_failure_is_not_retried(self):
        error = RuntimeError("private")
        error.code = "site_changed"
        query = Mock(side_effect=error)
        self.assertEqual(probe.check(query, "id", "name")["attempts"], 1)

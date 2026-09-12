"""Probe one real bill; publish only fixed codes and execution metadata."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import importlib
import json
import os
from pathlib import Path
import re
import sys
import time
import types

SAFE_CODES = {"cannot_connect", "site_changed", "invalid_response", "invalid_auth",
              "upstream_rejected", "ocr_failed", "ocr_unavailable", "captcha_expired"}
RETRY_CODES = {"cannot_connect", "upstream_rejected", "ocr_failed", "captcha_expired"}


def check(query, water_id, customer_name, sleep=time.sleep):
    for attempt in range(1, 3):
        try:
            # Third-party output and exception strings must never reach public logs.
            with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
                result = query(water_id, customer_name, history_limit=None)
            valid = (isinstance(result, dict) and result.get("status") == "success"
                     and result.get("ocr_status") == "success"
                     and result.get("verification_status") == "accepted"
                     and result.get("ocr_backend") == "builtin"
                     and len(result.get("bills", [])) == 1
                     and bool(result["bills"][0].get("fields", {}).get("用水度數"))
                     and bool(result["bills"][0].get("fields", {}).get("應繳總金額")))
            code = "ok" if valid else "invalid_response"
        except Exception as error:
            raw_code = getattr(error, "code", None)
            code = raw_code if isinstance(raw_code, str) and raw_code in SAFE_CODES else "check_error"
        if code not in RETRY_CODES or attempt == 2:
            return {"status": "available" if code == "ok" else "failed", "code": code, "attempts": attempt}
        sleep(15)
    raise AssertionError("unreachable")


def main():
    version = os.environ.get("TAIWATER_RELEASE", "")
    water_id = os.environ.pop("TAIWATER_MONITOR_WATER_ID", "")
    customer_name = os.environ.pop("TAIWATER_MONITOR_CUSTOMER_NAME", "")
    report = {"status": "unknown", "code": "check_error", "attempts": 0}
    try:
        if not water_id or not customer_name:
            report["code"] = "missing_configuration"
        elif not re.fullmatch(r"v\d+\.\d+\.\d+", version):
            report["code"] = "invalid_release"
        else:
            root = Path(os.environ["TAIWATER_RELEASE_ROOT"]).resolve()
            namespace = types.ModuleType("taiwater_live_probe")
            namespace.__path__ = [str(root / "custom_components" / "taiwater")]
            sys.modules[namespace.__name__] = namespace
            with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
                client = importlib.import_module("taiwater_live_probe.client")
            report = check(client.query, water_id, customer_name)
    except Exception:
        pass  # No exception text, traceback, response, or account information.
    report["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report["version"] = version if re.fullmatch(r"v\d+\.\d+\.\d+", version) else "unknown"
    print(json.dumps(report))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write("## 台水實際查詢檢查\n\n")
            for key, value in report.items():
                stream.write(f"- {key}: `{value}`\n")
            stream.write("\nGitHub 雲端、單一測試帳戶、最新一期帳單；不驗證 HA Recorder 或所有帳戶。\n")
    return 0 if report["status"] == "available" else 1


if __name__ == "__main__":
    sys.exit(main())

"""整合共用設定。"""

DOMAIN = "taiwater"
NAME = "台灣自來水公司"
VERSION = "0.1.0"
PLATFORMS = ["sensor", "binary_sensor", "button", "switch"]
DEFAULT_OPTIONS = {
    "enabled": True,
    "schedule": "weekly",
    "query_time": "09:00",
    "weekday": 0,
    "monthday": 1,
    "history_limit": 0,
    "allocation": "days",
    "carbon_factor": None,
    "carbon_factor_source": "",
    "ocr_url": "",
}
STORAGE_VERSION = 1

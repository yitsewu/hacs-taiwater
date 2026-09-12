"""Run inside an unmodified official HA Core image, without external OCR."""
import asyncio
import importlib
import importlib.metadata
import json
import platform
from pathlib import Path
import resource
import sys
import tempfile
import time
import types
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.requirements import async_process_requirements

from ocr_smoke import synthetic_digits_png


async def main():
    root = Path(__file__).resolve().parents[1]/"custom_components/taiwater"
    manifest = json.loads((root/"manifest.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as config:
        hass = HomeAssistant(config)
        try:
            # Exercise HA's real resolver, including its package constraints/wheel index.
            await async_process_requirements(hass, "taiwater", manifest["requirements"])
            namespace = types.ModuleType("taiwater_core_smoke")
            namespace.__path__ = [str(root)]
            sys.modules[namespace.__name__] = namespace
            client = importlib.import_module(namespace.__name__+".client")
            native = importlib.import_module(namespace.__name__+".native_ocr")
            assert importlib.util.find_spec("ddddocr") is None
            timings = []
            for code in ("12345", "54321", "2345"):
                start = time.monotonic()
                with patch("urllib.request.build_opener", side_effect=AssertionError("no external OCR")):
                    actual = await hass.async_add_executor_job(client.recognize, synthetic_digits_png(code))
                assert actual == code, "synthetic_recognition_mismatch"
                timings.append(round(time.monotonic()-start,3))
            assert native.ENGINE._model is not None
            print(json.dumps({"result":"passed","core":importlib.metadata.version("homeassistant"),
                "python":platform.python_version(),"architecture":platform.machine(),
                "numpy":importlib.metadata.version("numpy"),"pillow":importlib.metadata.version("Pillow"),
                "inference_seconds":timings,"peak_rss_kib":resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                "external_ocr":False,"model_download":False}))
        finally:
            await hass.async_stop()


asyncio.run(main())

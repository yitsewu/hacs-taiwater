"""Bundled model behavior without HA, external OCR, model downloads or real CAPTCHA."""
from concurrent.futures import ThreadPoolExecutor
import importlib
import io
from pathlib import Path
import sys
import threading
import time
import types
from unittest.mock import Mock, patch

from PIL import Image
import pytest

PACKAGE = "taiwater_native_tests"
namespace = types.ModuleType(PACKAGE)
namespace.__path__ = [str(Path(__file__).resolve().parents[1] / "custom_components/taiwater")]
sys.modules[PACKAGE] = namespace
native = importlib.import_module(f"{PACKAGE}.native_ocr")


def png():
    out = io.BytesIO()
    Image.new("RGB", (160,64), "white").save(out, format="PNG")
    return out.getvalue()


def test_integrity_failure_and_load_backoff_recovery(tmp_path):
    with patch.object(native, "_ROOT", tmp_path):
        with pytest.raises(FileNotFoundError):
            native._load_model()
    factory = Mock(side_effect=[ValueError("synthetic"), Mock(predict=Mock(return_value="1234"))])
    engine = native.LocalOCR(factory)
    with patch.object(native.time, "monotonic", return_value=10):
        for _ in range(2):
            with pytest.raises(native.OCRUnavailable):
                engine.recognize(png())
        assert factory.call_count == 1
    with patch.object(native.time, "monotonic", return_value=41):
        assert engine.recognize(png()) == "1234"
    assert factory.call_count == 2


def test_changed_model_hash_rejected(tmp_path):
    for name in native._HASHES:
        (tmp_path/name).write_bytes(b"not-a-model")
    with patch.object(native, "_ROOT", tmp_path), pytest.raises(native.OCRUnavailable):
        native._load_model()


def test_parallel_accounts_load_once_and_serialize_inference():
    active = 0
    peak = 0
    guard = threading.Lock()

    def predict(_values):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(.02)
        with guard:
            active -= 1
        return "1234"

    factory = Mock(return_value=Mock(predict=predict))
    engine = native.LocalOCR(factory)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(engine.recognize, [png()]*4)) == ["1234"]*4
    assert factory.call_count == 1 and peak == 1


@pytest.mark.parametrize("result", ["", "123", "unexpected text", None])
def test_invalid_recognition_keeps_loaded_model(result):
    factory = Mock(return_value=Mock(predict=Mock(return_value=result)))
    engine = native.LocalOCR(factory)
    with pytest.raises(native.OCRFailed):
        engine.recognize(png())
    assert engine._model is not None


def test_inference_failure_releases_model_for_retry():
    engine = native.LocalOCR(lambda: Mock(predict=Mock(side_effect=RuntimeError("synthetic"))))
    with pytest.raises(native.OCRUnavailable):
        engine.recognize(png())
    assert engine._model is None


@pytest.mark.parametrize("image", [b"", b"invalid", b"x"*(native.MAX_BYTES+1)], ids=["empty", "invalid", "oversized"])
def test_invalid_images_rejected(image):
    engine = native.LocalOCR(lambda: Mock())
    with pytest.raises(native.OCRFailed):
        engine.recognize(image)


@pytest.mark.asyncio
async def test_event_loop_call_refused():
    with pytest.raises(native.OCRUnavailable):
        native.ENGINE.recognize(png())


def test_bundled_model_synthetic_accuracy_without_external_runtime():
    # Generated public test glyphs; no saved CAPTCHA or private portal response.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"scripts"))
    from ocr_smoke import synthetic_digits_png

    with patch("urllib.request.urlopen", side_effect=AssertionError("offline inference")):
        model = native.LocalOCR()
        for expected in ("12345", "54321", "2345"):
            assert model.recognize(synthetic_digits_png(expected)) == expected

"""Bundled, offline OCR shared by all entries; call only from an executor."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
from pathlib import Path
import re
import threading
import time

_ROOT = Path(__file__).with_name("models")
_HASHES = {
    "ocr_graph.json": "af7c68d7c63c8eece8c3705db8b63c46d12c36fac07c731b4d2d7e5f4a85de2e",
    "ocr_weights.npz": "7534858b7f50ed0b137814ba1875dfb858bc34812ac149445e5d76c5eb067f6d",
}
MAX_BYTES = 2_000_000
MAX_PIXELS = 4_000_000
MAX_WIDTH = 512


class OCRUnavailable(Exception):
    """Dependency/model unavailable; retry later or use manual verification."""


class OCRFailed(Exception):
    """Image or recognized text failed validation."""


def _load_model():
    import numpy as np

    from ._numpy_ocr import Model

    payloads = {}
    for name, digest in _HASHES.items():
        content = (_ROOT / name).read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise OCRUnavailable
        payloads[name] = content
    graph = json.loads(payloads["ocr_graph.json"])
    with np.load(io.BytesIO(payloads["ocr_weights.npz"]), allow_pickle=False) as archive:
        weights = {key: archive[key] for key in archive.files}
    return Model(graph, weights)


class LocalOCR:
    """One lazy model, one inference at a time, bounded wait and load-failure backoff."""

    def __init__(self, factory=None):
        self._factory = factory or _load_model
        self._model = None
        self._lock = threading.Lock()
        self._retry_at = 0.0

    def recognize(self, content: bytes) -> str:
        # Guard future call sites against accidentally moving inference onto HA's loop.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise OCRUnavailable
        if not isinstance(content, bytes) or not 0 < len(content) <= MAX_BYTES:
            raise OCRFailed
        if not self._lock.acquire(timeout=30):
            raise OCRUnavailable
        try:
            if self._model is None:
                if time.monotonic() < self._retry_at:
                    raise OCRUnavailable
                try:
                    self._model = self._factory()
                except Exception:
                    self._retry_at = time.monotonic() + 30
                    raise OCRUnavailable from None
            try:
                import numpy as np
                from PIL import Image

                with Image.open(io.BytesIO(content)) as image:
                    width = int(image.width * 64 / image.height)
                    if image.width * image.height > MAX_PIXELS or not 8 <= width <= MAX_WIDTH:
                        raise OCRFailed
                    image.seek(0)
                    image = image.convert("RGB").resize((width, 64), Image.Resampling.LANCZOS).convert("L")
                    values = (np.asarray(image, dtype=np.float32) / 255 - .5) / .5
            except ImportError:
                raise OCRUnavailable from None
            except Exception:
                raise OCRFailed from None
            try:
                result = self._model.predict(values[None, None])
            except Exception:
                self._model = None
                self._retry_at = time.monotonic() + 30
                raise OCRUnavailable from None
            if not isinstance(result, str) or not re.fullmatch(r"[A-Za-z0-9]{4,8}", result):
                raise OCRFailed
            return result
        finally:
            self._lock.release()


ENGINE = LocalOCR()

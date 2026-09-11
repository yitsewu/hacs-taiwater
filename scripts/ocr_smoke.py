"""Exercise the OCR container over HTTP with an in-memory synthetic PNG."""

from __future__ import annotations

import argparse
import binascii
import json
import re
import struct
import time
import urllib.error
import urllib.request
import zlib


_DIGITS = {
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
}
_CODE_PATTERN = re.compile(r"[A-Za-z0-9]{4,8}\Z")


def _chunk(kind: bytes, payload: bytes) -> bytes:
    checksum = binascii.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)


def synthetic_digits_png(text: str = "12345", scale: int = 10) -> bytes:
    """Create a deterministic, high-contrast RGB PNG without files or dependencies."""
    glyph_width = 5 * scale
    glyph_height = 7 * scale
    gap = scale
    margin = 2 * scale
    width = margin * 2 + len(text) * glyph_width + (len(text) - 1) * gap
    height = margin * 2 + glyph_height
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            ink = False
            local_y = y - margin
            if 0 <= local_y < glyph_height:
                slot = x - margin
                stride = glyph_width + gap
                index, local_x = divmod(slot, stride)
                if 0 <= index < len(text) and local_x < glyph_width:
                    pattern = _DIGITS[text[index]]
                    ink = pattern[local_y // scale][local_x // scale] == "1"
            value = 0 if ink else 255
            row.extend((value, value, value))
        rows.append(bytes(row))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(b"".join(rows), level=9))
        + _chunk(b"IEND", b"")
    )


def wait_until_healthy(base_url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=1) as response:
                if response.status == 200 and json.load(response) == {"status": "ok"}:
                    return
        except (OSError, ValueError, urllib.error.URLError):
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError("OCR health check did not become ready")
        time.sleep(0.5)


def run_smoke(base_url: str, startup_timeout: float = 60) -> None:
    base_url = base_url.rstrip("/")
    wait_until_healthy(base_url, startup_timeout)
    request = urllib.request.Request(
        f"{base_url}/recognize",
        data=synthetic_digits_png(),
        headers={"Content-Type": "application/octet-stream"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.load(response)
    if response.status != 200 or not isinstance(result, dict):
        raise RuntimeError("OCR response contract failed")
    code = result.get("code")
    if not isinstance(code, str) or _CODE_PATTERN.fullmatch(code) is None:
        raise RuntimeError("OCR result did not contain 4-8 alphanumeric characters")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:18080")
    parser.add_argument("--startup-timeout", type=float, default=60)
    args = parser.parse_args()
    run_smoke(args.url, args.startup_timeout)
    print("OCR HTTP smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

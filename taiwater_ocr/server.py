"""Bounded, memory-only HTTP service for TaiWater CAPTCHA OCR."""

from __future__ import annotations

import io
import json
import re
import signal
import socket
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Callable


LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8080
MAX_BODY_BYTES = 2_000_000
MAX_IMAGE_PIXELS = 4_000_000
MAX_CONCURRENT_REQUESTS = 2
READ_TIMEOUT_SECONDS = 10.0
_CODE_PATTERN = re.compile(r"[A-Za-z0-9]{4,8}\Z")


class OCRFailed(Exception):
    """The supplied image could not produce a safe OCR result."""


class OCRUnavailable(Exception):
    """The OCR runtime could not be initialized."""


class RequestProblem(Exception):
    """A client error with a fixed, non-sensitive response."""

    def __init__(self, status: HTTPStatus, error: str):
        super().__init__(error)
        self.status = status
        self.error = error


class OCRService:
    """Initialize and use one OCR engine under a single serialization lock."""

    def __init__(self, engine_factory: Callable[..., object] | None = None):
        self._engine_factory = engine_factory
        self._engine = None
        self._lock = threading.RLock()

    def _initialize_locked(self) -> None:
        if self._engine is not None:
            return
        try:
            factory = self._engine_factory
            if factory is None:
                import ddddocr

                factory = ddddocr.DdddOcr
            self._engine = factory(show_ad=False)
        except Exception as exc:
            raise OCRUnavailable from exc

    def initialize(self) -> None:
        with self._lock:
            self._initialize_locked()

    def recognize(self, image_bytes: bytes) -> str:
        try:
            from PIL import Image

            with Image.open(io.BytesIO(image_bytes)) as image:
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise OCRFailed
                image.seek(0)
                normalized = io.BytesIO()
                image.convert("RGB").save(normalized, format="PNG")
                normalized_bytes = normalized.getvalue()
        except OCRFailed:
            raise
        except ImportError as exc:
            raise OCRUnavailable from exc
        except Exception as exc:
            raise OCRFailed from exc

        try:
            with self._lock:
                self._initialize_locked()
                result = self._engine.classification(normalized_bytes)
        except OCRUnavailable:
            raise
        except Exception as exc:
            raise OCRFailed from exc
        finally:
            normalized_bytes = b""

        if not isinstance(result, str) or _CODE_PATTERN.fullmatch(result) is None:
            raise OCRFailed
        return result


def _json_bytes(value: dict[str, str]) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")


class OCRRequestHandler(BaseHTTPRequestHandler):
    """Serve only the health and recognition endpoints without request logging."""

    protocol_version = "HTTP/1.1"
    server_version = "TaiWaterOCR"
    sys_version = ""
    ocr_service: OCRService

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(READ_TIMEOUT_SECONDS)

    def version_string(self) -> str:
        return self.server_version

    def log_message(self, format: str, *args: object) -> None:
        return

    def send_error(self, code: int, message=None, explain=None) -> None:
        """Replace BaseHTTPRequestHandler HTML errors with a fixed JSON body."""
        try:
            status = HTTPStatus(code)
        except ValueError:
            status = HTTPStatus.BAD_REQUEST
        self._send_json(status, {"error": "invalid_request"})

    def handle_expect_100(self) -> bool:
        self._send_error(HTTPStatus.EXPECTATION_FAILED, "expectation_failed")
        return False

    def _send_json(self, status: HTTPStatus, value: dict[str, str]) -> None:
        payload = _json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def _send_error(self, status: HTTPStatus, error: str) -> None:
        self._send_json(status, {"error": error})

    def _content_length(self) -> int:
        if self.headers.get("Transfer-Encoding") is not None:
            raise RequestProblem(HTTPStatus.BAD_REQUEST, "invalid_request")
        values = self.headers.get_all("Content-Length", [])
        if not values:
            raise RequestProblem(HTTPStatus.LENGTH_REQUIRED, "length_required")
        if len(values) != 1:
            raise RequestProblem(HTTPStatus.BAD_REQUEST, "invalid_request")
        try:
            length = int(values[0], 10)
        except ValueError as exc:
            raise RequestProblem(HTTPStatus.BAD_REQUEST, "invalid_request") from exc
        if length <= 0:
            raise RequestProblem(HTTPStatus.BAD_REQUEST, "invalid_request")
        if length > MAX_BODY_BYTES:
            raise RequestProblem(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "payload_too_large")
        return length

    def _read_body(self) -> bytes:
        length = self._content_length()
        deadline = time.monotonic() + READ_TIMEOUT_SECONDS
        chunks: list[bytes] = []
        remaining = length
        try:
            while remaining:
                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    raise socket.timeout
                self.connection.settimeout(timeout)
                chunk = self.rfile.read(min(remaining, 64 * 1024))
                if not chunk:
                    raise RequestProblem(HTTPStatus.BAD_REQUEST, "incomplete_body")
                chunks.append(chunk)
                remaining -= len(chunk)
        except (TimeoutError, socket.timeout) as exc:
            raise RequestProblem(HTTPStatus.REQUEST_TIMEOUT, "request_timeout") from exc
        except OSError as exc:
            raise RequestProblem(HTTPStatus.BAD_REQUEST, "invalid_request") from exc
        return b"".join(chunks)

    def do_GET(self) -> None:
        if self.path != "/health":
            self._send_error(HTTPStatus.NOT_FOUND, "not_found")
            return
        self._send_json(HTTPStatus.OK, {"status": "ok"})

    def do_POST(self) -> None:
        if self.path != "/recognize":
            self._send_error(HTTPStatus.NOT_FOUND, "not_found")
            return
        try:
            body = self._read_body()
        except RequestProblem as exc:
            self._send_error(exc.status, exc.error)
            return

        try:
            code = self.ocr_service.recognize(body)
        except OCRUnavailable:
            self._send_error(HTTPStatus.SERVICE_UNAVAILABLE, "ocr_unavailable")
        except OCRFailed:
            self._send_error(HTTPStatus.UNPROCESSABLE_ENTITY, "ocr_failed")
        except Exception:
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error")
        else:
            self._send_json(HTTPStatus.OK, {"code": code})
        finally:
            body = b""

    def do_HEAD(self) -> None:
        self._send_error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed")

    def do_PUT(self) -> None:
        self._send_error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed")

    def do_DELETE(self) -> None:
        self._send_error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed")

    def do_PATCH(self) -> None:
        self._send_error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed")


class BoundedThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """A threaded server that never creates more than the configured workers."""

    daemon_threads = False
    block_on_close = True
    allow_reuse_address = True
    request_queue_size = 4

    def __init__(self, server_address, handler_class, max_workers: int):
        self._workers = threading.BoundedSemaphore(max_workers)
        super().__init__(server_address, handler_class)

    def process_request(self, request, client_address) -> None:
        self._workers.acquire()
        try:
            super().process_request(request, client_address)
        except Exception:
            self._workers.release()
            self.shutdown_request(request)

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._workers.release()

    def handle_error(self, request, client_address) -> None:
        return


def make_server(host: str, port: int, service: OCRService) -> BoundedThreadingHTTPServer:
    class Handler(OCRRequestHandler):
        ocr_service = service

    return BoundedThreadingHTTPServer(
        (host, port), Handler, max_workers=MAX_CONCURRENT_REQUESTS
    )


def main() -> int:
    service = OCRService()
    try:
        service.initialize()
    except OCRUnavailable:
        return 1

    stop_requested = threading.Event()

    def request_stop(signum, frame) -> None:
        stop_requested.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    server = make_server(LISTEN_HOST, LISTEN_PORT, service)
    server.timeout = 0.5
    try:
        while not stop_requested.is_set():
            server.handle_request()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

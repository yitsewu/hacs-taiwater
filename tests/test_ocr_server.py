import contextlib
import http.client
import io
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "taiwater_ocr"))
import server


class FakeImage:
    width = 32
    height = 16

    def __init__(self):
        self.seek_calls = []
        self.convert_calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def seek(self, frame):
        self.seek_calls.append(frame)

    def convert(self, mode):
        self.convert_calls.append(mode)
        return self

    def save(self, output, format):
        self.saved_format = format
        output.write(b"normalized-png")


def fake_pil_module(fake_image=None):
    image = fake_image or FakeImage()

    class ImageAPI:
        @staticmethod
        def open(source):
            source.read()
            return image

    return SimpleNamespace(Image=ImageAPI), image


class RunningServer:
    def __init__(self, service):
        self.server = server.make_server("127.0.0.1", 0, service)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}
        )

    @property
    def port(self):
        return self.server.server_address[1]

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        return response.status, json.loads(payload)


class FakeService:
    def __init__(self, result="12345", error=None):
        self.result = result
        self.error = error

    def recognize(self, image):
        if self.error is not None:
            raise self.error
        return self.result


class OCRServiceTest(unittest.TestCase):
    def test_engine_module_is_lazy_and_first_frame_becomes_rgb_png(self):
        engine = Mock()
        engine.classification.return_value = "12345"
        factory = Mock(return_value=engine)
        pil_module, fake_image = fake_pil_module()

        with patch.dict(
            sys.modules,
            {
                "ddddocr": SimpleNamespace(DdddOcr=factory),
                "PIL": pil_module,
            },
        ):
            service = server.OCRService()
            self.assertEqual(service.recognize(b"source-image"), "12345")

        factory.assert_called_once_with(show_ad=False)
        engine.classification.assert_called_once_with(b"normalized-png")
        self.assertEqual(fake_image.seek_calls, [0])
        self.assertEqual(fake_image.convert_calls, ["RGB"])
        self.assertEqual(fake_image.saved_format, "PNG")

    def test_concurrent_calls_initialize_once_and_serialize_engine(self):
        state_lock = threading.Lock()
        factory_calls = 0
        active_calls = 0
        max_active_calls = 0

        class Engine:
            def classification(self, image):
                nonlocal active_calls, max_active_calls
                with state_lock:
                    active_calls += 1
                    max_active_calls = max(max_active_calls, active_calls)
                time.sleep(0.02)
                with state_lock:
                    active_calls -= 1
                return "12345"

        def factory(**kwargs):
            nonlocal factory_calls
            with state_lock:
                factory_calls += 1
            time.sleep(0.02)
            return Engine()

        service = server.OCRService(engine_factory=factory)
        results = []
        pil_module, _ = fake_pil_module()
        with patch.dict(sys.modules, {"PIL": pil_module}):
            threads = [
                threading.Thread(
                    target=lambda: results.append(service.recognize(b"source-image"))
                )
                for _ in range(4)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)

        self.assertEqual(results, ["12345"] * 4)
        self.assertEqual(factory_calls, 1)
        self.assertEqual(max_active_calls, 1)


class OCRHTTPTest(unittest.TestCase):
    def test_health_and_recognize_contract(self):
        with RunningServer(FakeService()) as running:
            status, result = running.request("GET", "/health")
            self.assertEqual((status, result), (200, {"status": "ok"}))

            status, result = running.request(
                "POST",
                "/recognize",
                body=b"source-image",
                headers={"Content-Type": "application/octet-stream"},
            )
            self.assertEqual((status, result), (200, {"code": "12345"}))

    def test_oversize_content_length_is_rejected_without_reading_body(self):
        with RunningServer(FakeService()) as running:
            connection = http.client.HTTPConnection(
                "127.0.0.1", running.port, timeout=2
            )
            connection.putrequest("POST", "/recognize")
            connection.putheader("Content-Length", str(server.MAX_BODY_BYTES + 1))
            connection.endheaders()
            response = connection.getresponse()
            result = json.loads(response.read())
            connection.close()

        self.assertEqual(response.status, 413)
        self.assertEqual(result, {"error": "payload_too_large"})

    def test_ocr_failure_is_controlled(self):
        with RunningServer(FakeService(error=server.OCRFailed())) as running:
            status, result = running.request(
                "POST", "/recognize", body=b"source-image"
            )
        self.assertEqual(status, 422)
        self.assertEqual(result, {"error": "ocr_failed"})

    def test_unknown_method_uses_controlled_json(self):
        with RunningServer(FakeService()) as running:
            status, result = running.request("OPTIONS", "/recognize")
        self.assertEqual(status, 501)
        self.assertEqual(result, {"error": "invalid_request"})

    def test_requests_never_exceed_worker_limit(self):
        class BlockingService:
            def __init__(self):
                self.active = 0
                self.max_active = 0
                self.lock = threading.Lock()
                self.two_active = threading.Event()
                self.release = threading.Event()

            def recognize(self, image):
                with self.lock:
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                    if self.active == server.MAX_CONCURRENT_REQUESTS:
                        self.two_active.set()
                self.release.wait(timeout=2)
                with self.lock:
                    self.active -= 1
                return "12345"

        service = BlockingService()
        completed = []
        with RunningServer(service) as running:
            workers = [
                threading.Thread(
                    target=lambda: completed.append(
                        running.request("POST", "/recognize", body=b"source-image")
                    )
                )
                for _ in range(server.MAX_CONCURRENT_REQUESTS + 1)
            ]
            for worker in workers:
                worker.start()
            self.assertTrue(service.two_active.wait(timeout=1))
            time.sleep(0.05)
            with service.lock:
                self.assertEqual(service.active, server.MAX_CONCURRENT_REQUESTS)
            self.assertEqual(completed, [])

            service.release.set()
            for worker in workers:
                worker.join(timeout=2)

        self.assertEqual(service.max_active, server.MAX_CONCURRENT_REQUESTS)
        self.assertEqual(len(completed), 3)
        self.assertTrue(
            all(item == (200, {"code": "12345"}) for item in completed)
        )

    def test_secret_exception_is_not_logged_or_returned(self):
        marker = "fixture-secret-must-not-escape"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with RunningServer(FakeService(error=RuntimeError(marker))) as running:
                status, result = running.request(
                    "POST", "/recognize", body=marker.encode("ascii")
                )

        visible = stdout.getvalue() + stderr.getvalue() + json.dumps(result)
        self.assertEqual(status, 500)
        self.assertEqual(result, {"error": "internal_error"})
        self.assertNotIn(marker, visible)


if __name__ == "__main__":
    unittest.main()

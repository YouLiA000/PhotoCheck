"""The HTTP process endpoint must reject overlapping heavy jobs."""

from __future__ import annotations

import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

from app import server


class ProcessConcurrencyTests(unittest.TestCase):
    def test_second_process_request_gets_429(self) -> None:
        started = threading.Event()
        release = threading.Event()
        first_status: list[int] = []

        def slow_process(*args, **kwargs):
            started.set()
            release.wait(3)
            return {"ok": True}

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        httpd.daemon_threads = True
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        with (
            mock.patch.object(server.service, "process", side_effect=slow_process),
            mock.patch.object(server, "PROCESS_SLOT", threading.BoundedSemaphore(1)),
        ):
            thread.start()
            try:
                def first_request() -> None:
                    connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
                    try:
                        connection.request(
                            "POST",
                            "/api/process",
                            body=json.dumps({"source_id": "sample:one"}),
                            headers={"Content-Type": "application/json"},
                        )
                        first_status.append(connection.getresponse().status)
                    finally:
                        connection.close()

                worker = threading.Thread(target=first_request)
                worker.start()
                self.assertTrue(started.wait(3), "first process request did not start")

                second = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
                try:
                    second.request(
                        "POST",
                        "/api/process",
                        body=json.dumps({"source_id": "sample:two"}),
                        headers={"Content-Type": "application/json"},
                    )
                    self.assertEqual(second.getresponse().status, 429)
                finally:
                    second.close()

                release.set()
                worker.join(5)
                self.assertEqual(first_status, [200])
            finally:
                release.set()
                httpd.shutdown()
                httpd.server_close()
                thread.join(5)


if __name__ == "__main__":
    unittest.main()

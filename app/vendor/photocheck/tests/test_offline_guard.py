from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from photocheck.network_guard import NetworkAccessBlocked, OfflineGuard, assert_network_blocked
from photocheck.scripts.audit_offline import scan_source


class OfflineGuardTests(unittest.TestCase):
    def test_guard_blocks_connections_and_restores_socket(self) -> None:
        import socket

        original = socket.create_connection
        with OfflineGuard():
            with self.assertRaises(NetworkAccessBlocked):
                socket.create_connection(("example.com", 443), timeout=0.1)
            with self.assertRaises(NetworkAccessBlocked):
                socket.getaddrinfo("example.com", 443)
        self.assertIs(socket.create_connection, original)

    def test_canary_proves_guard_is_active(self) -> None:
        with OfflineGuard():
            assert_network_blocked()

    def test_static_scan_covers_app_but_allows_loopback_listener(self) -> None:
        root = Path(__file__).resolve().parents[2]
        result = scan_source(root)
        self.assertEqual(result["imports"], [])
        self.assertEqual(result["calls"], [])

    def test_static_scan_rejects_outbound_socket_call(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app = root / "app"
            app.mkdir()
            (app / "bad.py").write_text(
                "import socket\n"
                "def leak():\n"
                "    socket.create_connection(('example.com', 443))\n",
                encoding="utf-8",
            )
            result = scan_source(root)
        self.assertTrue(result["imports"] or result["calls"])


if __name__ == "__main__":
    unittest.main()

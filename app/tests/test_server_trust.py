"""本地服务的信任边界。

绑 `127.0.0.1` 只保证「只有本机能连」，不保证「只有本页面能调」：
用户浏览器里任意网页都能向 `127.0.0.1:<port>` 发请求。对隐私工具来说，
这等于把本地样本与检测结果暴露给任意网页，所以三道校验各钉一条：

  Host            → 挡 DNS rebinding（攻击者域名解析到 127.0.0.1 后读响应）
  Sec-Fetch-Site  → 挡现代浏览器的跨站请求
  Origin          → 兜住不发 Sec-Fetch-* 的老浏览器

这里起真实 server（端口 0 由内核分配）跑，不 mock —— 因为白名单是按
**真实端口**写进去的，mock 掉 bind 就测不出这个行为。
"""

from __future__ import annotations

import http.client
import threading
import unittest

from app import server


class TrustBoundary(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.httpd = server.serve(port=0)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def request(self, method: str = "GET", path: str = "/", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, headers=headers or {})
            resp = conn.getresponse()
            body = resp.read()
            return resp.status, body
        finally:
            conn.close()

    def test_normal_request_passes(self):
        status, body = self.request()
        self.assertEqual(status, 200)
        self.assertIn(b"<", body)

    def test_foreign_host_is_rejected(self):
        # DNS rebinding：Host 是攻击者域名，但连接打到 127.0.0.1
        status, _ = self.request(headers={"Host": "evil.example:80"})
        self.assertEqual(status, 403)

    def test_cross_site_fetch_metadata_is_rejected(self):
        status, _ = self.request(headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)

    def test_same_site_fetch_metadata_is_rejected(self):
        # 同一站点的不同来源（如子域）对本地隐私服务也应拒绝
        status, _ = self.request(headers={"Sec-Fetch-Site": "same-site"})
        self.assertEqual(status, 403)

    def test_foreign_origin_is_rejected(self):
        status, _ = self.request(headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)

    def test_same_origin_is_allowed(self):
        status, _ = self.request(headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)

    def test_post_is_protected_too(self):
        status, _ = self.request(
            method="POST",
            path="/api/detect",
            headers={"Host": "evil.example", "Content-Length": "2"},
        )
        self.assertEqual(status, 403)

    def test_each_server_binds_its_own_whitelist(self):
        # 两个实例并存时，白名单不能互相覆盖（否则第二个实例的端口能冒充第一个）
        other = server.serve(port=0)
        try:
            other_port = other.server_address[1]
            self.assertNotEqual(other_port, self.port)
            other_cls = other.RequestHandlerClass
            self.assertIn(f"127.0.0.1:{other_port}", other_cls.allowed_hosts)
            self.assertNotIn(f"127.0.0.1:{self.port}", other_cls.allowed_hosts)
        finally:
            other.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

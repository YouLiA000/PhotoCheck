"""本地 HTTP 服务（只用标准库）。

为什么不上框架：这个程序的卖点是「拿到就能跑、不联网」。用 ``http.server``
意味着运行时只需要 Python 本身 + 三个算法库，少一层依赖就少一个装不上的理由。
"""

from __future__ import annotations

import json
import mimetypes
import re
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import service

WEB = Path(__file__).resolve().parent / "web"
MAX_UPLOAD = 40 * 1024 * 1024
PROCESS_SLOT = threading.BoundedSemaphore(1)

# 静态资源的 MIME 显式兜底：mimetypes 在精简便携解释器/部分 Windows 上可能返回 None，
# 导致 .js/.css 被当成 octet-stream，浏览器拒加载。这里按扩展名锁定最常用的几种。
MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
}

_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_IDCARD_RE = re.compile(r"(?<![0-9A-Za-z])\d{17}[\dXx](?![0-9A-Za-z])")


def _public_error(exc: Exception) -> str:
    """Return a user-safe message without raw identifiers or tracebacks."""

    text = str(exc)
    text = _PHONE_RE.sub("1**********", text)
    text = _IDCARD_RE.sub("******************", text)
    return text


def _log_exception(exc: Exception, route: str) -> None:
    """Log only the exception type; exception text may contain private input."""

    print(f"PhotoCheck request failed: {type(exc).__name__} ({route})", file=sys.stderr)


class Handler(BaseHTTPRequestHandler):
    server_version = "PhotoCheck/1.0"

    # 由 serve() 在绑定之后按真实端口写入；为空表示不做该项校验（直接构造 Handler 的单测场景）。
    allowed_hosts: frozenset[str] = frozenset()
    allowed_origins: frozenset[str] = frozenset()

    # ---- 基础工具 ----

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        # 默认实现会往 stderr 刷每条请求，演示时窗口里全是噪音
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if getattr(self, "head_only", False):
            return
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _json(self, payload: dict, status: int = 200) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_UPLOAD:
            raise ValueError("请求体过大")
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

    def _send_file(self, path: Path, content_type: str | None = None) -> None:
        if not path.is_file():
            self._error(HTTPStatus.NOT_FOUND, "文件不存在")
            return
        ctype = (content_type or MIME_TYPES.get(path.suffix.lower())
                 or mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self._send(HTTPStatus.OK, path.read_bytes(), ctype)

    # ---- 路由 ----

    def _trusted(self) -> bool:
        """本地服务的信任边界。

        绑 `127.0.0.1` 只保证「只有本机能连」，不保证「只有本页面能调」：
        用户浏览器里任意网页都能向 `127.0.0.1:<port>` 发跨站请求（简单请求不触发预检），
        配合 DNS rebinding 还能绕过同源读到响应。对「照片不出本机」的工具来说，
        这等于把本地样本与检测结果暴露给任意网页。

        三道校验各自堵一条路，缺一条就有一条绕过方式：
        Host 挡 DNS rebinding、Sec-Fetch-Site 挡现代浏览器的跨站、Origin 兜老浏览器。
        """
        host = self.headers.get("Host", "")
        if self.allowed_hosts and host not in self.allowed_hosts:
            self._error(HTTPStatus.FORBIDDEN, "Host 不在白名单内")
            return False
        if self.headers.get("Sec-Fetch-Site") in {"cross-site", "same-site"}:
            self._error(HTTPStatus.FORBIDDEN, "跨站请求被拒绝")
            return False
        origin = self.headers.get("Origin")
        if origin and self.allowed_origins and origin not in self.allowed_origins:
            self._error(HTTPStatus.FORBIDDEN, "请求来源不在白名单内")
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802
        if not self._trusted():
            return
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)
        try:
            if route in ("/", "/index.html"):
                self._send_file(WEB / "index.html", "text/html; charset=utf-8")
            elif route.startswith("/static/"):
                name = route[len("/static/"):]
                if ".." in name or name.startswith("/"):
                    self._error(HTTPStatus.BAD_REQUEST, "非法路径")
                    return
                self._send_file(WEB / name)
            elif route == "/api/samples":
                self._json({"samples": service.samples()})
            elif route == "/api/status":
                self._json(service.status())
            elif route == "/api/source-image":
                source = (query.get("source") or [""])[0]
                self._send_file(service.resolve_source(source))
            elif route == "/api/output-image":
                self._send_file(service.output_image((query.get("analysis") or [None])[0]))
            elif route == "/api/report":
                kind = (query.get("kind") or ["txt"])[0]
                if kind not in ("json", "txt"):
                    self._error(HTTPStatus.BAD_REQUEST, "kind 只能是 json 或 txt")
                    return
                path = service.report_file(kind, (query.get("analysis") or [None])[0])
                self._send_file(path, "application/json; charset=utf-8"
                                if kind == "json" else "text/plain; charset=utf-8")
            elif route == "/api/health":
                self._json({"ok": True})
            else:
                self._error(HTTPStatus.NOT_FOUND, f"未知路径 {route}")
        except FileNotFoundError as exc:
            self._error(HTTPStatus.NOT_FOUND, _public_error(exc))
        except Exception as exc:  # noqa: BLE001
            _log_exception(exc, route)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "内部错误，请重试或查看本地自检")

    def do_HEAD(self) -> None:  # noqa: N802
        # BaseHTTPRequestHandler 没实现 HEAD，不补的话 curl -I / 健康检查会得 501。
        # 复用 do_GET 的路由与状态码，只不写响应体。
        self.head_only = True
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if not self._trusted():
            return
        route = urlparse(self.path).path
        try:
            if route == "/api/upload":
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0:
                    self._error(HTTPStatus.BAD_REQUEST, "空文件")
                    return
                if length > MAX_UPLOAD:
                    self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "图片超过 40 MB")
                    return
                payload = self.rfile.read(length)
                name = self.headers.get("X-Filename") or "upload.jpg"
                source = service.save_upload(name, payload)
                self._json({"source_id": source})
                return
            body = self._read_json()
            if route == "/api/detect":
                self._json(service.detect(str(body.get("source_id") or "")))
            elif route == "/api/reveal":
                folder = service.reveal_in_explorer(body.get("analysis_id"))
                self._json({"folder": folder})
            elif route == "/api/process":
                if not PROCESS_SLOT.acquire(blocking=False):
                    self._error(
                        HTTPStatus.TOO_MANY_REQUESTS,
                        "已有图片正在处理，请等待当前任务完成",
                    )
                    return
                try:
                    result = service.process(
                        str(body.get("source_id") or ""),
                        [str(x) for x in (body.get("selected_ids") or [])],
                        str(body.get("method") or "solid"),
                        body.get("manual_boxes") or [],
                        str(body.get("analysis_id") or "") or None,
                    )
                finally:
                    PROCESS_SLOT.release()
                self._json(result)
            else:
                self._error(HTTPStatus.NOT_FOUND, f"未知接口 {route}")
        except (ValueError, FileNotFoundError, RuntimeError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, _public_error(exc))
        except Exception as exc:  # noqa: BLE001
            _log_exception(exc, route)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "内部错误，请重试或查看本地自检")


def serve(host: str = "127.0.0.1", port: int = 7861) -> ThreadingHTTPServer:
    """绑定本地端口并返回 server。

    白名单必须用**真实端口**：`port=0` 时端口由内核分配，只有 `bind` 之后才知道。
    所以这里派生一个 per-server 的 handler 子类，而不是改全局 `Handler`——
    多实例或多测试并存时，后者会互相覆盖。
    """
    httpd = ThreadingHTTPServer((host, port), Handler)
    actual_port = httpd.server_address[1]

    def _hosts_for(p: int) -> frozenset[str]:
        names = {f"127.0.0.1:{p}", f"localhost:{p}"}
        if host in {"::1", "0:0:0:0:0:0:0:1"}:
            names.add(f"[::1]:{p}")
        return frozenset(names)

    hosts = _hosts_for(actual_port)
    httpd.RequestHandlerClass = type(
        "BoundHandler",
        (Handler,),
        {
            "allowed_hosts": hosts,
            "allowed_origins": frozenset(f"http://{h}" for h in hosts),
        },
    )
    httpd.daemon_threads = True
    return httpd


def pick_port(start: int = 7861, attempts: int = 20) -> int:
    import socket

    for port in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"{start}–{start + attempts} 都被占用")


def main(argv: list[str] | None = None) -> int:
    import argparse
    import threading
    import time
    import webbrowser

    parser = argparse.ArgumentParser(description="PhotoCheck 照片隐私体检（本地运行）")
    parser.add_argument("--port", type=int, default=0, help="0 = 自动挑一个空闲端口")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args(argv)

    port = args.port or pick_port()
    try:
        service.purge_stale_work()
    except (OSError, ValueError) as exc:
        _log_exception(exc, "startup cleanup")
    httpd = serve(port=port)
    url = f"http://127.0.0.1:{port}/"

    print("=" * 56)
    print("  PhotoCheck · 照片隐私体检")
    print(f"  已启动：{url}")
    print("  全部在本地运行，不联网、不上传任何图片。")
    print("  关闭这个窗口即退出。")
    print("=" * 56)

    # Open the local endpoint immediately, then warm models in the background.
    # Detection and warm-up share the same detector lock, so a very early user
    # click waits instead of racing model initialization.
    threading.Thread(target=service.warm_up, name="photocheck-warmup", daemon=True).start()

    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

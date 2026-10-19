"""测试夹具：本地 HTTP 测试服务器 + 下载管理器辅助函数。"""
from __future__ import annotations

import gzip
import http.server
import re
import threading
import time

import pytest

from .helpers import data


class TestHTTPHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        st = self.server.state
        p = self.path.split("?", 1)[0]

        if p.startswith("/file/"):
            seg = p.rsplit("/", 1)[-1]
            n = int(re.match(r"\d+", seg).group())
            self._send_data(data(n), support_range=True)
        elif p.startswith("/norange/"):
            n = int(p.rsplit("/", 1)[-1])
            self._send_data(data(n), support_range=False)
        elif p.startswith("/slow/"):
            n = int(p.rsplit("/", 1)[-1])
            self._send_slow(data(n), support_range=True)
        elif p.startswith("/slow-norange/"):
            n = int(p.rsplit("/", 1)[-1])
            self._send_slow(data(n), support_range=False)
        elif p.startswith("/redirect/"):
            target = p[len("/redirect/"):]
            self.send_response(302)
            self.send_header("Location", "/file/" + target)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif p == "/redirect-private":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/x")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif p == "/flaky":
            st["flaky"] = st.get("flaky", 0) + 1
            if st["flaky"] <= 2:
                self.send_response(429)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send_data(data(2000), support_range=True)
        elif p == "/cd":
            self._send_data(
                data(4096), support_range=True,
                cd="attachment; filename=\"report.pdf\"; "
                   "filename*=UTF-8''%E6%8A%A5%E5%91%8A%20report.pdf",
            )
        elif p == "/hang":
            time.sleep(10)  # 由客户端超时中断
            self._send_data(data(100), support_range=True)
        elif p == "/liar":
            # 谎报 Content-Length，提前断开，测试“不完整文件不得标记成功”
            body = data(1000)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body) + 500))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            self.close_connection = True
        elif p == "/gzip":
            # 服务器强制 gzip 传输编码，测试“不得因解压导致大小误判失败”
            comp = gzip.compress(data(2000))
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(comp)))
            self.end_headers()
            self.wfile.write(comp)
        elif p == "/html":
            self._send_html()
        elif p == "/html-res":
            self._send_html_resources()
        elif p.startswith("/status/"):
            code = int(p.rsplit("/", 1)[-1])
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif p == "/auth":
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    # ---------- 内部实现 ----------

    def _send_data(self, body: bytes, support_range: bool, cd: str = ""):
        total = len(body)
        start = 0
        end = total - 1
        status = 200
        rh = self.headers.get("Range")
        if support_range and rh:
            m = re.match(r"bytes=(\d+)-(\d*)", rh)
            if m:
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else total - 1
                if start >= total or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{total}")
                    self.end_headers()
                    return
                end = min(end, total - 1)
                status = 206
        payload = body[start:end + 1]
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("ETag", '"test-etag-001"')
        self.send_header("Last-Modified", "Mon, 01 Jan 2024 00:00:00 GMT")
        if cd:
            self.send_header("Content-Disposition", cd)
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            self.server.state["range_206"] = self.server.state.get("range_206", 0) + 1
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_HEAD(self):
        p = self.path.split("?", 1)[0]
        if p.startswith("/file/"):
            seg = p.rsplit("/", 1)[-1]
            n = int(re.match(r"\d+", seg).group())
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(n))
            self.send_header("ETag", '"test-etag-001"')
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
        elif p == "/cd":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", "4096")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header(
                "Content-Disposition",
                'attachment; filename="report.pdf"; '
                "filename*=UTF-8''%E6%8A%A5%E5%91%8A%20report.pdf",
            )
            self.end_headers()
        elif p == "/json":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "100")
            self.end_headers()
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def _send_slow(self, body: bytes, support_range: bool):
        total = len(body)
        start = 0
        end = total - 1
        status = 200
        rh = self.headers.get("Range")
        if support_range and rh:
            m = re.match(r"bytes=(\d+)-(\d*)", rh)
            if m:
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else total - 1
                if start >= total or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{total}")
                    self.end_headers()
                    return
                end = min(end, total - 1)
                status = 206
        payload = body[start:end + 1]
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("ETag", '"slow-etag"')
        self.send_header("Last-Modified", "Mon, 01 Jan 2024 00:00:00 GMT")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            self.server.state["range_206"] = self.server.state.get("range_206", 0) + 1
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        for i in range(0, len(payload), 2048):
            self.wfile.write(payload[i:i + 2048])
            self.wfile.flush()
            time.sleep(0.03)

    def _send_html(self):
        html = f"""
        <html><body>
        <a href="/file/1000.pdf">document.pdf</a>
        <a href="/file/2000.jpg">photo.jpg</a>
        <a href="/html">another page</a>
        <a href="/file/3000" download>download-attr.bin</a>
        <a href="https://example.com/external.zip">external zip</a>
        </body></html>
        """
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html_resources(self):
        html = f"""
        <html><body>
        <a href="/file/1000.pdf">doc.pdf</a>
        <a href="/file/2000.jpg">img.jpg</a>
        <a href="/file/3000.xlsx">sheet.xlsx</a>
        <a href="/cd" download>cd-file</a>
        <a href="/file/4000.zip">archive.zip</a>
        </body></html>
        """
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Server:
    def __init__(self, url: str, state: dict, httpd, thread):
        self.url = url
        self.state = state
        self._httpd = httpd
        self._thread = thread


@pytest.fixture()
def server():
    state: dict = {}
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), TestHTTPHandler)
    httpd.state = state
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    srv = _Server(f"http://127.0.0.1:{httpd.server_port}", state, httpd, thread)
    yield srv
    httpd.shutdown()
    thread.join(timeout=2)

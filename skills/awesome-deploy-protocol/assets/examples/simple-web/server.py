import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/healthz":
            payload, kind = b"ok", "text/plain"
        elif path == "/abc":
            payload, kind = json.dumps({"message": "Hello from Awesome API"}).encode(), "application/json"
        elif path == "/":
            payload = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>Awesome demo</title><h1>部署成功</h1><p>这个页面由仓库代码构建并通过 HTTPS 发布。</p><a href="/abc">查看同域名 API</a></html>'.encode()
            kind = "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), Handler).serve_forever()

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/healthz":
            payload, kind = b"ok", "text/plain"
        elif os.getenv("APP_MODE") == "api":
            payload = json.dumps(
                {"service": "api", "pathReceived": path, "message": "Separate API service"}
            ).encode()
            kind = "application/json"
        else:
            payload = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>Awesome services</title><h1>前端服务正常</h1><p><a href="/abc">访问独立 API 服务</a></p></html>'.encode()
            kind = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), Handler).serve_forever()

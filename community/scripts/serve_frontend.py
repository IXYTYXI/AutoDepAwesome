"""Local same-origin gateway. All static and API authorization is owned by FastAPI."""
import http.client
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.core.config import Settings

HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"}


class FrontendHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, settings, **kwargs):
        self.settings = settings
        super().__init__(*args, **kwargs)

    def proxy(self):
        if self.headers.get("Transfer-Encoding"):
            self.send_error(400)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400)
            return
        if length < 0 or length > self.settings.max_body_bytes:
            self.send_error(413)
            return
        body = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS}
        upstream = http.client.HTTPConnection("127.0.0.1", self.settings.port, timeout=90)
        try:
            upstream.request(self.command, self.path, body=body, headers=headers)
            result = upstream.getresponse()
            data = result.read()
            self.send_response(result.status)
            for k, v in result.getheaders():
                if k.lower() not in HOP_HEADERS | {"server", "date", "content-length"}:
                    self.send_header(k, v)
            if self.command != "HEAD":
                self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        except (OSError, http.client.HTTPException):
            self.send_error(502, "Backend unavailable")
        finally:
            upstream.close()

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = proxy

    def log_message(self, *_):
        pass  # OAuth query strings and credentials must not appear in access logs.


def main():
    settings = Settings()
    if settings.environment == "production":
        raise SystemExit("前端开发服务器不可用于生产")
    with ThreadingHTTPServer((settings.host, settings.frontend_port), partial(FrontendHandler, settings=settings)) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()

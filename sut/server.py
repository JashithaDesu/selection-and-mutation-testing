"""HTTP API for the gateway (stdlib only).

Run:  python -m sut.server --port 8080
The process prints "LISTENING <port>" once ready so tests can use port 0.
"""

import argparse
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sut.ratelimit import RateLimiter
from sut.registry import Conflict, NotFound, Registry, ValidationError, spec_from_dict
from sut.router import Router

MAX_BODY_BYTES = 64 * 1024
_FN_PATH = re.compile(r"^/functions/([^/]+)$")
_INVOKE_PATH = re.compile(r"^/invoke/([^/]+)$")


class App:
    def __init__(self, rate_per_s=50, burst=100):
        self.registry = Registry()
        self.router = Router(self.registry, limiter=RateLimiter(rate_per_s, burst))
        self.lock = threading.Lock()

    def handle(self, method, path, body):
        """Return (status, json_body_or_None, extra_headers)."""
        with self.lock:
            if path == "/healthz" and method == "GET":
                return 200, {"status": "ok"}, {}
            if path == "/functions":
                if method == "GET":
                    return 200, {"functions": self.registry.names()}, {}
                if method == "POST":
                    spec = spec_from_dict(body)
                    version = self.registry.create(spec)
                    return 201, {"name": spec.name, "version": version}, {}
                return 405, {"error": "method not allowed"}, {}
            m = _FN_PATH.match(path)
            if m:
                name = m.group(1)
                if method == "GET":
                    spec, version = self.registry.get(name)
                    return 200, {"spec": spec.to_dict(), "version": version}, {}
                if method == "PUT":
                    if isinstance(body, dict) and body.get("name", name) != name:
                        return 400, {"error": "name in body does not match path"}, {}
                    data = dict(body or {}, name=name) if isinstance(body, dict) else body
                    version = self.registry.update(spec_from_dict(data))
                    return 200, {"name": name, "version": version}, {}
                if method == "DELETE":
                    self.registry.delete(name)
                    self.router.remove(name)
                    return 204, None, {}
                return 405, {"error": "method not allowed"}, {}
            m = _INVOKE_PATH.match(path)
            if m and method == "POST":
                res = self.router.invoke(m.group(1), body)
                headers = {"X-Replica": res.replica or "", "X-Cold-Start": str(res.cold_start).lower()}
                if res.status == 429:
                    headers["Retry-After"] = str(max(1, int(res.retry_after + 0.999)))
                return res.status, res.body, headers
            return 404, {"error": "not found"}, {}


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # keep test output quiet
            pass

        def _send(self, status, payload, headers=None):
            data = b"" if payload is None else json.dumps(payload).encode()
            self.send_response(status)
            if payload is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self, method):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                if length <= 16 * MAX_BODY_BYTES:
                    self.rfile.read(length)  # drain so the client sees the response, not a reset
                self.close_connection = True
                return self._send(413, {"error": "payload too large"})
            raw = self.rfile.read(length) if length else b""
            body = None
            if raw:
                try:
                    body = json.loads(raw)
                except ValueError:
                    return self._send(400, {"error": "invalid JSON"})
            try:
                status, payload, headers = app.handle(method, self.path, body)
            except ValidationError as exc:
                status, payload, headers = 400, {"error": "invalid spec", "details": exc.errors}, {}
            except NotFound:
                status, payload, headers = 404, {"error": "not found"}, {}
            except Conflict:
                status, payload, headers = 409, {"error": "already exists"}, {}
            self._send(status, payload, headers)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_DELETE(self):
            self._dispatch("DELETE")

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description="mini FaaS gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--rate", type=float, default=50.0)
    parser.add_argument("--burst", type=int, default=100)
    args = parser.parse_args(argv)
    app = App(rate_per_s=args.rate, burst=args.burst)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    print(f"LISTENING {server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

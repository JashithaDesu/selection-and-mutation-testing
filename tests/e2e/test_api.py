"""End-to-end stage: start the real server as a separate process and talk to
it over HTTP only (black box). Slowest stage by design: process start-up,
sockets, JSON serialisation, real wall-clock time."""

import json
import os
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


STARTUP_TIMEOUT_S = 3  # normal start-up is ~0.1 s


class ServerProcess:
    def __init__(self, *args):
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        # Run the server the way it runs in production: with normal (buffered)
        # stdout. If a developer has PYTHONUNBUFFERED set, a missing flush()
        # would otherwise go unnoticed on their machine only.
        env.pop("PYTHONUNBUFFERED", None)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "sut.server", "--port", "0", *args],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        line = self._read_startup_line()
        if not line.startswith("LISTENING "):
            self.stop()
            raise RuntimeError(f"server failed to start: {line!r}")
        self.base = f"http://127.0.0.1:{int(line.split()[1])}"

    def _read_startup_line(self):
        """readline() with a deadline: a server that never announces itself must
        fail this test quickly, not hang the whole suite (works on Windows too)."""
        result = []
        reader = threading.Thread(target=lambda: result.append(self.proc.stdout.readline()), daemon=True)
        reader.start()
        reader.join(STARTUP_TIMEOUT_S)
        return result[0].strip() if result else "<no output within %ss>" % STARTUP_TIMEOUT_S

    def request(self, method, path, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                payload = resp.read()
                return resp.status, (json.loads(payload) if payload else None), dict(resp.headers)
        except urllib.error.HTTPError as err:
            with err:  # close the error response; Python 3.14 warns about leaked ones
                payload = err.read()
                return err.code, (json.loads(payload) if payload else None), dict(err.headers)

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc.stdout.close()


SPEC = {"name": "shout", "runtime": "python3.12", "handler": "uppercase"}


class TestFunctionLifecycleE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ServerProcess()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def test_01_health(self):
        self.assertEqual(self.srv.request("GET", "/healthz")[:2], (200, {"status": "ok"}))

    def test_02_deploy_invoke_update_delete(self):
        status, body, _ = self.srv.request("POST", "/functions", SPEC)
        self.assertEqual((status, body), (201, {"name": "shout", "version": 1}))

        status, body, headers = self.srv.request("POST", "/invoke/shout", {"text": "hej"})
        self.assertEqual((status, body), (200, {"result": {"text": "HEJ"}}))
        self.assertEqual(headers["X-Cold-Start"], "true")
        self.assertEqual(self.srv.request("POST", "/invoke/shout", {"text": "a"})[2]["X-Cold-Start"], "false")

        status, body, _ = self.srv.request("PUT", "/functions/shout", {"runtime": "node20", "handler": "wordcount"})
        self.assertEqual(body["version"], 2)
        status, body, _ = self.srv.request("POST", "/invoke/shout", {"text": "b a b"})
        self.assertEqual(body["result"]["top"][0], ["b", 2])

        self.assertEqual(self.srv.request("DELETE", "/functions/shout")[0], 204)
        self.assertEqual(self.srv.request("POST", "/invoke/shout", {"text": "x"})[0], 404)

    def test_03_error_mapping(self):
        self.assertEqual(self.srv.request("POST", "/functions", raw=b"{nope")[0], 400)
        status, body, _ = self.srv.request("POST", "/functions", dict(SPEC, name="x", memory_mb=7))
        self.assertEqual(status, 400)
        self.assertGreaterEqual(len(body["details"]), 2)
        self.srv.request("POST", "/functions", dict(SPEC, name="dup"))
        self.assertEqual(self.srv.request("POST", "/functions", dict(SPEC, name="dup"))[0], 409)
        self.assertEqual(self.srv.request("GET", "/functions/ghost")[0], 404)
        self.assertEqual(self.srv.request("POST", "/invoke/dup", {"text": 1})[0], 400)

    def test_04_payload_too_large(self):
        big = json.dumps({"text": "x" * (70 * 1024)}).encode()
        self.assertEqual(self.srv.request("POST", "/invoke/dup", raw=big)[0], 413)

    def test_05_handler_crash_is_isolated(self):
        self.srv.request("POST", "/functions", dict(SPEC, name="crash", handler="fail"))
        self.assertEqual(self.srv.request("POST", "/invoke/crash", {})[0], 500)
        self.assertEqual(self.srv.request("GET", "/healthz")[0], 200)


class TestRateLimitE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ServerProcess("--rate", "4", "--burst", "2")
        cls.srv.request("POST", "/functions", dict(SPEC, handler="echo"))

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def test_429_with_retry_after_then_recovers(self):
        statuses = [self.srv.request("POST", "/invoke/shout", {})[0] for _ in range(3)]
        self.assertEqual(statuses, [200, 200, 429])
        _, _, headers = self.srv.request("POST", "/invoke/shout", {})
        self.assertEqual(headers["Retry-After"], "1")
        time.sleep(0.3)  # rate 4/s -> one token every 0.25s
        self.assertEqual(self.srv.request("POST", "/invoke/shout", {})[0], 200)


if __name__ == "__main__":
    unittest.main()

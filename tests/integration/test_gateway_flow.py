"""Integration stage: real Registry + Router + RateLimiter + Autoscaler wired
together, driven through the in-process App (no network). A fake clock makes
time-dependent behaviour (idle scale-to-zero, refill) deterministic."""

import unittest

from sut.autoscaler import Autoscaler
from sut.ratelimit import RateLimiter
from sut.registry import Registry, ValidationError
from sut.router import Overloaded, Router
from sut.server import App
from tests.support import FakeClock, spec


def wired(clock, rate=100, burst=100, idle=60):
    reg = Registry()
    router = Router(reg, limiter=RateLimiter(rate, burst, clock),
                    autoscaler=Autoscaler(idle_timeout_s=idle), clock=clock)
    return reg, router


class TestScalingLifecycle(unittest.TestCase):
    def test_cold_warm_idle_cold(self):
        clock = FakeClock()
        reg, router = wired(clock, idle=60)
        reg.create(spec(handler="echo"))

        first = router.invoke("hello", {"n": 1})
        second = router.invoke("hello", {"n": 2})
        self.assertTrue(first.cold_start)
        self.assertFalse(second.cold_start)
        self.assertEqual(first.replica, second.replica)

        clock.advance(30); router.tick()
        self.assertEqual(len(router.replicas("hello")), 1, "still warm before idle timeout")

        clock.advance(31); router.tick()
        self.assertEqual(router.replicas("hello"), [], "scaled to zero after idle timeout")
        self.assertTrue(router.invoke("hello", {}).cold_start)

    def test_concurrent_load_scales_out_and_back(self):
        clock = FakeClock()
        reg, router = wired(clock)
        reg.create(spec(target_concurrency=2, max_replicas=4))
        leases = [router.acquire("hello") for _ in range(8)]
        self.assertEqual(len(router.replicas("hello")), 4)
        self.assertEqual([r.in_flight for r in router.replicas("hello")], [2, 2, 2, 2])
        with self.assertRaises(Overloaded):
            router.acquire("hello")  # 4 replicas x 2 slots, all taken, max_replicas reached
        for lease in leases:
            router.release(lease)
        clock.advance(1); router.tick()
        self.assertEqual(len(router.replicas("hello")), 4, "warm, recently used")
        clock.advance(120); router.tick()
        self.assertEqual(len(router.replicas("hello")), 0)

    def test_min_replicas_survive_idle(self):
        clock = FakeClock()
        reg, router = wired(clock)
        reg.create(spec(min_replicas=1))
        router.invoke("hello", {})
        clock.advance(1000); router.tick()
        self.assertEqual(len(router.replicas("hello")), 1)

    def test_deleted_function_pool_is_reaped(self):
        clock = FakeClock()
        reg, router = wired(clock)
        reg.create(spec())
        router.invoke("hello", {})
        reg.delete("hello")
        router.tick()
        self.assertEqual(router.replicas("hello"), [])


class TestRateLimitIntegration(unittest.TestCase):
    def test_burst_then_refill(self):
        clock = FakeClock()
        reg, router = wired(clock, rate=2, burst=3)
        reg.create(spec())
        statuses = [router.invoke("hello", {}).status for _ in range(5)]
        self.assertEqual(statuses, [200, 200, 200, 429, 429])
        limited = router.invoke("hello", {})
        self.assertAlmostEqual(limited.retry_after, 0.5)
        clock.advance(0.5)
        self.assertEqual(router.invoke("hello", {}).status, 200)

    def test_limits_are_per_function(self):
        clock = FakeClock()
        reg, router = wired(clock, rate=1, burst=1)
        reg.create(spec(name="one"))
        reg.create(spec(name="two"))
        self.assertEqual(router.invoke("one", {}).status, 200)
        self.assertEqual(router.invoke("one", {}).status, 429)
        self.assertEqual(router.invoke("two", {}).status, 200)


class TestAppHandle(unittest.TestCase):
    """The App layer without HTTP: routing table and error mapping."""

    def setUp(self):
        self.app = App()

    def test_create_get_update_delete(self):
        status, body, _ = self.app.handle("POST", "/functions", spec().to_dict())
        self.assertEqual((status, body["version"]), (201, 1))
        status, body, _ = self.app.handle("PUT", "/functions/hello", {"runtime": "go1.22", "handler": "echo"})
        self.assertEqual((status, body["version"]), (200, 2))
        status, body, _ = self.app.handle("GET", "/functions/hello", None)
        self.assertEqual(body["spec"]["runtime"], "go1.22")
        self.assertEqual(self.app.handle("DELETE", "/functions/hello", None)[0], 204)
        self.assertEqual(self.app.handle("GET", "/functions", None)[1], {"functions": []})

    def test_put_name_mismatch(self):
        self.app.handle("POST", "/functions", spec().to_dict())
        status, _, _ = self.app.handle("PUT", "/functions/hello", {"name": "other", "runtime": "go1.22", "handler": "echo"})
        self.assertEqual(status, 400)

    def test_invalid_spec_raises_for_http_layer(self):
        with self.assertRaises(ValidationError):
            self.app.handle("POST", "/functions", dict(spec().to_dict(), memory_mb=3))

    def test_unknown_route_and_method(self):
        self.assertEqual(self.app.handle("GET", "/nope", None)[0], 404)
        self.assertEqual(self.app.handle("PATCH", "/functions", None)[0], 405)

    def test_invoke_headers(self):
        self.app.handle("POST", "/functions", spec().to_dict())
        status, body, headers = self.app.handle("POST", "/invoke/hello", {"x": 1})
        self.assertEqual(status, 200)
        self.assertEqual(body, {"result": {"x": 1}})
        self.assertEqual(headers["X-Cold-Start"], "true")


if __name__ == "__main__":
    unittest.main()

import unittest

from sut.ratelimit import RateLimiter
from sut.registry import NotFound, Registry
from sut.router import Overloaded, RateLimited, Replica, Router
from tests.support import FakeClock, spec


class TestPick(unittest.TestCase):
    def test_least_loaded_then_least_served(self):
        pool = [Replica("a", in_flight=2), Replica("b", in_flight=1, served=9), Replica("c", in_flight=1, served=3)]
        self.assertEqual(Router._pick(pool).id, "c")


class TestAcquireRelease(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.reg = Registry()
        self.router = Router(self.reg, limiter=RateLimiter(100, 100, self.clock), clock=self.clock)

    def test_unknown_function(self):
        with self.assertRaises(NotFound):
            self.router.acquire("ghost")

    def test_first_acquire_is_cold(self):
        self.reg.create(spec())
        lease = self.router.acquire("hello")
        self.assertTrue(lease.cold_start)
        self.router.release(lease)
        self.assertFalse(self.router.acquire("hello").cold_start)

    def test_release_twice_fails(self):
        self.reg.create(spec())
        lease = self.router.acquire("hello")
        self.router.release(lease)
        with self.assertRaises(ValueError):
            self.router.release(lease)

    def test_overloaded_when_at_max(self):
        self.reg.create(spec(max_replicas=1, target_concurrency=2))
        self.router.acquire("hello")
        self.router.acquire("hello")
        with self.assertRaises(Overloaded):
            self.router.acquire("hello")

    def test_rate_limited(self):
        router = Router(self.reg, limiter=RateLimiter(1, 1, self.clock), clock=self.clock)
        self.reg.create(spec())
        router.release(router.acquire("hello"))
        with self.assertRaises(RateLimited) as ctx:
            router.acquire("hello")
        self.assertGreater(ctx.exception.retry_after, 0)


class TestInvoke(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.reg = Registry()
        self.router = Router(self.reg, clock=self.clock)

    def test_ok(self):
        self.reg.create(spec(handler="uppercase"))
        res = self.router.invoke("hello", {"text": "x"})
        self.assertEqual((res.status, res.body), (200, {"result": {"text": "X"}}))

    def test_status_codes(self):
        self.reg.create(spec(name="bad", handler="uppercase"))
        self.reg.create(spec(name="boom", handler="fail"))
        self.assertEqual(self.router.invoke("nope", {}).status, 404)
        self.assertEqual(self.router.invoke("bad", {}).status, 400)
        self.assertEqual(self.router.invoke("boom", {}).status, 500)

    def test_saturated_function_returns_503(self):
        """All replicas busy at max scale -> invoke() answers 503 instead of queueing."""
        self.reg.create(spec(max_replicas=1, target_concurrency=1))
        busy = self.router.acquire("hello")          # occupy the only slot
        res = self.router.invoke("hello", {})
        self.assertEqual(res.status, 503)
        self.assertEqual(res.body, {"error": "all replicas saturated"})
        self.router.release(busy)
        self.assertEqual(self.router.invoke("hello", {}).status, 200, "slot free again")

    def test_in_flight_returns_to_zero(self):
        self.reg.create(spec(handler="fail"))
        self.router.invoke("hello", {})
        self.assertEqual(self.router.in_flight("hello"), 0)


if __name__ == "__main__":
    unittest.main()

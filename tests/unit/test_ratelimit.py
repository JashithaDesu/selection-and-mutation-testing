import unittest

from sut.ratelimit import RateLimiter, TokenBucket
from tests.support import PROPERTY_CASES, FakeClock, rng


class TestTokenBucket(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()

    def test_starts_full_and_drains(self):
        b = TokenBucket(rate_per_s=1, burst=3, clock=self.clock)
        self.assertEqual([b.allow() for _ in range(4)], [True, True, True, False])

    def test_refills_over_time(self):
        b = TokenBucket(rate_per_s=2, burst=2, clock=self.clock)
        b.allow(); b.allow()
        self.assertFalse(b.allow())
        self.clock.advance(0.5)
        self.assertTrue(b.allow())
        self.assertFalse(b.allow())

    def test_never_exceeds_burst(self):
        b = TokenBucket(rate_per_s=10, burst=5, clock=self.clock)
        self.clock.advance(3600)
        self.assertEqual(b.tokens(), 5)

    def test_retry_after(self):
        b = TokenBucket(rate_per_s=4, burst=1, clock=self.clock)
        self.assertEqual(b.retry_after(), 0.0)
        b.allow()
        self.assertAlmostEqual(b.retry_after(), 0.25)

    def test_cost_larger_than_burst_is_rejected(self):
        b = TokenBucket(rate_per_s=1, burst=2, clock=self.clock)
        self.assertFalse(b.allow(cost=3))
        self.assertEqual(b.tokens(), 2)

    def test_invalid_parameters(self):
        with self.assertRaises(ValueError):
            TokenBucket(rate_per_s=0, burst=1)
        with self.assertRaises(ValueError):
            TokenBucket(rate_per_s=1, burst=0)

    def test_property_admitted_requests_bounded(self):
        """Property: over any window of T seconds at most burst + rate*T requests pass."""
        r = rng()
        for _ in range(PROPERTY_CASES // 4):
            rate, burst = r.randint(1, 20), r.randint(1, 20)
            clock = FakeClock()
            b = TokenBucket(rate, burst, clock)
            admitted, elapsed = 0, 0.0
            for _ in range(r.randint(1, 60)):
                dt = r.random() * 0.3
                clock.advance(dt)
                elapsed += dt
                admitted += b.allow()
            self.assertLessEqual(admitted, burst + rate * elapsed + 1e-9)


class TestRateLimiter(unittest.TestCase):
    def test_buckets_are_independent(self):
        clock = FakeClock()
        lim = RateLimiter(rate_per_s=1, burst=1, clock=clock)
        self.assertTrue(lim.allow("a"))
        self.assertFalse(lim.allow("a"))
        self.assertTrue(lim.allow("b"))

    def test_forget_resets_bucket(self):
        clock = FakeClock()
        lim = RateLimiter(rate_per_s=1, burst=1, clock=clock)
        lim.allow("a")
        lim.forget("a")
        self.assertTrue(lim.allow("a"))


if __name__ == "__main__":
    unittest.main()

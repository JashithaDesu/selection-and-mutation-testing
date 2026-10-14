"""Token-bucket rate limiting, one bucket per function."""

import time


class TokenBucket:
    def __init__(self, rate_per_s, burst, clock=time.monotonic):
        if rate_per_s <= 0:
            raise ValueError("rate_per_s must be > 0")
        if burst < 1:
            raise ValueError("burst must be >= 1")
        self.rate = float(rate_per_s)
        self.burst = float(burst)
        self._clock = clock
        self._tokens = float(burst)
        self._last = clock()

    def _refill(self):
        now = self._clock()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self.burst, self._tokens + elapsed * self.rate)
            self._last = now

    def tokens(self):
        self._refill()
        return self._tokens

    def allow(self, cost=1.0):
        if cost > self.burst:
            return False
        self._refill()
        if self._tokens >= cost:
            self._tokens -= cost
            return True
        return False

    def retry_after(self, cost=1.0):
        """Seconds until `cost` tokens are available (0 if available now)."""
        self._refill()
        missing = cost - self._tokens
        if missing <= 0:
            return 0.0
        return missing / self.rate


class RateLimiter:
    def __init__(self, rate_per_s, burst, clock=time.monotonic):
        self.rate_per_s = rate_per_s
        self.burst = burst
        self._clock = clock
        self._buckets = {}

    def bucket(self, key):
        if key not in self._buckets:
            self._buckets[key] = TokenBucket(self.rate_per_s, self.burst, self._clock)
        return self._buckets[key]

    def allow(self, key):
        return self.bucket(key).allow()

    def retry_after(self, key):
        return self.bucket(key).retry_after()

    def forget(self, key):
        self._buckets.pop(key, None)

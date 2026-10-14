"""Request routing: rate limiting, cold starts, replica selection, execution."""

import itertools
import time
from dataclasses import dataclass, field

from sut.autoscaler import Autoscaler
from sut.handlers import HANDLERS, HandlerError
from sut.ratelimit import RateLimiter
from sut.registry import NotFound


@dataclass
class Replica:
    id: str
    in_flight: int = 0
    served: int = 0


@dataclass
class InvokeResult:
    status: int
    body: dict
    replica: str = None
    cold_start: bool = False
    retry_after: float = 0.0


@dataclass
class Lease:
    name: str
    replica: Replica
    cold_start: bool
    released: bool = field(default=False)


class Overloaded(Exception):
    pass


class RateLimited(Exception):
    def __init__(self, retry_after):
        super().__init__(f"retry after {retry_after:.3f}s")
        self.retry_after = retry_after


class Router:
    def __init__(self, registry, limiter=None, autoscaler=None, clock=time.monotonic):
        self.registry = registry
        self.clock = clock
        self.limiter = limiter or RateLimiter(rate_per_s=50, burst=100, clock=clock)
        self.autoscaler = autoscaler or Autoscaler()
        self._pools = {}
        self._ids = itertools.count(1)

    # -- pool management -------------------------------------------------
    def replicas(self, name):
        return list(self._pools.get(name, []))

    def in_flight(self, name):
        return sum(r.in_flight for r in self._pools.get(name, []))

    def _resize(self, name, target):
        pool = self._pools.setdefault(name, [])
        while len(pool) < target:
            pool.append(Replica(id=f"{name}-{next(self._ids)}"))
        # Only remove idle replicas, newest first, never interrupt in-flight work.
        idx = len(pool) - 1
        while len(pool) > target and idx >= 0:
            if pool[idx].in_flight == 0:
                pool.pop(idx)
            idx -= 1

    def tick(self):
        """Periodic reconcile loop: apply autoscaler decisions to every pool."""
        now = self.clock()
        for name in list(self._pools):
            try:
                spec, _ = self.registry.get(name)
            except NotFound:
                self._pools.pop(name, None)
                self.autoscaler.forget(name)
                continue
            target = self.autoscaler.step(name, spec, self.in_flight(name), now)
            self._resize(name, target)

    def remove(self, name):
        self._pools.pop(name, None)
        self.autoscaler.forget(name)
        self.limiter.forget(name)

    # -- request path ----------------------------------------------------
    @staticmethod
    def _pick(pool):
        return min(pool, key=lambda r: (r.in_flight, r.served, r.id))

    def acquire(self, name):
        spec, _ = self.registry.get(name)  # raises NotFound
        if not self.limiter.allow(name):
            raise RateLimited(self.limiter.retry_after(name))
        now = self.clock()
        pool = self._pools.setdefault(name, [])
        cold = len(pool) == 0
        target = self.autoscaler.step(name, spec, self.in_flight(name) + 1, now)
        if target > len(pool):
            self._resize(name, target)
        replica = self._pick(pool)
        if replica.in_flight >= spec.target_concurrency:
            raise Overloaded(name)
        replica.in_flight += 1
        self.autoscaler.record_request(name, now)
        return Lease(name=name, replica=replica, cold_start=cold)

    def release(self, lease):
        if lease.released:
            raise ValueError("lease already released")
        lease.released = True
        lease.replica.in_flight -= 1
        lease.replica.served += 1

    def invoke(self, name, payload):
        try:
            lease = self.acquire(name)
        except NotFound:
            return InvokeResult(404, {"error": f"function '{name}' not found"})
        except RateLimited as exc:
            return InvokeResult(429, {"error": "rate limited"}, retry_after=exc.retry_after)
        except Overloaded:
            return InvokeResult(503, {"error": "all replicas saturated"})
        spec, _ = self.registry.get(name)
        try:
            result = HANDLERS[spec.handler](payload)
            status, body = 200, {"result": result}
        except HandlerError as exc:
            status, body = 400, {"error": str(exc)}
        except Exception:  # handler crash must not take the gateway down
            status, body = 500, {"error": "function failed"}
        finally:
            self.release(lease)
        return InvokeResult(status, body, replica=lease.replica.id, cold_start=lease.cold_start)

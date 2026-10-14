"""Replica-count decisions: concurrency-based scale-up, gradual scale-down,
scale-to-zero after an idle period (the Knative-style model Simple FaaS uses)."""

import math
from dataclasses import dataclass

DEFAULT_IDLE_TIMEOUT_S = 60.0


@dataclass
class ScaleState:
    replicas: int = 0
    last_request_at: float = None


def needed_replicas(in_flight, target_concurrency):
    if in_flight <= 0:
        return 0
    return math.ceil(in_flight / target_concurrency)


def desired_replicas(spec, current, in_flight, now, last_request_at,
                     idle_timeout_s=DEFAULT_IDLE_TIMEOUT_S):
    """Pure decision function: how many replicas should exist right now."""
    if in_flight > 0:
        desired = max(needed_replicas(in_flight, spec.target_concurrency), 1)
    else:
        idle = last_request_at is None or now - last_request_at >= idle_timeout_s
        desired = 0 if idle else current
    desired = max(desired, spec.min_replicas)
    desired = min(desired, spec.max_replicas)

    # Scale down gradually (at most half the replicas per step) to avoid
    # thrashing, except when going idle -> zero which is allowed in one step.
    if desired < current and desired > 0:
        step = max(1, current // 2)
        desired = max(desired, current - step)
    return desired


class Autoscaler:
    def __init__(self, idle_timeout_s=DEFAULT_IDLE_TIMEOUT_S):
        self.idle_timeout_s = idle_timeout_s
        self._state = {}

    def state(self, name):
        return self._state.setdefault(name, ScaleState())

    def record_request(self, name, now):
        self.state(name).last_request_at = now

    def step(self, name, spec, in_flight, now):
        st = self.state(name)
        st.replicas = desired_replicas(spec, st.replicas, in_flight, now,
                                       st.last_request_at, self.idle_timeout_s)
        return st.replicas

    def forget(self, name):
        self._state.pop(name, None)

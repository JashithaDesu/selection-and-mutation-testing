"""Shared test helpers (no production code imports this)."""

import random

from sut.registry import FunctionSpec

# Fixed seed: randomized "property" tests must be reproducible, otherwise a
# mutant could be killed in one run and survive in the next (flaky kill matrix).
PROPERTY_SEED = 20261014
PROPERTY_CASES = 200


def rng():
    return random.Random(PROPERTY_SEED)


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def spec(**overrides):
    base = dict(name="hello", runtime="python3.12", handler="echo")
    base.update(overrides)
    return FunctionSpec(**base)

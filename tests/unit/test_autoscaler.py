import unittest

from sut.autoscaler import Autoscaler, desired_replicas, needed_replicas
from tests.support import PROPERTY_CASES, rng, spec


class TestNeededReplicas(unittest.TestCase):
    def test_rounds_up(self):
        self.assertEqual(needed_replicas(0, 10), 0)
        self.assertEqual(needed_replicas(1, 10), 1)
        self.assertEqual(needed_replicas(10, 10), 1)
        self.assertEqual(needed_replicas(11, 10), 2)


class TestDesiredReplicas(unittest.TestCase):
    def test_scale_from_zero_on_first_request(self):
        s = spec(target_concurrency=5)
        self.assertEqual(desired_replicas(s, current=0, in_flight=1, now=0, last_request_at=None), 1)

    def test_scale_up_by_concurrency(self):
        s = spec(target_concurrency=5, max_replicas=10)
        self.assertEqual(desired_replicas(s, 1, in_flight=23, now=0, last_request_at=0), 5)

    def test_capped_at_max(self):
        s = spec(target_concurrency=1, max_replicas=3)
        self.assertEqual(desired_replicas(s, 1, in_flight=50, now=0, last_request_at=0), 3)

    def test_keeps_warm_while_recently_used(self):
        s = spec()
        self.assertEqual(desired_replicas(s, 2, in_flight=0, now=30, last_request_at=0), 2)

    def test_scales_to_zero_after_idle_timeout(self):
        s = spec()
        self.assertEqual(desired_replicas(s, 4, in_flight=0, now=60, last_request_at=0), 0)

    def test_respects_min_replicas_when_idle(self):
        s = spec(min_replicas=2)
        self.assertEqual(desired_replicas(s, 4, in_flight=0, now=500, last_request_at=0), 2)

    def test_scale_down_is_gradual(self):
        s = spec(target_concurrency=10, max_replicas=10)
        # 8 replicas, load now needs only 1: may drop at most half per step.
        self.assertEqual(desired_replicas(s, 8, in_flight=3, now=0, last_request_at=0), 4)

    def test_property_within_bounds(self):
        r = rng()
        for _ in range(PROPERTY_CASES):
            mn = r.randint(0, 3)
            mx = r.randint(max(1, mn), 10)
            s = spec(min_replicas=mn, max_replicas=mx, target_concurrency=r.randint(1, 8))
            d = desired_replicas(s, r.randint(0, mx), r.randint(0, 100), now=r.uniform(0, 200),
                                 last_request_at=r.choice([None, 0.0, 100.0]))
            self.assertGreaterEqual(d, mn)
            self.assertLessEqual(d, mx)


class TestAutoscaler(unittest.TestCase):
    def test_step_tracks_state(self):
        a = Autoscaler(idle_timeout_s=10)
        s = spec(target_concurrency=2)
        self.assertEqual(a.step("f", s, in_flight=3, now=0), 2)
        a.record_request("f", 0)
        self.assertEqual(a.step("f", s, in_flight=0, now=5), 2)
        self.assertEqual(a.step("f", s, in_flight=0, now=10), 0)

    def test_forget(self):
        a = Autoscaler()
        a.step("f", spec(), in_flight=1, now=0)
        a.forget("f")
        self.assertEqual(a.state("f").replicas, 0)


if __name__ == "__main__":
    unittest.main()

import unittest

from testchain import stats


class TestStats(unittest.TestCase):
    def test_wilson_known_value(self):
        lo, hi = stats.wilson(8, 10)
        self.assertAlmostEqual(lo, 0.4902, places=3)
        self.assertAlmostEqual(hi, 0.9433, places=3)

    def test_wilson_extremes_stay_in_range(self):
        self.assertAlmostEqual(stats.wilson(10, 10)[1], 1.0)
        self.assertAlmostEqual(stats.wilson(0, 10)[0], 0.0)

    def test_mcnemar(self):
        self.assertEqual(stats.mcnemar_exact(0, 0), 1.0)
        self.assertAlmostEqual(stats.mcnemar_exact(0, 6), 2 / 64)   # exact binomial
        self.assertAlmostEqual(stats.mcnemar_exact(3, 3), 1.0)
        self.assertEqual(stats.mcnemar_exact(2, 9), stats.mcnemar_exact(9, 2))

    def test_bootstrap_contains_mean_and_is_deterministic(self):
        vals = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        lo, hi = stats.bootstrap_mean_ci(vals, iters=2000)
        self.assertLess(lo, 5.5)
        self.assertGreater(hi, 5.5)
        self.assertEqual((lo, hi), stats.bootstrap_mean_ci(vals, iters=2000))

    def test_holm(self):
        self.assertEqual(stats.holm([0.01, 0.04, 0.03]), [0.03, 0.06, 0.06])


if __name__ == "__main__":
    unittest.main()

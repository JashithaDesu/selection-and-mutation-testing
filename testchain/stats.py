"""Small, dependency-free statistics used by the evaluation.

- Wilson score interval: CI for a detection rate (better than normal approx.
  near 0% / 100%, which is exactly where detection rates live).
- Exact McNemar test: compares two strategies on the *same* mutants (paired
  binary outcomes). Only discordant pairs carry information.
- Percentile bootstrap: CI for mean cost savings without assuming normality.
"""

import math
import random


def wilson(k, n, z=1.959964):
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def mcnemar_exact(b, c):
    """Two-sided exact McNemar p-value.

    b = mutants detected by A but not B, c = detected by B but not A.
    Under H0 each discordant pair is a fair coin flip: Binomial(b + c, 0.5).
    """
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def bootstrap_mean_ci(values, iters=5000, alpha=0.05, seed=12345):
    values = list(values)
    if not values:
        return (float("nan"), float("nan"))
    r = random.Random(seed)
    n = len(values)
    means = sorted(sum(r.choice(values) for _ in range(n)) / n for _ in range(iters))
    lo = means[int(alpha / 2 * iters)]
    hi = means[min(iters - 1, int((1 - alpha / 2) * iters))]
    return (lo, hi)


def holm(pvalues):
    """Holm-Bonferroni adjusted p-values (keeps input order)."""
    order = sorted(range(len(pvalues)), key=lambda i: pvalues[i])
    adjusted = [0.0] * len(pvalues)
    running = 0.0
    m = len(pvalues)
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * pvalues[i]))
        adjusted[i] = running
    return adjusted

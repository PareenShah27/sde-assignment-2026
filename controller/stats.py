"""Deterministic statistics. No decision here depends on an RNG seeded by wall-clock or on an LLM.

Monte Carlo is used for P(best) because it is exact enough and simple to audit; it uses a FIXED seed, so
the same counts always produce the same allocation and the same winner.
"""
from __future__ import annotations

import math

import numpy as np

MC_DRAWS = 4000
_SEED = 20270104


def beta_draws(alpha: np.ndarray, beta: np.ndarray, draws: int = MC_DRAWS, salt: int = 0) -> np.ndarray:
    rng = np.random.default_rng(_SEED + salt)
    return rng.beta(alpha[:, None], beta[:, None], size=(len(alpha), draws))


def prob_best(alpha: np.ndarray, beta: np.ndarray, salt: int = 0) -> np.ndarray:
    """P(option i has the highest rate) under independent Beta posteriors."""
    if len(alpha) == 1:
        return np.ones(1)
    S = beta_draws(alpha, beta, salt=salt)
    win = np.bincount(S.argmax(axis=0), minlength=len(alpha))
    return win / S.shape[1]


def prob_less(a1, b1, a2, b2, salt: int = 0) -> float:
    """P(rate1 < rate2)."""
    S = beta_draws(np.array([a1, a2], float), np.array([b1, b2], float), salt=salt)
    return float((S[0] < S[1]).mean())


def quantile_lift(a1, b1, a0, b0, q: float, salt: int = 0) -> float:
    """q-quantile of the relative lift rate1/rate0 - 1."""
    S = beta_draws(np.array([a1, a0], float), np.array([b1, b0], float), salt=salt)
    return float(np.quantile(S[0] / np.maximum(S[1], 1e-9) - 1, q))


def norm_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2))


def two_prop_z(x1, n1, x2, n2) -> float:
    """z for p1 - p2 (pooled SE). Positive = 1 better."""
    if n1 <= 0 or n2 <= 0:
        return 0.0
    p = (x1 + x2) / (n1 + n2)
    se = math.sqrt(max(p * (1 - p) * (1 / n1 + 1 / n2), 1e-12))
    return (x1 / n1 - x2 / n2) / se


def binom_sf(k: int, n: int, p: float) -> float:
    """P(X >= k), X ~ Binomial(n, p). Exact for small n, normal approx with continuity correction for large."""
    if k <= 0:
        return 1.0
    if n <= 2000:
        # log-space sum of the upper tail
        lp, lq = math.log(max(p, 1e-12)), math.log(max(1 - p, 1e-12))
        tot = 0.0
        for i in range(k, n + 1):
            tot += math.exp(math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq)
        return min(tot, 1.0)
    mu, sd = n * p, math.sqrt(n * p * (1 - p))
    return norm_sf((k - 0.5 - mu) / max(sd, 1e-9))


def wilson(x: float, n: float, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return 0.0, 1.0
    p = x / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def required_n(p: float, rel_lift: float, alpha: float = 0.05, power: float = 0.8) -> int:
    """Per-arm sample for a two-sided two-proportion test. Used to justify minimum sizes in docs/proposals."""
    za = 1.959964 if abs(alpha - 0.05) < 1e-9 else _z(1 - alpha / 2)
    zb = _z(power)
    d = p * rel_lift
    return int(math.ceil((za + zb) ** 2 * 2 * p * (1 - p) / (d * d)))


def _z(q: float) -> float:
    # Acklam's rational approximation is overkill; bisection on erfc is plenty here.
    lo, hi = -10.0, 10.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if 1 - norm_sf(mid) < q:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2

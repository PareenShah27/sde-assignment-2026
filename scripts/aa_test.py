"""A/A and A/B simulation of the experiment policy (controller/variants.py:allocate), for SUBMISSION §5.

Runs the real allocation function day by day against synthetic arms and reports:
  * false-winner rate: a winner declared when all arms are truly equal (A/A)
  * true-winner rate and time-to-winner when one arm is better by +X% relative (A/B)
  * expected engagement cost of exploration when one challenger is X% worse

  python scripts/aa_test.py [--sims 400] [--daily-connected 1100] [--days 8]
"""
import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from controller import settings as S  # noqa: E402
from controller.variants import Posterior, allocate, recency  # noqa: E402


def run(true_rates, sims, daily, days, seed=0, history=3000):
    k = len(true_rates)
    opts = [f"A{i}" for i in range(k)]
    rng = np.random.default_rng(seed)
    winners, first_day, cost = [], [], []
    for _ in range(sims):
        n = {d: np.zeros(k) for d in range(-1, days)}
        x = {d: np.zeros(k) for d in range(-1, days)}
        n[-1][0] = history  # the incumbent has pre-D0 history, challengers don't
        x[-1][0] = rng.binomial(history, true_rates[0])
        alloc = {"A0": 1.0}
        won, when, c = None, None, 0.0
        for day in range(days):
            if day > 0:  # one plan a day, like the controller
                wts = recency(day, n.keys())
                N = sum(wts[d] * n[d] for d in n if d in wts)
                X = sum(wts[d] * x[d] for d in x if d in wts)
                rbar = X.sum() / N.sum()
                pm = (X + S.PRIOR_STRENGTH * rbar) / (N + S.PRIOR_STRENGTH)
                post = Posterior(N, X, pm)
                new, diag = allocate(post, opts, alloc)
                e_cur = sum(alloc.get(o, 0) * post.mean[i] for i, o in enumerate(opts))
                e_new = sum(new.get(o, 0) * post.mean[i] for i, o in enumerate(opts))
                if e_new >= e_cur - S.HARM_TOLERANCE:
                    alloc = new
                if diag.get("winner") and won is None:
                    won, when = diag["winner"], day
            share = np.array([alloc.get(o, 0.0) for o in opts])
            m = rng.multinomial(daily, share / share.sum())
            n[day] += m
            x[day] += rng.binomial(m.astype(int), true_rates)
            c += (share * true_rates).sum() / true_rates[0] - 1
        winners.append(won)
        first_day.append(when)
        cost.append(c / days)
    return winners, first_day, cost


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sims", type=int, default=400)
    ap.add_argument("--daily-connected", type=int, default=1100)
    ap.add_argument("--days", type=int, default=8)
    a = ap.parse_args()
    p = 0.55
    print(f"daily connected per cohort={a.daily_connected}, days={a.days}, sims={a.sims}")
    for k in (3, 4):
        w, _, cost = run(np.full(k, p), a.sims, a.daily_connected, a.days)
        fw = np.mean([x is not None for x in w])
        print(f"A/A  {k} arms: false-winner rate {fw:.1%}; mean engagement cost of exploring {np.mean(cost):+.2%}")
    for lift in (0.10, 0.15, 0.22):
        r = np.full(3, p)
        r[2] = p * (1 + lift)
        w, d, cost = run(r, a.sims, a.daily_connected, a.days, seed=1)
        right = np.mean([x == "A2" for x in w])
        wrong = np.mean([x not in (None, "A2") for x in w])
        days = [x for x, y in zip(d, w) if y == "A2"]
        print(f"A/B  +{lift:.0%} challenger: found {right:.1%} (wrong winner {wrong:.1%}), median day "
              f"{np.median(days) if days else float('nan'):.0f}; mean engagement vs incumbent {np.mean(cost):+.2%}")
    for drop in (0.10, 0.20):
        r = np.full(4, p)
        r[1] = p * (1 - drop)
        _, _, cost = run(r, a.sims, a.daily_connected, a.days, seed=2)
        print(f"A/B  -{drop:.0%} challenger (4 arms): mean engagement vs incumbent {np.mean(cost):+.2%}")


if __name__ == "__main__":
    main()

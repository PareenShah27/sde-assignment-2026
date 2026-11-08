"""Call-time optimisation (C3 bootstrapping + C4 per-cohort call time).

We decide WHO is dialled in a given hour (schedule.hour_weights); capacity and windows stay the business's.

Data    The pre-D0 history ran the business default (no hour tables), so every hour sampled every kind of
        lead — a natural experiment on "who picks up when". We use history days only, so our own schedule
        changes never feed back into the estimate of their own effect.
Model   For a grouping G of feature cells: p_g(h) = pickup of group g at hour h, shrunk toward the group mean
        times the workflow-wide hour curve. Weight w_g(h) = clip((p_g(h)/mean_g)^beta). Only RELATIVE weights
        within an hour matter to the dialler: a lead who picks up mornings gets priority in mornings, and is
        skipped (not lost) in the evening. Nobody is starved: every dialling hour keeps weight >= SCHED_MIN_W.
Dry run A fluid replay of one dialling day (replay_day): per feature cell, leads with attempts left, hourly
        capacity, weighted selection, 2h retry gap, engaged leads leave. Old vs new weights on the same pool.
Choice  Groupings (none / each feature / each feature pair) x beta are scored by CROSS-FITTING: weights fitted
        on even history days, replayed with pickup measured on odd days, and vice versa. The best grouping is
        this optimiser's cohort preference; CohortPlanner turns it into the shared cohort object. We claim the
        cross-fitted gain, not the in-sample one, and verify 24h after apply.
"""
from __future__ import annotations

import itertools
import logging

import numpy as np

from . import settings as S
from .aggregates import FEAT_NAMES, NCODE
from .cohorts import build, code_shares, membership
from .governance import Draft
from .incidents import protected

log = logging.getLogger("controller.schedule")
KAPPA_GROUP = 300.0
KAPPA_CODE = 50.0
BETAS = (1.0, 2.0, 3.0, 4.0)


def history_matrices(w) -> list[tuple[np.ndarray, np.ndarray]]:
    """Per history day: (dials[24, NCODE], connected[24, NCODE])."""
    out = []
    for d in sorted(w.pick):
        if d < 0:
            P = w.pick[d]
            out.append((P[:, :, :, 0].sum(axis=2).astype(float), P[:, :, :, 1].sum(axis=2).astype(float)))
    return out


def fit_weights(n, c, idx, ngroups, hours, beta) -> np.ndarray:
    """(ngroups, 24) hour weights; 0 outside the dialling window."""
    W = np.zeros((ngroups, 24))
    tot_n, tot_c = n.sum(), c.sum()
    if tot_n <= 0:
        W[:, hours] = 1.0
        return W
    pbar = tot_c / tot_n
    r = np.where(n.sum(1) > 0, c.sum(1) / np.maximum(n.sum(1), 1) / max(pbar, 1e-9), 1.0)
    for g in range(ngroups):
        m = idx == g
        ng, cg = n[:, m].sum(1), c[:, m].sum(1)
        if ng.sum() < S.SCHED_MIN_DIALS:
            W[g, hours] = 1.0
            continue
        pg = cg.sum() / ng.sum()
        ph = (cg + KAPPA_GROUP * pg * r) / (ng + KAPPA_GROUP)
        rel = ph[hours] / max(pg, 1e-9)
        W[g, hours] = np.clip(rel ** beta, S.SCHED_MIN_W, S.SCHED_MAX_W)
        W[g, hours] /= W[g, hours].mean()
    return W


def shrunk_p(n, c) -> np.ndarray:
    """(24, NCODE) pickup per hour x cell, shrunk toward cell mean x workflow hour curve."""
    tot_n, tot_c = n.sum(), c.sum()
    pbar = tot_c / max(tot_n, 1)
    pbar_code = (c.sum(0) + KAPPA_CODE * pbar) / (n.sum(0) + KAPPA_CODE)
    r = np.where(n.sum(1) > 0, c.sum(1) / np.maximum(n.sum(1), 1) / max(pbar, 1e-9), 1.0)
    return (c + KAPPA_CODE * pbar_code[None, :] * r[:, None]) / (n + KAPPA_CODE)


def replay_day(pool, p, e, cap, W, hours, attempts: int, gap_hours: int = 2) -> dict:
    """Fluid replay of one dialling day.

    pool[code] active leads at the start of the day; p[h, code] pickup; e[code] P(engaged | connected);
    cap[h] dials in hour h; W[h, code] selection weight; `attempts` per lead per day. Each hour the dialler
    spreads capacity over eligible leads in proportion to count x weight (what weighted sampling does in
    expectation). Engaged leads leave; the rest come back after `gap_hours`. The same model scores old and new
    weights, so its simplifications (no cross-day carry-over, fluid counts) largely cancel in the ratio.
    """
    ready = [pool.astype(float).copy()] + [np.zeros(len(pool)) for _ in range(attempts - 1)]
    cooling: list[list] = []  # [release_hour, attempt index, counts]
    dials = conn = eng = 0.0
    for h in hours:
        for item in [x for x in cooling if x[0] <= h]:
            ready[item[1]] += item[2]
            cooling.remove(item)
        elig = sum(ready)
        score = elig * W[h]
        if score.sum() <= 0 or cap[h] <= 0:
            continue
        d = np.minimum(cap[h] * score / score.sum(), elig)
        frac = np.divide(d, elig, out=np.zeros_like(d), where=elig > 0)
        for k in range(attempts):
            taken = ready[k] * frac
            ready[k] = ready[k] - taken
            cn = taken * p[h]
            en = cn * e
            dials += taken.sum(); conn += cn.sum(); eng += en.sum()
            if k + 1 < attempts:
                cooling.append([h + gap_hours, k + 1, taken - en])
    return {"dials": dials, "connected": conn, "engaged": eng}


def code_weights(W_groups: np.ndarray, idx: np.ndarray) -> np.ndarray:
    """(24, NCODE) from (ngroups, 24)."""
    return W_groups[idx].T


def config_code_weights(cfg: dict) -> np.ndarray:
    """(24, NCODE) weights implied by a config's schedule (1 where no table, 0 for hours a table omits)."""
    ids, idx = membership(cfg.get("cohorts") or [])
    hw = ((cfg.get("schedule") or {}).get("hour_weights") or {})
    G = np.ones((len(ids), 24))
    for g, cid in enumerate(ids):
        t = hw.get(cid) if cid in hw else hw.get("default")
        if t:
            G[g, :] = 0
            for h, v in t.items():
                G[g, int(h)] = float(v)
    return G[idx].T


class Replayer:
    """Holds a workflow's history inputs and scores weightings by cross-fitted replay."""

    def __init__(self, ctl, wid: str):
        w = ctl.agg.wf[wid]
        wf = ctl.wfs[wid]
        self.mats = history_matrices(w)
        lo, hi = wf["dialling_window"]
        self.hours = list(range(lo, hi))
        self.attempts = max(1, int(round(np.mean(wf["cadence"]))))
        self.n = sum(m[0] for m in self.mats) if self.mats else None
        self.c = sum(m[1] for m in self.mats) if self.mats else None
        self.folds = [(sum(m[0] for m in self.mats[i::2]), sum(m[1] for m in self.mats[i::2]), len(self.mats[i::2]))
                      for i in (0, 1)] if len(self.mats) >= 4 else []
        shares = code_shares(w)
        hist_leads = [a.sum() for d, a in w.leads.items() if d < 0]
        per_day = float(np.mean(hist_leads)) if hist_leads else float(wf["daily_batch"]["size"])
        self.pool = shares * per_day * len(wf["cadence"]) / 2  # today's batch + carry-over (approx.)
        E = [a for d, a in w.eng.items() if d < 0]
        if E:
            Es = sum(E)
            ce, ee = Es[..., 0].sum(axis=(1, 2, 3)), Es[..., 1].sum(axis=(1, 2, 3))
            tot = ee.sum() / max(ce.sum(), 1)
            self.e = (ee + 20 * tot) / (ce + 20)
        else:
            self.e = np.full(NCODE, 0.5)

    @property
    def ok(self) -> bool:
        return bool(self.folds)

    def run(self, n, c, ndays, W) -> dict:
        return replay_day(self.pool, shrunk_p(n, c), self.e, n.sum(1) / max(ndays, 1), W, self.hours, self.attempts)

    def cross_fit(self, idx, ngroups, beta, W_old=None) -> dict:
        """Average over the two folds of (new/old - 1) for pickup and leads engaged."""
        Wo = np.ones((24, NCODE)) if W_old is None else W_old
        gp = ge = 0.0
        for fi, ei in ((0, 1), (1, 0)):
            Wn = code_weights(fit_weights(self.folds[fi][0], self.folds[fi][1], idx, ngroups, self.hours, beta), idx)
            n, c, k = self.folds[ei]
            o, nw = self.run(n, c, k, Wo), self.run(n, c, k, Wn)
            gp += ((nw["connected"] / nw["dials"]) / (o["connected"] / o["dials"]) - 1) / 2
            ge += (nw["engaged"] / o["engaged"] - 1) / 2
        return {"pickup": gp, "engaged": ge}


class Schedule:
    name = "schedule"

    # ---------------------------------------------------------------- cohort preference
    def feature_preference(self, ctl, wid: str):
        ws = ctl.store.wf_state(wid)
        day = ctl.now // 1440
        cached = ws.get("sched_pref")
        if cached and cached["day"] == day:
            return cached["features"], cached["gain_business"]
        rp = Replayer(ctl, wid)
        shares = code_shares(ctl.agg.wf[wid])
        best, results = ([], 2.0, {"pickup": 0.0, "engaged": 0.0}), []
        if rp.ok:
            cands = [[f] for f in FEAT_NAMES] + [list(p) for p in itertools.combinations(FEAT_NAMES, 2)]
            for feats in cands:
                cohorts = build(feats, shares)
                if not cohorts:
                    continue
                ids, idx = membership(cohorts)
                for beta in BETAS:
                    g = rp.cross_fit(idx, len(ids), beta)
                    results.append({"features": feats, "beta": beta, "pickup": round(g["pickup"], 4),
                                    "engaged": round(g["engaged"], 4)})
                    if g["engaged"] > best[2]["engaged"]:
                        best = (feats, beta, g)
        feats, beta, g = best
        if g["engaged"] < S.SCHED_MIN_GAIN:
            feats = []
        singles = {}
        for r in results:
            if len(r["features"]) == 1:
                f = r["features"][0]
                singles[f] = max(singles.get(f, 0.0), r["engaged"])
        ws["sched_pref"] = {"day": day, "features": feats, "beta": beta,
                            "gain_pickup_cf": round(g["pickup"], 4), "gain_business": round(g["engaged"], 4) if feats else 0.0,
                            "singles": singles,
                            "top": sorted(results, key=lambda r: -r["engaged"])[:5], "history_days": len(rp.mats)}
        with ctl.store.tx():
            ctl.store.save_wf_state(wid, ws)
        return feats, ws["sched_pref"]["gain_business"]

    def feature_gains(self, ctl, wid: str) -> dict[str, float]:
        """Per-feature cross-fitted gain in leads concluded. Below SCHED_MIN_GAIN we don't ask for cohorts at
        all (not worth an approval on its own); hour tables can still ride along on cohorts others want."""
        feats, _ = self.feature_preference(ctl, wid)
        if not feats:
            return {}
        return dict(ctl.store.wf_state(wid)["sched_pref"].get("singles") or {})

    # ---------------------------------------------------------------- drafts
    def tick(self, ctl):
        for wid in ctl.wfs:
            try:
                self._wf(ctl, wid)
            except Exception:  # an optimiser bug must never take down the loop or the safety modules
                log.exception("schedule planner failed for %s", wid)

    def _done(self, ctl, wid, ws, day):
        ws["sched_done_day"] = day
        with ctl.store.tx():
            ctl.store.save_wf_state(wid, ws)

    def _wf(self, ctl, wid: str):
        ws = ctl.store.wf_state(wid)
        day = ctl.now // 1440
        if ws.get("sched_done_day") == day or protected(ctl, wid, "schedule"):
            return
        if any(p["created_minute"] // 1440 == day and "schedule" in (p["meta"].get("kinds") or [])
               for p in ctl.store.proposals(workflow_id=wid)):
            return self._done(ctl, wid, ws, day)
        self.feature_preference(ctl, wid)
        ws = ctl.store.wf_state(wid)
        pref = ws["sched_pref"]
        rp = Replayer(ctl, wid)
        if not rp.ok:
            return self._done(ctl, wid, ws, day)
        cohorts = ctl.cohort_target.get(wid, [])
        cur = ctl.current[wid]["config"]
        ids, idx = membership(cohorts)
        W = fit_weights(rp.n, rp.c, idx, len(ids), rp.hours, pref["beta"])
        tables = {}
        for g, cid in enumerate(ids):
            if not np.allclose(W[g, rp.hours], 1.0):
                tables[cid] = {str(h): round(float(W[g, h]), 3) for h in rp.hours}
        if tables and "default" not in tables:
            tables["default"] = {str(h): 1.0 for h in rp.hours}  # explicit, so supervisors see the whole picture
        new_w = config_code_weights({"cohorts": cohorts, "schedule": {"hour_weights": tables}})
        old_w = config_code_weights(cur)
        cf = rp.cross_fit(idx, len(ids), pref["beta"], W_old=old_w)
        full_old, full_new = rp.run(rp.n, rp.c, len(rp.mats), old_w), rp.run(rp.n, rp.c, len(rp.mats), new_w)
        same_cohorts = [x["id"] for x in cohorts] == [x["id"] for x in cur.get("cohorts") or []]
        # cohorts already wanted (by this or another optimiser): hour tables ride along at a lower bar
        bar = S.SCHED_MIN_GAIN_PIGGYBACK if cohorts else S.SCHED_MIN_GAIN
        if (cf["engaged"] < bar or not tables) and same_cohorts:
            return self._done(ctl, wid, ws, day)
        patch = {"schedule": {"hour_weights": tables}}
        note = ""
        if not same_cohorts:
            patch["cohorts"] = cohorts
            note = f" (introduces {len(cohorts)} cohorts on {sorted({f for x in cohorts for f in x['where']})})"
        pk = lambda r: r["connected"] / r["dials"]
        ctl.drafts[wid].append(Draft(
            wid=wid, kind="schedule",
            summary=f"Call each cohort at the hours it picks up most{note}: est. pickup {cf['pickup']:+.1%}",
            patch=patch,
            rationale=(f"History ({len(rp.mats)} days, every hour sampled under the business default) shows "
                       f"cohorts pick up at different hours. Weights = (relative pickup)^{pref['beta']:.0f}, "
                       f"clipped [{S.SCHED_MIN_W}, {S.SCHED_MAX_W}]; every hour keeps a positive weight. Window and "
                       f"capacity unchanged; only who is called first in each hour. Cross-fitted replay: pickup "
                       f"{cf['pickup']:+.1%}, leads concluded {cf['engaged']:+.1%}."),
            dry_run={"method": "fluid replay of a dialling day on history; weights fitted on even days, scored on odd "
                               "days and vice versa", "history_days": len(rp.mats),
                     "pickup_before": round(pk(full_old), 4), "pickup_after": round(pk(full_new), 4),
                     "cross_fitted": {k: round(v, 4) for k, v in cf.items()},
                     "grouping_candidates_top5": pref.get("top")},
            lift=max(cf["engaged"], 0.0), meta={"kpi": "pickup_rate"}))

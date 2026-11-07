"""The ONE cohort object (C2) shared by scheduling, experiments, detection and the supervisor view.

A cohort set for a workflow is a partition of the 252 feature cells built as a product over at most two
features (e.g. age_band x city_tier). Cells under MIN_COHORT_SHARE of traffic fold into `default`, which
caps fragmentation. IDs are derived from the definition ("age_band=60+|city_tier=1"), so the same
definition always has the same ID across config versions, in proposals, in the audit log and in the view.

Each optimiser (schedule now, variant experiments later) reports which features it would split on and what
that split is worth on the workflow's KPI. CohortPlanner merges those preferences into one target set, at
most once per day (stability for supervisors), ordered so the primary KPI's optimiser wins ties.
"""
from __future__ import annotations

import itertools

import numpy as np

from . import settings as S
from .aggregates import FEATURES, NCODE, cells_where


def cohort_id(where: dict) -> str:
    return "|".join(f"{f}={'+'.join(v)}" for f, v in sorted(where.items()))


def code_shares(w, days: int = 7) -> np.ndarray:
    """Share of uploaded leads per feature cell, recent `days` days (falls back to all)."""
    tot = np.zeros(NCODE)
    ds = sorted(w.leads)
    for d in ds[-days:]:
        tot += w.leads[d]
    if tot.sum() == 0:
        return np.full(NCODE, 1 / NCODE)
    return tot / tot.sum()


def build(features: list[str], shares: np.ndarray) -> list[dict]:
    """Product cells over `features`, dropping cells under MIN_COHORT_SHARE (they fall into default).
    Ordered largest-first; capped at MAX_COHORTS."""
    if not features:
        return []
    cells = []
    for combo in itertools.product(*[FEATURES[f] for f in features]):
        where = {f: [v] for f, v in zip(features, combo)}
        s = float(shares[cells_where(where)].sum())
        if s >= S.MIN_COHORT_SHARE:
            cells.append((s, {"id": cohort_id(where), "where": where}))
    cells.sort(key=lambda x: (-x[0], x[1]["id"]))
    out = [c for _, c in cells[: S.MAX_COHORTS]]
    # never leave `default` empty-but-implied by covering ~everything with tiny remainders: fine either way,
    # the simulator assigns unmatched leads to default.
    return out


def membership(cohorts: list[dict]) -> tuple[list[str], np.ndarray]:
    """(ids, idx) where idx[code] is the cohort index (0 = default), first-match semantics like the dialler."""
    ids = ["default"] + [c["id"] for c in cohorts]
    idx = np.zeros(NCODE, dtype=np.int16)
    free = np.ones(NCODE, dtype=bool)
    for i, c in enumerate(cohorts):
        m = free & cells_where(c["where"])
        idx[m] = i + 1
        free &= ~m
    return ids, idx


class CohortPlanner:
    """Decides the target cohort set per workflow (once per day) from the optimisers' preferences."""

    def __init__(self, scorers: list):
        self.scorers = scorers  # objects with .feature_preference(ctl, wid) -> (features, gain_on_primary)

    def tick(self, ctl):
        ctl.cohort_target = getattr(ctl, "cohort_target", {})
        for wid in ctl.wfs:
            ctl.cohort_target[wid] = self.target(ctl, wid)

    def target(self, ctl, wid: str) -> list[dict]:
        ws = ctl.store.wf_state(wid)
        cur = ctl.current[wid]["config"].get("cohorts") or []
        day = ctl.now // 1440
        cached = ws.get("cohort_target")
        if cached and cached["day"] == day:
            return cached["cohorts"]
        from .incidents import protected
        if protected(ctl, wid, "cohorts"):
            return cur
        prefs = []
        for s in self.scorers:
            feats, gain = s.feature_preference(ctl, wid)
            if feats and gain > 0:
                prefs.append((gain, s.name, feats))
        prefs.sort(key=lambda x: -x[0])
        chosen: list[str] = []
        for _, _, feats in prefs:
            for f in feats:
                if f not in chosen and len(chosen) < 2:
                    chosen.append(f)
        shares = code_shares(ctl.agg.wf[wid])
        target = build(chosen, shares)
        # hysteresis: if the current set uses the same features, keep it verbatim (no churn from share drift)
        cur_feats = sorted({f for c in cur for f in c["where"]})
        if cur and cur_feats == sorted(chosen):
            target = cur
        ws["cohort_target"] = {"day": day, "cohorts": target, "features": chosen,
                               "prefs": [{"gain": round(g, 4), "by": n, "features": f} for g, n, f in prefs]}
        with ctl.store.tx():
            ctl.store.save_wf_state(wid, ws)
            if [c["id"] for c in target] != [c["id"] for c in cur]:
                ctl.store.decide(ctl.now, ctl.ts, wid, "cohort_plan",
                                 f"target cohorts on {chosen or 'none'}: {len(target)} cohorts + default",
                                 ws["cohort_target"])
        return target

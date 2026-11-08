"""The ONE cohort object (C2) shared by scheduling, experiments, detection and the supervisor view.

A cohort set for a workflow is a partition of the 252 feature cells built as a product over at most two
features (e.g. age_band x city_tier). Cells under MIN_COHORT_SHARE of traffic fold into `default`, which
caps fragmentation. IDs are derived from the definition ("age_band=60+|city_tier=1"), so the same
definition always has the same ID across config versions, in proposals, in the audit log and in the view.

Each optimiser (call-time schedule, variant experiments) reports, per feature, what splitting on it is worth
in leads concluded (cross-fitted, so noise doesn't look like structure). CohortPlanner adds those up, takes the
top two features, and changes the live set at most once a day and only when clearly better (hysteresis).
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


def build(features: list[str], shares: np.ndarray, min_share: float | None = None) -> list[dict]:
    """Product cells over `features`, dropping cells under min_share (they fall into default).
    Ordered largest-first; capped at MAX_COHORTS."""
    floor = S.MIN_COHORT_SHARE if min_share is None else max(S.MIN_COHORT_SHARE, min_share)
    if not features:
        return []
    cells = []
    for combo in itertools.product(*[FEATURES[f] for f in features]):
        where = {f: [v] for f, v in zip(features, combo)}
        s = float(shares[cells_where(where)].sum())
        if s >= floor:
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

    HYSTERESIS = 0.8  # keep the current features unless the new choice is worth >25% more

    def __init__(self, scorers: list):
        # scorers expose .name and .feature_gains(ctl, wid) -> {feature: expected relative gain in leads
        # concluded from splitting on it} (0 / absent when the split isn't worth its own bar)
        self.scorers = scorers

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
        from .cfg import parse_when
        from .incidents import protected
        if protected(ctl, wid, "cohorts"):
            return cur
        last_change = max((parse_when(v["applied_at"]) for v in ctl.store.config_versions(wid)
                           if "cohorts" in v["changed_dims"] and v["applied_at"]), default=None)
        if cur and last_change is not None and ctl.now - last_change < S.COHORT_CHANGE_MIN_HOURS * 60:
            return cur  # stability: live cohorts change at most every COHORT_CHANGE_MIN_HOURS
        score: dict[str, float] = {}
        by: dict[str, dict] = {}
        for s in self.scorers:
            for f, g in (s.feature_gains(ctl, wid) or {}).items():
                if g > 0:
                    score[f] = score.get(f, 0.0) + g
                    by.setdefault(f, {})[s.name] = round(g, 4)
        chosen = [f for f, _ in sorted(score.items(), key=lambda x: (-x[1], x[0]))[:2]]
        cur_feats = sorted({f for c in cur for f in c["where"]})
        # hysteresis: churning cohorts costs an approval and confuses supervisors; keep what's live unless the
        # new split is clearly better
        if cur_feats and sum(score.get(f, 0) for f in cur_feats) >= self.HYSTERESIS * sum(score[f] for f in chosen):
            chosen = cur_feats
        w = ctl.agg.wf[wid]
        shares = code_shares(w)
        recent = [d for d in sorted(w.eng) if d < day][-3:]
        daily_conn = (sum(float(w.eng[d][..., 0].sum()) for d in recent) / len(recent)) if recent else 0.0
        min_share = S.MIN_COHORT_CONNECTED_PER_DAY / daily_conn if daily_conn else 1.0
        target = cur if (cur and cur_feats == sorted(chosen)) else build(chosen, shares, min_share)
        ws["cohort_target"] = {"day": day, "cohorts": target, "features": chosen,
                               "feature_scores": {f: {"total": round(v, 4), **by[f]} for f, v in score.items()}}
        with ctl.store.tx():
            ctl.store.save_wf_state(wid, ws)
            if [c["id"] for c in target] != [c["id"] for c in cur]:
                ctl.store.decide(ctl.now, ctl.ts, wid, "cohort_plan",
                                 f"target cohorts on {chosen or 'none'}: {len(target)} cohorts + default",
                                 ws["cohort_target"])
        return target

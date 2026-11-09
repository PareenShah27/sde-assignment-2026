"""Caller-ID health (C5): detect a degrading number, rotate away from it, and explore unproven numbers safely.

Measurement  The dialler draws caller_id independently of who the lead is, so within the same hour every number
             sees the same mix of people. For number d over the last DID_WINDOW_HOURS we compare its connects
             with what the OTHER numbers' pickup rate in the same (day, hour) cells predicts:
                 ratio_d = connected_d / sum_cells(dials_d,cell * pickup_others,cell)
             This removes time-of-day and lead-mix effects without a model. z uses the binomial variance.
States       unknown -> probing -> healthy;  any -> unhealthy (ratio < 0.8, z < -3, >= DID_MIN_DIALS);
             unhealthy -> probing again after DID_REST_DAYS (doubling after each failed re-test) at a 2% share.
Allocation   healthy numbers share traffic EQUALLY (spreading load per number is itself protective: carriers
             flag high-volume numbers); each probing number gets DID_PROBE_SHARE; unhealthy get 0.
             The probe share is sized so a bad unknown number cannot move expected caller-ID health by >2%.
Routing      caller_id is operational: auto-apply where the policy allows (max_shift respected as a rate limit,
             one step per cycle), otherwise approval. Removing a number measured as bad is urgent.
"""
from __future__ import annotations

import logging
import math

import numpy as np

from . import settings as S
from .cfg import cohort_ids, dist, fmt_minute, tv
from .governance import Draft
from .incidents import protected

log = logging.getLogger("controller.callerid")


def did_stats(w, m0: int, m1: int, exclude=()) -> dict[str, dict]:
    """Per number: dials, connected, expected (from other numbers, same hour cells), ratio, z.
    Numbers in `exclude` are left out of every comparator pool (a bad number must not flatter the rest)."""
    out = {d: {"dials": 0, "connected": 0, "expected": 0.0, "var": 0.0} for d in w.opts["caller_id"]}
    for day in range(m0 // 1440, (m1 - 1) // 1440 + 1):
        P = w.pick.get(day)
        if P is None:
            continue
        for h in range(24):
            t = day * 1440 + h * 60
            if not (m0 <= t < m1):
                continue
            cell = P[h].sum(axis=0)  # (nD, 2)
            keep = np.array([d not in exclude for d in w.opts["caller_id"]])
            tot_n, tot_c = cell[keep, 0].sum(), cell[keep, 1].sum()
            for i, d in enumerate(w.opts["caller_id"]):
                n, c = int(cell[i, 0]), int(cell[i, 1])
                if n == 0:
                    continue
                rn, rc = (tot_n - n, tot_c - c) if keep[i] else (tot_n, tot_c)
                o = out[d]
                o["dials"] += n
                o["connected"] += c
                if rn > 0:
                    p = rc / rn
                    o["expected"] += n * p
                    o["var"] += n * p * (1 - p)
    for d, o in out.items():
        o["ratio"] = o["connected"] / o["expected"] if o["expected"] > 0 else None
        o["z"] = (o["connected"] - o["expected"]) / math.sqrt(o["var"]) if o["var"] > 0 else 0.0
        o.pop("var")
    return out


def robust_stats(w, m0: int, m1: int, known_bad=()) -> dict[str, dict]:
    """Two passes: numbers that look bad in pass 1 (or are known bad) are excluded from the comparators."""
    s = did_stats(w, m0, m1, exclude=set(known_bad))
    bad = set(known_bad) | {d for d, o in s.items() if o["ratio"] is not None and o["dials"] >= S.DID_MIN_DIALS
                            and o["ratio"] < S.DID_BAD_RATIO and o["z"] < -S.DID_BAD_Z}
    if len(bad) >= len(s):
        return s
    return did_stats(w, m0, m1, exclude=bad)


class CallerID:
    def tick(self, ctl):
        for wid in ctl.wfs:
            try:
                self._wf(ctl, wid)
            except Exception:
                log.exception("caller-id planner failed for %s", wid)

    def _wf(self, ctl, wid: str):
        st = ctl.store
        w = ctl.agg.wf[wid]
        ws = st.wf_state(wid)
        dids = ws.get("dids")
        if dids is None:
            dids = self._bootstrap(ctl, wid)
        known_bad = {d for d, v in dids.items() if v["state"] == "unhealthy"}
        stats = robust_stats(w, ctl.now - S.DID_WINDOW_HOURS * 60, ctl.now, known_bad)
        changed = []
        for d in list(stats):
            rec = dids.setdefault(d, {"state": "unknown", "since": ctl.now})
            if rec["state"] == "probing":
                # a probe is judged on everything since it started (small shares need more than 24h)
                stats[d] = robust_stats(w, max(rec["since"], ctl.now - 72 * 60), ctl.now, known_bad - {d})[d]
            s = stats[d]
            r, z, n = s["ratio"], s["z"], s["dials"]
            rec["last"] = {"dials": n, "ratio": None if r is None else round(r, 3), "z": round(z, 2)}
            new = rec["state"]
            if r is not None and n >= S.DID_MIN_DIALS and r < S.DID_BAD_RATIO and z < -S.DID_BAD_Z:
                new = "unhealthy"
            elif rec["state"] == "probing" and r is not None and n >= S.DID_MIN_DIALS and r >= S.DID_GOOD_RATIO and z > -2:
                new = "healthy"
            elif rec["state"] == "unhealthy" and ctl.now - rec["since"] >= rec.get("rest_days", S.DID_REST_DAYS) * 1440:
                new = "probing"
            if new != rec["state"]:
                changed.append((d, rec["state"], new, s))
                if new == "probing":
                    rec["reprobe"] = True
                elif new == "unhealthy" and rec.get("reprobe"):
                    rec["rest_days"] = rec.get("rest_days", S.DID_REST_DAYS) * 2  # failed re-test: back off
                elif new == "healthy":
                    rec.pop("reprobe", None)
                    rec.pop("rest_days", None)
                rec["state"], rec["since"] = new, ctl.now
        ws["dids"] = dids
        with st.tx():
            st.save_wf_state(wid, ws)
            for d, old, new, s in changed:
                st.decide(ctl.now, ctl.ts, wid, "did_health",
                          f"caller ID {d}: {old} -> {new} (pickup ratio {s['ratio'] and round(s['ratio'], 2)} vs other "
                          f"numbers in the same hours, z={s['z']:.1f}, {s['dials']} dials/{S.DID_WINDOW_HOURS}h)", s)
                if new == "unhealthy":
                    st.alert(ctl.now, "notify", wid, f"did_unhealthy:{d}",
                             f"{wid}: caller ID {d} picks up at {s['ratio']:.0%} of the other numbers; rotating out.")
                elif new == "healthy":  # a re-test (probing) is not a recovery; only clear on proven health
                    st.resolve_alert(ctl.now, wid, f"did_unhealthy:{d}")
        self._plan(ctl, wid, dids, stats)

    def _bootstrap(self, ctl, wid: str) -> dict:
        """Numbers the business already dials (history) start healthy unless history says otherwise."""
        w = ctl.agg.wf[wid]
        hist_days = [d for d in w.pick if d < 0]
        dids = {d: {"state": "unknown", "since": ctl.now} for d in w.opts["caller_id"]}
        if hist_days:
            s = robust_stats(w, min(hist_days) * 1440, 0)
            for d, o in s.items():
                if o["dials"] >= S.DID_MIN_DIALS:
                    bad = o["ratio"] is not None and o["ratio"] < S.DID_BAD_RATIO and o["z"] < -S.DID_BAD_Z
                    dids[d]["state"] = "unhealthy" if bad else "healthy"
                    dids[d]["history"] = {"dials": o["dials"], "ratio": o["ratio"] and round(o["ratio"], 3)}
        with ctl.store.tx():
            ctl.store.decide(ctl.now, ctl.ts, wid, "did_bootstrap",
                             "caller IDs from history: " + ", ".join(f"{d}={v['state']}" for d, v in dids.items()), dids)
        return dids

    def target(self, dids: dict, cur_default: dict) -> dict:
        healthy = [d for d, v in dids.items() if v["state"] == "healthy"]
        probing = [d for d, v in dids.items() if v["state"] in ("probing", "unknown")]
        if not healthy:
            healthy = [d for d in cur_default if dids.get(d, {}).get("state") != "unhealthy"]
            probing = [d for d in probing if d not in healthy]
        if not healthy:
            return cur_default
        probing = probing[:3]  # at most 15% of traffic on unproven numbers at once
        share = {d: S.DID_REPROBE_SHARE if dids.get(d, {}).get("reprobe") else S.DID_PROBE_SHARE for d in probing}
        out = {d: (1 - sum(share.values())) / len(healthy) for d in healthy}
        out.update(share)
        # load spreading: cap any single number, giving the excess to probes that already look good
        promising = [d for d in probing if (dids[d].get("last") or {}).get("dials", 0) >= 100
                     and ((dids[d].get("last") or {}).get("ratio") or 0) >= 0.9]
        if promising:
            excess = sum(max(0.0, out[d] - S.DID_MAX_SHARE) for d in healthy)
            for d in healthy:
                out[d] = min(out[d], S.DID_MAX_SHARE)
            for d in promising:
                out[d] += excess / len(promising)
        return {d: round(v, 4) for d, v in sorted(out.items())}

    def _plan(self, ctl, wid: str, dids: dict, stats: dict):
        cfg = ctl.current[wid]["config"]
        cur = dist(cfg, "default", "caller_id")
        tgt = self.target(dids, cur)
        bad_live = [d for d in cur if dids.get(d, {}).get("state") == "unhealthy" and cur[d] > 0]
        urgent = bool(bad_live)
        if not urgent and (tv(cur, tgt) < 0.02 or protected(ctl, wid, "caller_id")):
            return
        # newly probed numbers: mark them probing so the state is honest in the view
        newly = [d for d in tgt if dids.get(d, {}).get("state") == "unknown"]
        if newly:
            for d in newly:
                dids[d]["state"], dids[d]["since"] = "probing", ctl.now
            ws = ctl.store.wf_state(wid)
            ws["dids"] = dids
            with ctl.store.tx():
                ctl.store.save_wf_state(wid, ws)
        patch = {"allocations": {"default": {"caller_id": tgt}}}
        for k in cohort_ids(cfg)[1:]:
            own = (cfg["allocations"].get(k) or {}).get("caller_id")
            if own:  # someone set a cohort-specific caller ID: keep their choice, minus unhealthy numbers
                keep = {d: v for d, v in own.items() if dids.get(d, {}).get("state") != "unhealthy"}
                patch["allocations"][k] = {"caller_id": keep or tgt}

        def exp_ratio(alloc):
            tot = 0.0
            for d, s in alloc.items():
                r = (stats.get(d) or {}).get("ratio")
                st = dids.get(d, {}).get("state")
                r = r if (r is not None and (stats[d]["dials"] >= S.DID_MIN_DIALS)) else (0.9 if st != "unhealthy" else 0.5)
                tot += s * r
            return tot
        lift = exp_ratio(tgt) / max(exp_ratio(cur), 1e-9) - 1
        why_bad = "; ".join(f"{d} at {stats[d]['ratio']:.0%} of other numbers' pickup (z={stats[d]['z']:.1f}, "
                            f"{stats[d]['dials']} dials)" for d in bad_live if stats[d]["ratio"] is not None)
        summary = (f"Stop dialling from caller ID {', '.join(bad_live)} (pickup collapsed)" if urgent else
                   f"Rotate caller IDs: {', '.join(f'{d} {v:.0%}' for d, v in tgt.items())}")
        mid = ctl.wfs[wid]["mid_flight"]
        ctl.drafts[wid].append(Draft(
            wid=wid, kind="did_rotation", urgent=urgent, risk_reducing=urgent,
            summary=summary, patch=patch,
            rationale=((f"{why_bad}. " if why_bad else "") +
                       "Healthy numbers share traffic equally; unproven numbers are probed at "
                       f"{S.DID_PROBE_SHARE:.0%} each until {S.DID_MIN_DIALS} dials show pickup >= "
                       f"{S.DID_GOOD_RATIO:.0%} of the others." +
                       (" Note: this workflow locks allocations per campaign, so this reaches new campaigns only."
                        if mid == "next_campaign_only" else "")),
            dry_run={"window_hours": S.DID_WINDOW_HOURS,
                     "per_number": {d: {**(stats.get(d) or {}), "state": dids.get(d, {}).get("state")} for d in dids},
                     "allocation_before": cur, "allocation_after": tgt,
                     "expected_pickup_change": round(lift, 4),
                     "assumption": "unmeasured numbers assumed at 0.9x of measured ones (conservative)"},
            lift=max(lift, 0.0), meta={"kpi": "pickup_rate", "bad": bad_live}))

"""Post-apply verification: "what happened next" for every change we applied, and guardrail rollbacks.

For each applied change, VERIFY_AFTER_HOURS later we compare the 24h after with the 24h before (same hours of
day, so time-of-day mix cancels) on BOTH KPIs, separately. The result is written to the audit log.

If the guardrail KPI fell by more than the workflow's allowed drop (objective.guardrail.*_max_drop, read as
an absolute rate drop) with z < -GUARDRAIL_Z, and the change touched a dim that can move that KPI
(pickup <- caller_id/schedule; engagement <- welcome/voice/prompt/cohorts), we propose an urgent rollback of
those dims to the pre-change values. Before/after comparisons are confounded by day-to-day drift (campaign
mix, carrier conditions), which is why the bar is a large, significant drop and why experiments (randomised
arms) — not this check — decide winners.
"""
from __future__ import annotations

from . import settings as S
from .cfg import fmt_minute
from .governance import Draft
from .stats import two_prop_z

MOVES = {"pickup_rate": {"caller_id", "schedule"},
         "engagement_rate": {"welcome_message", "voice", "prompt_version", "cohorts"}}


def guardrail(objective: dict) -> tuple[str, float]:
    g = objective.get("guardrail") or {}
    for k, v in g.items():
        if k.startswith("pickup"):
            return "pickup_rate", float(v)
        if k.startswith("engagement"):
            return "engagement_rate", float(v)
    return ("engagement_rate" if objective.get("primary") == "pickup_rate" else "pickup_rate"), 0.02


class Verifier:
    def tick(self, ctl):
        for wid in ctl.wfs:
            ws = ctl.store.wf_state(wid)
            todo = [a for a in ws.get("applied", []) if not a.get("verified")
                    and ctl.now >= a["minute"] + S.VERIFY_AFTER_HOURS * 60]
            if not todo:
                continue
            for a in todo:
                self._verify(ctl, wid, ws, a)
            with ctl.store.tx():
                ctl.store.save_wf_state(wid, ws)

    def _verify(self, ctl, wid, ws, a):
        w = ctl.agg.wf[wid]
        h = S.VERIFY_AFTER_HOURS * 60
        t = a["minute"] - a["minute"] % 60
        before, after = w.window(t - h, t), w.window(t, t + h)
        k = {}
        for name, num, den in (("pickup_rate", "connected", "dials"), ("engagement_rate", "engaged", "connected")):
            b = before[num] / before[den] if before[den] else None
            f = after[num] / after[den] if after[den] else None
            z = two_prop_z(after[num], after[den], before[num], before[den])
            k[name] = {"before": b and round(b, 4), "after": f and round(f, 4), "z": round(z, 2)}
        gname, gdrop = guardrail(ctl.wfs[wid]["objective"])
        g = k[gname]
        breach = (g["before"] is not None and g["after"] is not None and g["after"] - g["before"] < -gdrop
                  and g["z"] < -S.GUARDRAIL_Z and set(a["dims"]) & MOVES[gname])
        a["verified"] = True
        a["result"] = k
        a["guardrail_breach"] = bool(breach)
        with ctl.store.tx():
            ctl.store.decide(
                ctl.now, ctl.ts, wid, "post_apply_check",
                f"v{a['version']} ({'+'.join(a.get('kinds') or [])}) 24h after vs before: pickup "
                f"{_fmt(k['pickup_rate'])}, engagement {_fmt(k['engagement_rate'])}"
                + (f" -> GUARDRAIL BREACH on {gname}, rolling back" if breach else ""),
                {"kpis": k, "guardrail": [gname, gdrop], "before": before, "after": after},
                proposal_id=a["proposal_id"], config_version=a["version"])
            if breach:
                ctl.store.alert(ctl.now, "notify", wid, f"guardrail:{a['version']}",
                                f"{wid}: {gname} fell {g['before']:.1%} -> {g['after']:.1%} after v{a['version']}; "
                                f"rollback proposed.")
        if breach:
            self._rollback(ctl, wid, a, gname, g)

    def _rollback(self, ctl, wid, a, gname, g):
        versions = {v["version"]: v for v in ctl.store.config_versions(wid)}
        prev = versions.get(a["version"] - 1, {}).get("config")
        if not prev:
            return
        dims = set(a["dims"]) & MOVES[gname]
        patch = {}
        if "schedule" in dims:
            patch["schedule"] = {"hour_weights": (prev.get("schedule") or {}).get("hour_weights") or {}}
        if "cohorts" in dims:
            patch["cohorts"] = prev.get("cohorts") or []
        if "prompt_version" in dims:
            patch["prompt_version"] = prev["prompt_version"]
        for d in dims & {"caller_id", "welcome_message", "voice"}:
            patch.setdefault("allocations", {})
            for k, dd in (prev.get("allocations") or {}).items():
                if d in dd:
                    patch["allocations"].setdefault(k, {})[d] = dd[d]
        ctl.drafts[wid].append(Draft(
            wid=wid, kind="guardrail_rollback", urgent=True, risk_reducing=True,
            summary=f"Roll back {', '.join(sorted(dims))} from v{a['version']}: {gname} fell "
                    f"{g['before']:.1%} -> {g['after']:.1%}",
            patch=patch,
            rationale=f"24h before/after check on v{a['version']} (applied {fmt_minute(a['minute'])}) shows the "
                      f"guardrail {gname} dropped beyond its allowed margin (z={g['z']}).",
            dry_run={"kpi": gname, **g}, lift=0.0, meta={"rolls_back": a["version"]}))


def _fmt(k):
    if k["before"] is None or k["after"] is None:
        return "n/a"
    return f"{k['before']:.1%} -> {k['after']:.1%} (z={k['z']:+.1f})"

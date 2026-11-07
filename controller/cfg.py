"""Config helpers implemented from the documented contract (docs/SIMULATOR.md §3–4), not imported from sim/.

These mirror what the governance boundary checks so we can predict, before sending anything, whether a change
is auto-applicable (allowed dims + max total-variation shift) and what a diff looks like to a supervisor.
"""
from __future__ import annotations

import copy
import hashlib
import json

AGENT_DIMS = ["welcome_message", "voice", "caller_id"]
CHANGE_DIMS = ["cohorts", "welcome_message", "voice", "caller_id", "prompt_version", "schedule"]
CUSTOMER_FACING = {"welcome_message", "voice", "prompt_version", "cohorts"}  # what a customer can hear
OPERATIONAL = {"caller_id", "schedule"}


def fmt_minute(m: int) -> str:
    d, r = divmod(int(m), 1440)
    return f"D{d} {r // 60:02d}:{r % 60:02d}"


def parse_when(s: str) -> int:
    """'D3 14:00' -> sim minute; also accepts an int-like string or an ISO ts."""
    s = s.strip()
    if s.lstrip("-").isdigit():
        return int(s)
    if s[0] in "Dd":
        day, hm = s.split()
        h, m = hm.split(":")
        return int(day[1:]) * 1440 + int(h) * 60 + int(m)
    import datetime as dt
    t = dt.datetime.fromisoformat(s)
    base = dt.datetime(2027, 1, 4, tzinfo=t.tzinfo)
    return int((t - base).total_seconds() // 60)


def normalised(dist: dict) -> dict:
    s = float(sum(v for v in dist.values() if v > 0))
    return {k: v / s for k, v in dist.items() if v > 0} if s > 0 else {}


def dist(cfg: dict, cohort: str, dim: str) -> dict:
    a = (cfg.get("allocations") or {})
    return normalised((a.get(cohort) or {}).get(dim) or a["default"][dim])


def tv(a: dict, b: dict) -> float:
    return 0.5 * sum(abs(a.get(o, 0.0) - b.get(o, 0.0)) for o in set(a) | set(b))


def changed_dims(old: dict, new: dict) -> list[str]:
    ch = set()
    if (old.get("cohorts") or []) != (new.get("cohorts") or []):
        ch.add("cohorts")
    keys = {"default"} | set(old.get("allocations") or {}) | set(new.get("allocations") or {})
    for d in AGENT_DIMS:
        for k in keys:
            if _close(dist(old, k, d), dist(new, k, d)) is False:
                ch.add(d)
                break
    if old.get("prompt_version") != new.get("prompt_version"):
        ch.add("prompt_version")
    if _sched(old) != _sched(new):
        ch.add("schedule")
    return [d for d in CHANGE_DIMS if d in ch]


def _close(a: dict, b: dict, eps: float = 1e-9) -> bool:
    return all(abs(a.get(o, 0) - b.get(o, 0)) <= eps for o in set(a) | set(b))


def _sched(c: dict) -> dict:
    s = c.get("schedule") or {}
    hw = {k: {str(h): float(w) for h, w in v.items()} for k, v in (s.get("hour_weights") or {}).items()}
    return {"hour_weights": hw, "lead_preferred_hour": dict(s.get("lead_preferred_hour") or {})}


def max_shift(old: dict, new: dict) -> float:
    keys = {"default"} | {c["id"] for c in old.get("cohorts") or []} | {c["id"] for c in new.get("cohorts") or []}
    worst = 0.0
    for k in keys:
        for d in AGENT_DIMS:
            worst = max(worst, tv(dist(old, k, d), dist(new, k, d)))
    return worst


def cohort_ids(cfg: dict) -> list[str]:
    return ["default"] + [c["id"] for c in cfg.get("cohorts") or []]


def clean(cfg: dict) -> dict:
    """Canonical form: rounded normalised weights, no empty tables. Keeps diffs and hashes stable."""
    c = copy.deepcopy(cfg)
    c.setdefault("cohorts", [])
    c.setdefault("schedule", {})
    c["schedule"].setdefault("hour_weights", {})
    c["schedule"].setdefault("lead_preferred_hour", {})
    for k, dims in list(c.get("allocations", {}).items()):
        for d, w in list(dims.items()):
            n = normalised(w)
            dims[d] = {o: round(v, 4) for o, v in sorted(n.items()) if round(v, 4) > 0}
        if not dims and k != "default":
            del c["allocations"][k]
    return c


def config_hash(cfg: dict) -> str:
    return hashlib.sha256(json.dumps(clean(cfg), sort_keys=True).encode()).hexdigest()[:10]


def diff(old: dict, new: dict) -> dict:
    """Human-readable diff: only what changed, per dim and cohort."""
    out: dict = {}
    oc, nc = old.get("cohorts") or [], new.get("cohorts") or []
    if oc != nc:
        out["cohorts"] = {"before": [c["id"] for c in oc], "after": [c["id"] for c in nc],
                          "definitions": {c["id"]: c["where"] for c in nc}}
    keys = {"default"} | set(cohort_ids(old)) | set(cohort_ids(new))
    for d in AGENT_DIMS:
        per = {}
        for k in sorted(keys):
            if k not in cohort_ids(new) and k != "default":
                continue
            try:
                a = dist(old, k, d) if k in cohort_ids(old) else dist(old, "default", d)
            except KeyError:
                a = {}
            b = dist(new, k, d)
            if not _close(a, b, 1e-4):
                per[k] = {"before": {o: round(v, 3) for o, v in a.items()},
                          "after": {o: round(v, 3) for o, v in b.items()}}
        if per:
            out[d] = per
    if old.get("prompt_version") != new.get("prompt_version"):
        out["prompt_version"] = {"before": old.get("prompt_version"), "after": new.get("prompt_version")}
    if _sched(old) != _sched(new):
        out["schedule"] = {"hour_weights_after": _sched(new)["hour_weights"],
                           "hour_weights_before": _sched(old)["hour_weights"]}
    return out


def step_toward(cur: dict, target: dict, cohorts: list[str], dims: list[str], max_tv: float) -> dict:
    """Move allocations from cur toward target by at most max_tv (total variation) per cohort/dim."""
    out = copy.deepcopy(cur)
    for k in cohorts:
        for d in dims:
            a, b = dist(cur, k, d), dist(target, k, d)
            t = tv(a, b)
            if t <= max_tv + 1e-9:
                mix = b
            else:
                f = max_tv / t
                mix = {o: a.get(o, 0) + f * (b.get(o, 0) - a.get(o, 0)) for o in set(a) | set(b)}
            out.setdefault("allocations", {}).setdefault(k, {})[d] = {
                o: round(v, 4) for o, v in mix.items() if v > 1e-6}
    return out

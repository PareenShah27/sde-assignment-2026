"""Audit queries (C12) over our own store. Answers "why did workflow X change at T, who approved it, and what
happened next?" and "what was live for workflow X at T, and why?" without touching the simulator.

  python -m controller why --state-dir state/public_02_incidents --wf ks_cancel --at "D3 14:00"
  python -m controller log --state-dir ... --wf ks_cancel [--since "D3 00:00"] [--kind proposed]
"""
from __future__ import annotations

import json

from .cfg import fmt_minute, parse_when
from .store import Store


def live_version(store: Store, wid: str, minute: int) -> dict | None:
    best = None
    for v in store.config_versions(wid):
        at = parse_when(v["applied_at"]) if v["applied_at"] else -10**9
        if at <= minute:
            best = v
    return best


def why(store: Store, wid: str, minute: int, after_hours: int = 48) -> str:
    v = live_version(store, wid, minute)
    L = [f"# {wid} at {fmt_minute(minute)}"]
    if not v:
        return "\n".join(L + ["no config version recorded"])
    at = parse_when(v["applied_at"]) if v["applied_at"] else None
    L.append(f"Live config: v{v['version']} by **{v['actor']}**"
             + (f", applied {fmt_minute(at)}" if at is not None else " (business default)")
             + f"; changed: {', '.join(v['changed_dims']) or '-'}")
    if v["config"]:
        c = v["config"]
        L.append(f"  prompt={c.get('prompt_version')}  cohorts={[x['id'] for x in c.get('cohorts') or []]}")
        for k, dims in (c.get("allocations") or {}).items():
            L.append(f"  allocations[{k}] = " + json.dumps(dims, sort_keys=True))
    pid = v.get("proposal_id")
    if v["actor"] == "supervisor":
        L.append("Why: a client supervisor edited the config directly (outside our governance; recorded, not overwritten).")
    elif pid:
        p = store.proposal(pid)
        if p:
            b = p["body"]
            L += ["", f"## Why (proposal {pid})",
                  f"Route: {p['route']}" + (f", approval {p['approval_id']}" if p.get("approval_id") else ""),
                  f"Summary: {b['change_summary']}",
                  f"Impact score: {b['impact_score']}  affected leads: {b['affected_leads']}",
                  f"Rationale: {b['rationale']}",
                  "Diff: " + json.dumps(b["diff"], sort_keys=True)[:1500],
                  "Dry run: " + json.dumps(b["dry_run"], sort_keys=True, default=str)[:1500]]
            if b.get("evidence"):
                L.append("Evidence: " + json.dumps(b["evidence"]))
        L += ["", "## Lifecycle"]
        for d in store.db.execute("SELECT * FROM decisions WHERE proposal_id=? ORDER BY id", (pid,)):
            L.append(f"- {fmt_minute(d['sim_minute'])}  {d['kind']:<18} {d['summary']}")
    else:
        L.append("Why: business default config at D0.")
    start = at if at is not None else minute
    L += ["", f"## What happened next (until {fmt_minute(start + after_hours * 60)})"]
    for d in store.decisions(workflow_id=wid, since=start, until=start + after_hours * 60, limit=200):
        if d["proposal_id"] == pid and pid:
            continue
        L.append(f"- {fmt_minute(d['sim_minute'])}  {d['kind']:<18} {d['summary']}")
    return "\n".join(L)


def log_lines(store: Store, wid=None, since=None, kind=None, limit=500) -> str:
    out = []
    for d in store.decisions(workflow_id=wid, since=since, kind=kind, limit=limit):
        out.append(f"{fmt_minute(d['sim_minute'])}  {d['workflow_id'] or '-':<14} {d['kind']:<20} {d['summary']}")
    return "\n".join(out)

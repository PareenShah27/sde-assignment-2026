"""The consolidated supervisor view (C11): one screen per workflow, from our own store only.

What a supervisor asked for (stakeholders/04): what's being tested, what's winning, what you want from me,
what changed today — not fifty alerts. Compliance (stakeholders/02) asked for operational changes we made
under the signed policy to be listed with their reason: that's the "changed without asking" section.

  python -m controller view   --state-dir state/<scenario> [--wf <workflow>] [--html out.html]
The view is always "now" (latest state). For any past moment, `controller why --wf X --at T` answers from the
audit store without mixing in later outcomes.
  python -m controller health --state-dir state/<scenario>
"""
from __future__ import annotations

import html
import json
import types
from pathlib import Path

import numpy as np

from .aggregates import Aggregates
from .cfg import AGENT_DIMS, cohort_ids, dist, fmt_minute, parse_when
from .cohorts import code_shares, membership
from .store import Store


def _ctx(state_dir: str | Path, at: int | None = None):
    sd = Path(state_dir)
    store = Store(sd / "controller.db")
    agg = Aggregates.load(sd / "aggregates.pkl")
    wfs = store.get("workflows") or {}
    last = store.db.execute("SELECT MAX(sim_minute) FROM decisions").fetchone()[0] or 0
    now = at if at is not None else last
    current = {}
    for wid in wfs:
        live = [v for v in store.config_versions(wid)
                if (parse_when(v["applied_at"]) if v["applied_at"] else -10**9) <= now and v["config"]]
        if live:
            current[wid] = {"version": live[-1]["version"], "actor": live[-1]["actor"], "config": live[-1]["config"]}
    return types.SimpleNamespace(store=store, agg=agg, wfs=wfs, current=current, now=now)


def _rate(a, b):
    return f"{a / b:.1%}" if b else "—"


def kpis(w, m0: int, m1: int) -> dict:
    k = w.window(m0, m1)
    return {"dials": k["dials"], "pickup": _rate(k["connected"], k["dials"]),
            "engagement": _rate(k["engaged"], k["connected"]), "engaged": k["engaged"]}


def workflow_view(ctx, wid: str) -> list[str]:
    from .variants import DIMS, Variants, recency
    st, w, wf = ctx.store, ctx.agg.wf.get(wid), ctx.wfs[wid]
    cur = ctx.current.get(wid)
    ws = st.wf_state(wid)
    day = ctx.now // 1440
    obj = wf["objective"]
    L = [f"## {wid}  ·  {wf.get('use_case') or ''}",
         f"Tenant {wf['tenant_id']} · primary KPI **{obj['primary']}** · guardrail {json.dumps(obj.get('guardrail'))} · "
         f"config v{cur['version'] if cur else '?'} (set by {cur['actor'] if cur else '?'}) · as of {fmt_minute(ctx.now)}", ""]
    # ---- KPIs, never blended
    if w:
        L += ["### How it's doing", "", "| window | dials | pickup | engagement | leads concluded |", "|---|---|---|---|---|"]
        for name, m0, m1 in (("today so far", day * 1440, ctx.now), ("yesterday", (day - 1) * 1440, day * 1440),
                             ("last 7 days", (day - 7) * 1440, ctx.now), ("pre-D0 history (business default)",
                                                                        min(w.pick or [0]) * 1440, 0)):
            k = kpis(w, m0, max(m1, m0 + 1))
            L.append(f"| {name} | {k['dials']:,} | {k['pickup']} | {k['engagement']} | {k['engaged']:,} |")
        L.append("")
    # ---- what we need from you
    pend = st.proposals(workflow_id=wid, statuses=("pending", "approved", "intent"))
    L += ["### What we need from you", ""]
    if not pend:
        L.append("Nothing waiting.")
    for p in sorted(pend, key=lambda p: -p["body"]["impact_score"]):
        b = p["body"]
        L.append(f"- **{p['status']}** {p.get('approval_id') or ''} · impact {b['impact_score']:.2f} · "
                 f"{b['affected_leads']:,} leads · {b['change_summary']}")
    L.append("")
    # ---- what's being tested / winning
    if w and cur:
        cfg = cur["config"]
        cohorts = cfg.get("cohorts") or []
        ids, idx = membership(cohorts)
        shares = code_shares(w)
        L += ["### What's being tested and what's winning", "",
              f"Cohorts ({len(cohorts)} + default): " + ", ".join(
                  f"{cid} ({shares[idx == g].sum():.0%})" for g, cid in enumerate(ids)), "",
              "| cohort | dim | option: share · connected · engagement · P(best) | status |", "|---|---|---|---|"]
        v = Variants()
        weights = recency(day, w.eng.keys())
        holds = {o for o, until in (ws.get("decayed") or {}).items() if until > ctx.now}
        for dim in DIMS:
            posts, _ = v.posteriors(ctx, wid, cohorts, dim, weights)
            for cid in ids:
                d = dist(cfg, cid, dim)
                post = posts[cid]
                opts = w.opts[dim]
                cells = []
                for i, o in enumerate(opts):
                    if d.get(o, 0) <= 0 and post.n[i] < 1:
                        continue
                    cells.append(f"{o}: {d.get(o, 0):.0%} · {post.n[i]:,.0f} · {post.mean[i]:.1%} · {post.pbest[i]:.2f}")
                win = (ws.get("winners") or {}).get(f"{cid}/{dim}")
                lead = max(d, key=d.get)
                status = (f"winner {win}" if win and d.get(win, 0) >= 0.8 else
                          "testing" if len(d) > 1 else f"fixed on {lead}")
                if holds & set(opts):
                    status += f" · held out: {', '.join(sorted(holds & set(opts)))}"
                L.append(f"| {cid} | {dim} | {'<br>'.join(cells)} | {status} |")
        L += ["", f"Prompt: **{cfg['prompt_version']}**. Caller IDs: " + ", ".join(
            f"{d} {s:.0%} ({(ws.get('dids') or {}).get(d, {}).get('state', '?')})"
            for d, s in dist(cfg, 'default', 'caller_id').items()) +
            (". Not in use: " + ", ".join(f"{d} ({r['state']})" for d, r in (ws.get("dids") or {}).items()
                                           if d not in dist(cfg, 'default', 'caller_id')) if ws.get("dids") else ""), ""]
    # ---- incidents
    inc = ws.get("incident")
    if inc:
        L += ["### Incidents", "",
              f"- Hallucination on {inc['prompt']} detected {fmt_minute(inc['opened'])}: {inc['rate']:.1%} flagged vs "
              f"{inc['baseline']:.1%} normal — **{inc['status']}**" +
              (f" (verified {fmt_minute(inc['verified_at'])}: {inc['after']['rate']:.1%} on {inc['after']['prompt']})"
               if inc.get("after") else ""), ""]
    # ---- what changed
    since = max(ctx.now - 1440, 0)
    L += [f"### What changed in the last 24h (since {fmt_minute(since)})", ""]
    rows = [d for d in st.decisions(workflow_id=wid, since=since, until=ctx.now, limit=2000)
            if d["kind"] in ("applied", "supervisor_edit", "decay_detected", "incident_opened", "incident_verified",
                             "did_health", "approval_rejected", "approval_expired", "stale")]
    if not rows:
        L.append("No changes.")
    for d in rows:
        L.append(f"- {fmt_minute(d['sim_minute'])} · {d['kind']} · {d['summary']}")
    # ---- compliance: changed without asking (policy-permitted auto-apply), with reasons
    auto = [p for p in st.proposals(workflow_id=wid, statuses=("auto_applied",))]
    L += ["", "### Changed without asking (within the signed auto-apply policy)", ""]
    if not auto:
        L.append("None. Every change on this workflow went through an approval.")
    # consecutive max_shift steps of the same change read as one change
    groups: list[list[dict]] = []
    for p in auto:
        key = (p["kind"], p["body"]["change_summary"].split(" (step of")[0])
        if groups and groups[-1][0]["_key"] == key and p["created_minute"] - groups[-1][-1]["created_minute"] <= 60:
            groups[-1].append({**p, "_key": key})
        else:
            groups.append([{**p, "_key": key}])
    for g in groups:
        p0, p1 = g[0], g[-1]
        when = fmt_minute(p0["created_minute"]) + (f"–{fmt_minute(p1['created_minute'])[-5:]}" if len(g) > 1 else "")
        ver = f"v{p0['applied_version']}" + (f"–v{p1['applied_version']}" if len(g) > 1 else "")
        steps = f" ({len(g)} steps; policy max_shift)" if len(g) > 1 else ""
        L.append(f"- {when} · {ver} · {', '.join(p0['dims'])} · {p0['_key'][1]}{steps}  \n"
                 f"  _why_: {p1['body']['rationale'][:300]}")
    L.append("")
    return L


def supervisor_view(state_dir, wid=None, at=None) -> str:
    ctx = _ctx(state_dir, at)
    L = [f"# Supervisor view — {ctx.store.get('scenario')}", ""]
    for w in ([wid] if wid else ctx.wfs):
        L += workflow_view(ctx, w)
    return "\n".join(L)


# ------------------------------------------------------------------ on-call / loop health (C14)
def health(state_dir) -> str:
    sd = Path(state_dir)
    st = Store(sd / "controller.db")
    m = lambda k, n=48: [v for (v,) in st.db.execute(
        "SELECT value FROM metrics WHERE key=? ORDER BY sim_minute DESC LIMIT ?", (k, n))]
    now = st.db.execute("SELECT MAX(sim_minute) FROM metrics").fetchone()[0] or 0
    cyc, lag, eps = m("cycle_seconds"), m("event_lag_minutes", 1), m("ingest_events_per_s")
    cred = st.db.execute("SELECT sim_minute, value FROM metrics WHERE key='credits_remaining' ORDER BY sim_minute").fetchall()
    burn_day = 0.0
    if len(cred) > 1:
        recent = [c for c in cred if c[0] >= now - 1440]
        burn_day = recent[0][1] - recent[-1][1] if recent else 0.0
    left = cred[-1][1] if cred else None
    dec = st.db.execute("SELECT COUNT(*) FROM decisions WHERE sim_minute >= ?", (now - 1440,)).fetchone()[0]
    pend = st.db.execute("SELECT MIN(created_minute), COUNT(*) FROM proposals WHERE status IN ('pending','approved')").fetchone()
    by = dict(st.db.execute("SELECT status, COUNT(*) FROM proposals GROUP BY status").fetchall())
    alerts = st.db.execute("SELECT sim_minute, severity, workflow_id, code, message, resolved_minute FROM alerts "
                           "ORDER BY resolved_minute IS NOT NULL, sim_minute DESC LIMIT 30").fetchall()
    L = [f"# Loop health — {st.get('scenario')} at {fmt_minute(now)}", "",
         "| signal | value |", "|---|---|",
         f"| decision lag per cycle (last 24h), median / max | {np.median(cyc):.2f}s / {max(cyc):.2f}s |" if cyc else "| decision lag | — |",
         f"| ingest rate (last 24h median) | {np.median(eps):,.0f} events/s |" if eps else "| ingest rate | — |",
         f"| freshest call event vs sim clock | {lag[0]:.0f} sim-minutes |" if lag else "| event lag | — |",
         f"| decisions logged, last 24h | {dec} |",
         f"| credits left / burned last 24h | {left} / {burn_day:.0f}" + (f" (runway {left / burn_day:.1f} days)" if burn_day else "") + " |",
         f"| approvals in flight / oldest | {pend[1]}" + (f" / since {fmt_minute(pend[0])}" if pend[0] is not None else "") + " |",
         f"| proposal outcomes | " + ", ".join(f"{k} {v}" for k, v in sorted(by.items())) + " |", "",
         "## Alerts (open first)", "", "| when | severity | workflow | code | message | resolved |", "|---|---|---|---|---|---|"]
    for a in alerts:
        L.append(f"| {fmt_minute(a[0])} | **{a[1]}** | {a[2] or '-'} | {a[3]} | {a[4]} | "
                 f"{fmt_minute(a[5]) if a[5] is not None else 'OPEN'} |")
    if not alerts:
        L.append("| — | — | — | — | no alerts | — |")
    return "\n".join(L)


def to_html(md: str, title: str) -> str:
    """Tiny markdown-ish renderer (headings, tables, bullets) so the view is a file anyone can open."""
    out, in_table = [], False
    for line in md.splitlines():
        esc = html.escape(line).replace("&lt;br&gt;", "<br>")
        esc = __import__("re").sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc)
        if line.startswith("|"):
            cells = [c.strip() for c in esc.strip("|").split("|")]
            if set("".join(cells)) <= set("-: "):
                continue
            if not in_table:
                out.append("<table>")
                in_table = True
                out.append("<tr>" + "".join(f"<th>{c}</th>" for c in cells) + "</tr>")
            else:
                out.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
            continue
        if in_table:
            out.append("</table>")
            in_table = False
        if line.startswith("#"):
            n = len(line) - len(line.lstrip("#"))
            out.append(f"<h{n}>{esc[n:].strip()}</h{n}>")
        elif line.startswith("- "):
            out.append(f"<li>{esc[2:]}</li>")
        elif line.strip():
            out.append(f"<p>{esc}</p>")
    if in_table:
        out.append("</table>")
    css = ("body{font:14px/1.45 system-ui,sans-serif;max-width:1100px;margin:24px auto;padding:0 16px;color:#1d2433;"
           "background:#fff}table{border-collapse:collapse;margin:8px 0 16px;width:100%}td,th{border:1px solid #d8dde6;"
           "padding:4px 8px;text-align:left;vertical-align:top;font-size:13px}th{background:#f3f5f9}h2{margin-top:36px;"
           "border-top:2px solid #1d2433;padding-top:12px}li{margin:2px 0}"
           "@media (prefers-color-scheme:dark){body{background:#12151c;color:#e3e7ef}th{background:#1d2230}"
           "td,th{border-color:#2d3446}h2{border-color:#e3e7ef}}")
    return (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,"
            f"initial-scale=1'><title>{html.escape(title)}</title><style>{css}</style></head><body>"
            + "\n".join(out) + "</body></html>")

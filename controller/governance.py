"""Governance client (C9): the only code that changes a workflow's config.

Planners never call the simulator's mutation endpoints. They emit `Draft`s (a patch + evidence) into
`ctl.drafts[wid]` during a cycle; at the end of the cycle this module

  1. reconciles every in-flight proposal with the simulator (crash recovery and normal polling are the same
     code path),
  2. merges the cycle's drafts per workflow into ONE batched change,
  3. routes it: policy-permitted auto-apply for operational / risk-reducing changes, otherwise one approval
     request, timed to the approvers' hours and inside our own request budget,
  4. writes the proposal row BEFORE the HTTP call (outbox), so a crash between "decide" and "send" is
     recovered by looking the proposal_id up instead of sending a second one.

Proposal state machine (proposals.status):

    intent ──send──▶ pending ──▶ approved ──apply──▶ applied
      │                 │           │  └─(base moved)─▶ stale      (re-planned from the new base)
      │                 │           └─(apply window)──▶ lapsed     (re-planned)
      │                 ├──▶ rejected                               (re-planned next cycle, within budget)
      │                 └──▶ expired   == rejection (Product): re-planned with fresh data, never resubmitted
      ├─(409 stale at submit)──▶ stale_at_submit
      └─(auto route)──▶ auto_applied | auto_rejected
"""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field

import numpy as np

from . import settings as S
from .aggregates import NCODE, cells_where
from .cfg import (AGENT_DIMS, OPERATIONAL, changed_dims, clean, cohort_ids, config_hash, diff, fmt_minute,
                  max_shift, parse_when, step_toward)
from .client import SimError, TransportError

log = logging.getLogger("controller.governance")

IN_FLIGHT = ("intent", "pending", "approved")
REACHED_APPROVER = ("pending", "approved", "applied", "rejected", "expired", "lapsed", "stale")


@dataclass
class Draft:
    wid: str
    kind: str                 # e.g. prompt_revert, did_rotation, variant_allocation, schedule, cohorts
    summary: str              # one line a supervisor understands
    patch: dict               # partial config; see apply_patch
    rationale: str
    dry_run: dict
    lift: float = 0.0         # expected relative lift in leads concluded (business outcome), conservative.
                              # KPIs are still reported separately in dry_run; this only ranks proposals.
    urgent: bool = False      # incident / rollback: may use the reserved request and skip routine timing
    risk_reducing: bool = False  # moves traffic AWAY from something measured as bad
    evidence: dict | None = None
    meta: dict = field(default_factory=dict)


def apply_patch(cfg: dict, patch: dict) -> dict:
    """Patch semantics: `cohorts` replaces the list (and drops allocation/hour tables of removed cohorts);
    `allocations.{cohort}.{dim}` replaces that distribution; `prompt_version` replaces;
    `schedule.hour_weights` replaces the whole table set."""
    out = copy.deepcopy(cfg)
    if "cohorts" in patch:
        out["cohorts"] = copy.deepcopy(patch["cohorts"])
        keep = set(cohort_ids(out))
        out["allocations"] = {k: v for k, v in out.get("allocations", {}).items() if k in keep}
        hw = out.setdefault("schedule", {}).setdefault("hour_weights", {})
        for k in list(hw):
            if k not in keep:
                del hw[k]
    for k, dims in (patch.get("allocations") or {}).items():
        for d, w in dims.items():
            out.setdefault("allocations", {}).setdefault(k, {})[d] = dict(w)
    if "prompt_version" in patch:
        out["prompt_version"] = patch["prompt_version"]
    if "schedule" in patch and "hour_weights" in patch["schedule"]:
        out.setdefault("schedule", {})["hour_weights"] = copy.deepcopy(patch["schedule"]["hour_weights"])
    return clean(out)


def in_hours(minute: int, hours: list[int], start_offset: float = 0, end_offset: float = 0) -> bool:
    h = (minute % 1440) / 60
    return hours[0] + start_offset <= h < hours[1] - end_offset


class Governance:
    def __init__(self, ctl):
        self.ctl = ctl

    # ================================================================== reconcile
    def tick(self, ctl):
        self.reconcile(ctl)
        for wid in ctl.wfs:
            drafts = ctl.drafts.get(wid) or []
            if drafts:
                self.route(ctl, wid, drafts)

    def reconcile(self, ctl):
        st = ctl.store
        for p in st.proposals(statuses=IN_FLIGHT):
            wid, pid = p["workflow_id"], p["proposal_id"]
            if p["status"] == "intent":
                self._recover_intent(ctl, p)
                continue
            if p["status"] == "pending":
                try:
                    a = ctl.sim.approval(p["approval_id"])
                except SimError as e:
                    log.warning("approval %s lookup failed: %s", p["approval_id"], e)
                    continue
                if a["status"] == "pending":
                    continue
                with st.tx():
                    st.update_proposal(pid, ctl.now, status=a["status"] if a["status"] != "lapsed" else "lapsed",
                                       reasons=a.get("reasons") or [])
                    st.decide(ctl.now, ctl.ts, wid, f"approval_{a['status']}",
                              f"{a['approval_id']} {a['status']}" + (f": {'; '.join(a.get('reasons') or [])}"
                                                                      if a.get("reasons") else ""),
                              {"decided_at": a.get("decided_at"), "apply_by": a.get("apply_by"),
                               "reasons": a.get("reasons")}, proposal_id=pid, approval_id=a["approval_id"])
                p["status"] = a["status"]
                p["apply_by"] = a.get("apply_by")
            if p["status"] == "approved":
                self._apply(ctl, p)

    def _recover_intent(self, ctl, p: dict):
        """We wrote the intent but don't know whether the request reached the simulator."""
        st, wid, pid = ctl.store, p["workflow_id"], p["proposal_id"]
        if p["route"] == "approval":
            found = next((a for a in ctl.sim.approvals(workflow_id=wid) if a["proposal_id"] == pid), None)
            if found:
                with st.tx():
                    st.update_proposal(pid, ctl.now, status="pending", approval_id=found["approval_id"])
                    st.decide(ctl.now, ctl.ts, wid, "reconciled", f"recovered in-flight request {found['approval_id']}",
                              proposal_id=pid, approval_id=found["approval_id"])
                return
        else:
            hist = ctl.sim.config_history(wid)
            v = next((v for v in hist if v.get("proposal_id") == pid), None)
            if v:
                with st.tx():
                    st.update_proposal(pid, ctl.now, status="auto_applied", applied_version=v["version"])
                    st.decide(ctl.now, ctl.ts, wid, "reconciled", f"recovered auto-apply -> v{v['version']}",
                              proposal_id=pid, config_version=v["version"])
                return
        # never reached the simulator: send it now if its base is still current, else drop it
        if ctl.current[wid]["version"] != p["base_version"]:
            with st.tx():
                st.update_proposal(pid, ctl.now, status="stale_at_submit")
                st.decide(ctl.now, ctl.ts, wid, "stale", "unsent proposal dropped: config moved on", proposal_id=pid)
            return
        self._send(ctl, p)

    def _apply(self, ctl, p: dict):
        st, wid, pid, aid = ctl.store, p["workflow_id"], p["proposal_id"], p["approval_id"]
        if ctl.current[wid]["version"] != p["base_version"]:
            # A supervisor (or a safety auto-apply) changed the config after we proposed. Applying would 409 and,
            # worse, the approver approved a diff against a config that no longer exists. Re-plan instead.
            with st.tx():
                st.update_proposal(pid, ctl.now, status="stale")
                st.decide(ctl.now, ctl.ts, wid, "stale",
                          f"approved {aid} not applied: base v{p['base_version']} superseded by "
                          f"v{ctl.current[wid]['version']} ({ctl.current[wid]['actor']}); will re-propose",
                          proposal_id=pid, approval_id=aid)
            return
        try:
            r = ctl.sim.apply(wid, aid)
        except SimError as e:
            status = {410: "lapsed"}.get(e.status, "stale" if e.code == "stale_base_version" else None)
            with st.tx():
                if status:
                    st.update_proposal(pid, ctl.now, status=status, reasons=[str(e.detail)])
                st.decide(ctl.now, ctl.ts, wid, f"apply_{status or 'error'}", f"apply {aid} failed: {e}",
                          proposal_id=pid, approval_id=aid)
            return
        except TransportError:
            # Unknown outcome; next cycle's config sync + approval lookup ('applied') tells us.
            r = None
            a = ctl.sim.approval(aid)
            if a["status"] != "applied":
                return
            r = {"version": ctl.sim.config(wid)["version"], "changed_dims": a["changed_dims"]}
        self._mark_applied(ctl, p, r["version"], "controller_approved")

    def _mark_applied(self, ctl, p: dict, version: int, actor: str):
        st, wid, pid = ctl.store, p["workflow_id"], p["proposal_id"]
        cfg = p["body"]["proposed_config"]
        with st.tx():
            st.update_proposal(pid, ctl.now, status="applied" if actor == "controller_approved" else "auto_applied",
                               applied_version=version)
            st.record_config(wid, {"version": version, "actor": actor, "changed_dims": p["dims"],
                                   "applied_at": ctl.ts, "proposal_id": pid, "approval_id": p.get("approval_id")},
                             cfg, ctl.now)
            st.decide(ctl.now, ctl.ts, wid, "applied",
                      f"v{version} live ({actor}): {p['body']['change_summary']}",
                      {"dims": p["dims"], "kinds": p["meta"].get("kinds"), "verify_after": ctl.now + S.VERIFY_AFTER_HOURS * 60},
                      proposal_id=pid, approval_id=p.get("approval_id"), config_version=version)
            ws = st.wf_state(wid)
            ws.setdefault("applied", []).append({"proposal_id": pid, "version": version, "minute": ctl.now,
                                                 "dims": p["dims"], "kinds": p["meta"].get("kinds"),
                                                 "verified": False})
            st.save_wf_state(wid, ws)
        ctl.current[wid] = {"workflow_id": wid, "version": version, "actor": actor, "config": cfg}

    # ================================================================== routing
    def route(self, ctl, wid: str, drafts: list[Draft]):
        wf = ctl.wfs[wid]
        gov = wf["governance"]
        pol = gov.get("auto_apply") or {}
        cur = ctl.current[wid]
        in_flight = ctl.store.proposals(workflow_id=wid, statuses=("intent", "pending", "approved"))
        approval_in_flight = [p for p in in_flight if p["route"] == "approval"]
        kinds_in_flight = {k for p in approval_in_flight for k in (p["meta"].get("kinds") or [])}
        # If the boundary refused one of our auto-applies recently, our pre-check is wrong somewhere: stop
        # retrying the same refused change and take the approval path for a while instead.
        auto_blocked = any(p["route"] == "auto" and p["status"] == "auto_rejected" and p["updated_minute"] > ctl.now - 120
                           for p in ctl.store.proposals(workflow_id=wid, statuses=("auto_rejected",)))

        auto, manual = [], []
        for d in drafts:
            dims = changed_dims(cur["config"], apply_patch(cur["config"], d.patch))
            if not dims:
                continue
            allowed = pol.get("enabled") and set(dims) <= set(pol.get("allowed_dims") or []) and not auto_blocked
            # Our own rule (stricter than policy): auto-apply only operational dims, or moves that reduce
            # exposure to something measured as bad. Anything that introduces new customer-facing exposure asks.
            voluntary_ok = set(dims) <= OPERATIONAL or d.risk_reducing
            (auto if allowed and voluntary_ok else manual).append(d)

        # Don't let a routine auto-apply invalidate an approval that's waiting (it would go stale).
        if approval_in_flight:
            auto = [d for d in auto if d.urgent]
        if auto:
            self._auto(ctl, wid, auto, pol)
            cur = ctl.current[wid]
        if manual:
            urgent = [d for d in manual if d.urgent and d.kind not in kinds_in_flight]
            routine = [d for d in manual if not d.urgent]
            if urgent:
                self._request(ctl, wid, urgent + routine, urgent=True)
            elif routine and not approval_in_flight:
                self._request(ctl, wid, routine, urgent=False)

    def _auto(self, ctl, wid: str, drafts: list[Draft], pol: dict):
        cur = ctl.current[wid]
        new = cur["config"]
        for d in drafts:
            new = apply_patch(new, d.patch)
        sh = max_shift(cur["config"], new)
        limit = float(pol.get("max_shift", 0))
        stepped = False
        if sh > limit + 1e-9:
            # Respect max_shift as a rate limit: take one step now, the planner re-emits the target next cycle.
            # Margin under the limit because weights are rounded to 4 dp and the boundary's check is exact.
            margin = 0.002
            while True:
                stepped_cfg = clean(step_toward(cur["config"], new, cohort_ids(new), AGENT_DIMS, limit - margin))
                if max_shift(cur["config"], stepped_cfg) <= limit - 1e-6 or margin > limit / 2:
                    break
                margin *= 2
            new, stepped = stepped_cfg, True
        dims = changed_dims(cur["config"], new)
        if not dims:
            return
        body = self._body(ctl, wid, cur, new, drafts, route="auto")
        if stepped:
            body["change_summary"] += f" (step of {limit:.2f} toward target; policy max_shift)"
        self._submit(ctl, wid, "auto", cur, new, drafts, body)

    def _request(self, ctl, wid: str, drafts: list[Draft], urgent: bool):
        wf = ctl.wfs[wid]
        gov = wf["governance"]
        hours = gov["approver_hours"]
        if urgent:
            if not in_hours(ctl.now, hours):
                return  # queue at approvers' start rather than at night (Compliance memo)
        elif not in_hours(ctl.now, hours, S.ROUTINE_START_OFFSET_H, S.ROUTINE_LAST_SEND_BEFORE_END_H):
            return
        sent = [p for p in ctl.store.proposals(workflow_id=wid, statuses=REACHED_APPROVER + ("intent",))
                if p["route"] == "approval" and p["created_minute"] > ctl.now - 1440]
        cap = gov["max_requests_per_24h"]
        budget = cap if urgent else min(cap - 1 if cap >= 3 else cap, S.ROUTINE_REQUESTS_PER_24H)
        if len(sent) >= budget:
            with ctl.store.tx():
                ctl.store.decide(ctl.now, ctl.ts, wid, "deferred",
                                 f"{'urgent' if urgent else 'routine'} request deferred: {len(sent)}/{budget} "
                                 f"requests used in 24h", {"kinds": [d.kind for d in drafts]})
            return
        cur = ctl.current[wid]
        new = cur["config"]
        for d in drafts:
            new = apply_patch(new, d.patch)
        if not changed_dims(cur["config"], new):
            return
        body = self._body(ctl, wid, cur, new, drafts, route="approval")
        self._submit(ctl, wid, "approval", cur, new, drafts, body, urgent=urgent)

    # ================================================================== proposal content (C10)
    def _body(self, ctl, wid, cur, new, drafts, route):
        dims = changed_dims(cur["config"], new)
        kinds = [d.kind for d in drafts]
        lift = sum(max(d.lift, 0.0) for d in drafts)
        impact = min(1.0, lift / 0.10)  # 1.0 == expected +10% leads concluded (see SUBMISSION §6)
        if any(d.urgent for d in drafts):
            impact = max(impact, 0.9)  # incident fixes go to the top of a supervisor's queue
        summary = " + ".join(d.summary for d in drafts)
        if len(drafts) > 1:
            summary = f"{len(drafts)} changes in one request: {summary}"
        evidence = {}
        for d in drafts:
            evidence.update(d.evidence or {})
        body = {
            "workflow_id": wid, "base_version": cur["version"], "proposed_config": new,
            "change_summary": summary[:500],
            "diff": diff(cur["config"], new) or {"dims": dims},
            "affected_leads": self.affected_leads(ctl, wid, cur["config"], new),
            "dry_run": {d.kind: d.dry_run for d in drafts},
            "impact_score": round(float(impact), 3),
            "rationale": " | ".join(f"[{d.kind}] {d.rationale}" for d in drafts),
        }
        if evidence:
            body["evidence"] = evidence
        if route == "approval":
            body["expires_in_hours"] = ctl.wfs[wid]["governance"]["proposal_ttl_hours"]
        return body

    def affected_leads(self, ctl, wid: str, old: dict, new: dict) -> int:
        """Leads still active (uploaded within the cadence horizon and not concluded) whose experience changes:
        every active lead for workflow-wide dims (prompt, cohort re-partition), else only leads in cohorts
        whose allocation or hour table changed. Counted from our own lead/conclusion cubes."""
        w = ctl.agg.wf[wid]
        horizon = len(ctl.wfs[wid]["cadence"])
        today = ctl.now // 1440
        up = np.zeros(NCODE)
        for d in range(today - horizon + 1, today + 1):
            if d in w.leads:
                up += w.leads[d]
            if d in w.concl:
                up -= w.concl[d]
        up = np.maximum(up, 0)
        dims = changed_dims(old, new)
        if {"prompt_version", "cohorts"} & set(dims):
            return int(up.sum())
        mask = np.zeros(NCODE, dtype=bool)
        unassigned = np.ones(NCODE, dtype=bool)
        hw_old = (old.get("schedule") or {}).get("hour_weights") or {}
        hw_new = (new.get("schedule") or {}).get("hour_weights") or {}
        cohorts = list(new.get("cohorts") or []) + [{"id": "default", "where": {}}]
        from .cfg import dist
        for c in cohorts:
            m = unassigned & cells_where(c["where"])
            unassigned &= ~m
            ch = any(dist(old, c["id"] if c["id"] in cohort_ids(old) else "default", d) != dist(new, c["id"], d)
                     for d in AGENT_DIMS)
            ch |= (hw_old.get(c["id"]) or hw_old.get("default")) != (hw_new.get(c["id"]) or hw_new.get("default"))
            if ch:
                mask |= m
        return int(up[mask].sum())

    # ================================================================== send
    def _submit(self, ctl, wid, route, cur, new, drafts, body, urgent=False):
        kinds = sorted({d.kind for d in drafts})
        pid = f"{wid}.{fmt_minute(ctl.now).replace(' ', 'T').replace(':', '')}.{'-'.join(kinds)}.{config_hash(new)}"
        if ctl.store.proposal(pid):
            return  # same plan at the same minute already recorded (restart replay)
        body["proposal_id"] = pid
        p = {"proposal_id": pid, "workflow_id": wid, "kind": "+".join(kinds), "route": route, "status": "intent",
             "base_version": cur["version"], "dims": changed_dims(cur["config"], new), "body": body,
             "created_minute": ctl.now,
             "meta": {"kinds": kinds, "urgent": urgent, "lift": sum(d.lift for d in drafts),
                      "drafts": [{"kind": d.kind, **d.meta} for d in drafts]}}
        with ctl.store.tx():
            ctl.store.add_proposal(p)
            ctl.store.decide(ctl.now, ctl.ts, wid, "proposed",
                             f"[{route}{', urgent' if urgent else ''}] {body['change_summary']}",
                             {"dims": p["dims"], "impact_score": body["impact_score"],
                              "affected_leads": body["affected_leads"], "rationale": body["rationale"],
                              "diff": body["diff"], "dry_run": body["dry_run"], "evidence": body.get("evidence")},
                             proposal_id=pid)
        self._send(ctl, ctl.store.proposal(pid))

    def _send(self, ctl, p: dict):
        st, wid, pid, body = ctl.store, p["workflow_id"], p["proposal_id"], p["body"]
        try:
            if p["route"] == "approval":
                a = ctl.sim.request_approval(body)
                with st.tx():
                    st.update_proposal(pid, ctl.now, status="pending", approval_id=a["approval_id"])
                    st.decide(ctl.now, ctl.ts, wid, "requested",
                              f"approval {a['approval_id']} requested, expires {a['expires_at'][5:16]}",
                              {"expires_at": a["expires_at"]}, proposal_id=pid, approval_id=a["approval_id"])
            else:
                body = {k: body[k] for k in ("proposal_id", "base_version", "proposed_config", "rationale")} | (
                    {"evidence": body["evidence"]} if body.get("evidence") else {})
                r = ctl.sim.auto_apply(wid, body)
                self._mark_applied(ctl, p, r["version"], "controller_auto")
        except TransportError:
            return  # stays 'intent'; reconcile next cycle looks it up by proposal_id
        except SimError as e:
            if e.code == "duplicate_proposal_id":
                self._recover_intent(ctl, p)
                return
            status = {"stale_base_version": "stale_at_submit", "auto_apply_not_permitted": "auto_rejected",
                      "out_of_credits": "failed"}.get(e.code, "invalid")
            with st.tx():
                st.update_proposal(pid, ctl.now, status=status, reasons=[str(e.detail)])
                st.decide(ctl.now, ctl.ts, wid, status, f"{p['route']} submit refused: {e}", proposal_id=pid)
                if status in ("auto_rejected", "invalid"):
                    # Our pre-check disagreed with the boundary: that's a bug worth a look, not a page.
                    st.alert(ctl.now, "ticket", wid, f"governance_{status}", str(e)[:300])


def applied_at_minute(v: dict) -> int | None:
    return parse_when(v["applied_at"]) if v.get("applied_at") else None

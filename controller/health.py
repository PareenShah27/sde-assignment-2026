"""Loop health rules (C14). The controller watches itself; alerts land in the `alerts` table with a severity:

  page    a human must act now; the message says what to look at.
  notify  business hours (Slack / email to the account team or the client supervisor).
  ticket  a bug in our assumptions, for the owning engineer's queue.

Paging rules (and why each is actionable):
  hallucination_unfixed  an incident detected >= 3h ago is still live (approval rejected, out of budget, or no
                         safe prompt). Look at: `controller why --wf X`, the approval queue, credits.
  credits_low            fewer than CREDIT_RESERVE credits: the next incident can't be validated.
  ingest_stalled         inside a dialling window, no new call events for >= 60 sim-minutes: we are deciding
                         on stale data. Look at: the event source / chunk endpoint, our ingest error log.
  loop_slow              one cycle took longer than LOOP_SLOW_SECONDS: we can't keep up with real time.
Not paged on purpose: a judgement-call rejection, an expired request (re-proposed with fresh data), exploration
results, approver latency, a caller ID rotated out (we already fixed it; the client hears about it in the view).
"""
from __future__ import annotations

from . import settings as S

INCIDENT_PAGE_AFTER_MIN = 180
INGEST_STALL_MIN = 60
LOOP_SLOW_SECONDS = 600  # a 30-minute cycle has 30 minutes of budget in production; page at a third of it


class LoopHealth:
    def tick(self, ctl):
        st = ctl.store
        with st.tx():
            for wid in ctl.wfs:
                inc = st.wf_state(wid).get("incident")
                code = "hallucination_unfixed"
                if inc and inc.get("status") == "open" and ctl.now - inc["opened"] >= INCIDENT_PAGE_AFTER_MIN:
                    why = [p["status"] + (f" ({'; '.join(p['reasons'] or [])})" if p.get("reasons") else "")
                           for p in st.proposals(workflow_id=wid) if "prompt_fix" in (p["meta"].get("kinds") or [])]
                    st.alert(ctl.now, "page", wid, code,
                             f"{wid}: hallucination on {inc['prompt']} still live {(ctl.now - inc['opened']) / 60:.1f}h after "
                             f"detection. Fix requests: {', '.join(why) or 'none sent (no safe candidate or no budget)'}. "
                             f"Look at `controller why --wf {wid}` and the approval queue.")
                else:
                    st.resolve_alert(ctl.now, wid, code)
            if ctl.credits < S.CREDIT_RESERVE:
                st.alert(ctl.now, "page", None, "credits_low",
                         f"{ctl.credits:.0f} credits left (< {S.CREDIT_RESERVE}): the next incident cannot be validated. "
                         f"Top up or approve a manual prompt revert.")
            else:
                st.resolve_alert(ctl.now, None, "credits_low")
            h = (ctl.now % 1440) // 60
            dialling = [wid for wid, wf in ctl.wfs.items() if wf["dialling_window"][0] + 1 <= h < wf["dialling_window"][1]]
            stalled = [wid for wid in dialling if ctl.agg.wf[wid].last_call_minute is not None
                       and ctl.now - ctl.agg.wf[wid].last_call_minute >= INGEST_STALL_MIN]
            if stalled:
                st.alert(ctl.now, "page", None, "ingest_stalled",
                         f"no call events for >= {INGEST_STALL_MIN} sim-min on {', '.join(stalled)} during dialling hours; "
                         f"decisions are running on stale data. Check the chunk endpoint and controller ingest log.")
            else:
                st.resolve_alert(ctl.now, None, "ingest_stalled")
            last = st.db.execute("SELECT value FROM metrics WHERE key='cycle_seconds' ORDER BY sim_minute DESC LIMIT 1").fetchone()
            if last and last[0] > LOOP_SLOW_SECONDS:
                st.alert(ctl.now, "page", None, "loop_slow",
                         f"last cycle took {last[0]:.0f}s (> {LOOP_SLOW_SECONDS}s). See `controller health` module timings.")
            else:
                st.resolve_alert(ctl.now, None, "loop_slow")
            for p in st.proposals(statuses=("expired", "lapsed")):
                if p["updated_minute"] >= ctl.now - S.STEP_MINUTES:
                    st.alert(ctl.now, "notify", p["workflow_id"], f"{p['status']}:{p['proposal_id']}",
                             f"{p['workflow_id']}: request {p.get('approval_id')} {p['status']}; it will be re-planned with "
                             f"fresh data (not resubmitted).")

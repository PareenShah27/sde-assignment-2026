"""Incident response (C8) and supervisor-edit awareness.

Hallucination incident
----------------------
Detect   flag rate of the CURRENT prompt over the last HAL_WINDOW_HOURS vs the workflow's baseline flag rate
         (pooled over prompts with a clean live record). Fires only on a population: >= HAL_MIN_CONNECTED
         connected calls, rate >= HAL_RATIO x baseline, binomial tail p < HAL_P. The QA detector is noisy
         (false positives and negatives), which is why we test rates, not individual flags.
Choose   1) a prompt with a clean LIVE record on this workflow (usually the one the supervisor replaced):
            real evidence about both safety and engagement, and it needs no LLM call;
         2) only if none exists: ask the LLM for candidates (3 credits) and validate them; never-live prompts
            must clear our stricter STRICT_PASS_RATE, not just the policy bar.
Validate one validation run per (workflow, prompt), cached and reused as evidence.
Propose  urgent draft -> governance batches it and sends at the first approver-hour slot.
Verify   after apply, the new prompt's flag rate over the next window must be back near baseline; the result is
         written to the audit log ("what happened next").

Engagement collapse after a supervisor prompt edit (the Kestrel memo's "compliance-script" failure mode):
a revert is PROPOSED (never auto-applied) when engagement under the new prompt is >8% below the previous
prompt with z > 3 on >= 400 connected calls each side.
"""
from __future__ import annotations

import logging
import math

import numpy as np

from . import settings as S
from .cfg import fmt_minute, parse_when
from .client import SimError
from .governance import Draft
from .stats import binom_sf, two_prop_z

log = logging.getLogger("controller.incidents")


def _hal_window(w, pi: int, m0: int, m1: int) -> tuple[int, int]:
    """(connected, flags) for prompt index pi in sim minutes [m0, m1)."""
    conn = flags = 0
    for d in range(m0 // 1440, (m1 - 1) // 1440 + 1):
        H = w.hal.get(d)
        if H is None:
            continue
        for h in range(24):
            t = d * 1440 + h * 60
            if m0 <= t < m1 or (t < m0 < t + 60):
                conn += int(H[h, pi, 0])
                flags += int(H[h, pi, 1])
    return conn, flags


def prompt_record(w, pi: int, upto_day: int) -> dict:
    """Live record of prompt index pi on this workflow: connected, flags, engaged (all days <= upto_day)."""
    c = f = e = 0
    for d, E in w.eng.items():
        if d <= upto_day:
            c += int(E[:, :, :, pi, 0].sum())
            e += int(E[:, :, :, pi, 1].sum())
            f += int(E[:, :, :, pi, 2].sum())
    return {"connected": c, "flags": f, "engaged": e}


class SupervisorWatch:
    """Records supervisor edits and protects the edited dims from routine changes for 24h."""

    def tick(self, ctl):
        for wid, versions in (ctl.new_versions or {}).items():
            for v in versions:
                if v["actor"] != "supervisor":
                    continue
                ws = ctl.store.wf_state(wid)
                if v["version"] in ws.get("seen_supervisor_versions", []):
                    continue
                prot = ws.setdefault("protected", {})
                at = parse_when(v["applied_at"]) if v.get("applied_at") else ctl.now
                for d in v["changed_dims"]:
                    prot[d] = at + 24 * 60
                ws.setdefault("seen_supervisor_versions", []).append(v["version"])
                with ctl.store.tx():
                    ctl.store.save_wf_state(wid, ws)
                    ctl.store.decide(ctl.now, ctl.ts, wid, "supervisor_edit",
                                     f"supervisor edit v{v['version']} ({', '.join(v['changed_dims'])}) respected; "
                                     f"those dims are frozen for routine changes until {fmt_minute(at + 1440)}",
                                     {"changed_dims": v["changed_dims"]}, config_version=v["version"])


def protected(ctl, wid: str, dim: str) -> bool:
    return ctl.store.wf_state(wid).get("protected", {}).get(dim, -1) > ctl.now


class Hallucination:
    def tick(self, ctl):
        for wid in ctl.wfs:
            self._wf(ctl, wid)

    # ------------------------------------------------------------------
    def baseline(self, ctl, wid: str, exclude: set[str]) -> tuple[float, int]:
        """Pooled flag rate over prompts with a live record (excluding suspects). Falls back to a prior."""
        w = ctl.agg.wf[wid]
        bad = set(ctl.store.wf_state(wid).get("bad_prompts", []))
        c = f = 0
        for i, pv in enumerate(w.opts["prompt_version"]):
            if pv in exclude or pv in bad:
                continue
            r = prompt_record(w, i, ctl.now // 1440)
            c += r["connected"]
            f += r["flags"]
        if c < 500:
            return 0.02, c  # conservative prior until we have our own data
        return max(f / c, 0.002), c

    def _wf(self, ctl, wid: str):
        st = ctl.store
        w = ctl.agg.wf[wid]
        cfg = ctl.current[wid]["config"]
        pv = cfg["prompt_version"]
        pi = w.ix["prompt_version"][pv]
        ws = st.wf_state(wid)
        # track when the current prompt went live
        if ws.get("prompt") != pv:
            ws["prev_prompt"], ws["prompt"] = ws.get("prompt"), pv
            at = ctl.current[wid].get("applied_at")
            ws["prompt_since"] = (parse_when(at) if at else ctl.now) if ws.get("prev_prompt") else -10**9
            ws["prompt_actor"] = ctl.current[wid]["actor"]
            with st.tx():
                st.save_wf_state(wid, ws)
        inc = ws.get("incident")
        m0 = max(ctl.now - S.HAL_WINDOW_HOURS * 60, ws.get("prompt_since", -10**9))
        conn, flags = _hal_window(w, pi, m0, ctl.now)
        base, base_n = self.baseline(ctl, wid, exclude={pv})

        if inc and inc.get("status") == "open":
            if inc["prompt"] != pv:
                self._verify(ctl, wid, ws, inc, pv, conn, flags, inc["baseline"])
            else:
                self._respond(ctl, wid, ws, inc)
            return
        if conn < S.HAL_MIN_CONNECTED:
            self._engagement_collapse(ctl, wid, ws)
            return
        rate = flags / conn
        p = binom_sf(flags, conn, base)
        if rate >= S.HAL_RATIO * base and p < S.HAL_P:
            inc = {"status": "open", "prompt": pv, "opened": ctl.now, "window_start": m0, "rate": round(rate, 4),
                   "baseline": round(base, 4), "connected": conn, "flags": flags, "p": p}
            ws["incident"] = inc
            bad = set(ws.get("bad_prompts", [])) | {pv}
            ws["bad_prompts"] = sorted(bad)
            with st.tx():
                st.save_wf_state(wid, ws)
                st.decide(ctl.now, ctl.ts, wid, "incident_opened",
                          f"hallucination spike on {pv}: {flags}/{conn} flagged ({rate:.1%}) vs baseline "
                          f"{base:.1%} (p={p:.1e})", inc)
                st.alert(ctl.now, "notify", wid, "hallucination",
                         f"{wid}: prompt {pv} flag rate {rate:.1%} vs {base:.1%}. Fix proposal is being prepared; "
                         f"check the approval queue and `python -m controller why --wf {wid}`.")
            self._respond(ctl, wid, ws, inc)
        else:
            self._engagement_collapse(ctl, wid, ws)

    # ------------------------------------------------------------------
    def _candidates_live(self, ctl, wid: str, bad_pv: str, base: float) -> list[tuple[str, dict]]:
        w = ctl.agg.wf[wid]
        bad = set(ctl.store.wf_state(wid).get("bad_prompts", [])) | {bad_pv}
        out = []
        for i, pv in enumerate(w.opts["prompt_version"]):
            if pv in bad:
                continue
            r = prompt_record(w, i, ctl.now // 1440)
            if r["connected"] < 500:
                continue
            if binom_sf(r["flags"], r["connected"], base * 1.25) < 0.05:
                continue  # not clean enough
            r["engagement"] = r["engaged"] / r["connected"]
            out.append((pv, r))
        return sorted(out, key=lambda x: -x[1]["engagement"])

    def _validation(self, ctl, wid: str, pv: str, purpose: str, n: int = S.VALIDATION_N) -> dict | None:
        ws = ctl.store.wf_state(wid)
        cached = ws.get("validations", {}).get(pv)
        if cached:
            return cached
        cost = math.ceil(n / 100)
        if ctl.credits < cost:
            with ctl.store.tx():
                ctl.store.alert(ctl.now, "page", wid, "credits_exhausted", f"cannot validate {pv}: {ctl.credits} credits left")
            return None
        try:
            r = ctl.sim.validate(wid, pv, n)
        except SimError as e:
            log.warning("validation failed: %s", e)
            return None
        ctl.credits = r.get("credits_remaining", ctl.credits - cost)
        rec = {k: r[k] for k in ("validation_run_id", "prompt_version", "n", "failures", "pass_rate", "ts")}
        ws = ctl.store.wf_state(wid)
        ws.setdefault("validations", {})[pv] = rec
        with ctl.store.tx():
            ctl.store.save_wf_state(wid, ws)
            ctl.store.spend(ctl.now, wid, "validation", cost, purpose, rec)
            ctl.store.decide(ctl.now, ctl.ts, wid, "validation",
                             f"validated {pv}: pass rate {rec['pass_rate']:.3f} on n={n} ({cost} credits)", rec)
        return rec

    def _respond(self, ctl, wid: str, ws: dict, inc: dict):
        gov = ctl.wfs[wid]["governance"]
        floor = gov.get("min_validation_pass_rate", 0.97)
        base = inc["baseline"]
        choice = None
        for pv, rec in self._candidates_live(ctl, wid, inc["prompt"], base):
            v = self._validation(ctl, wid, pv, f"incident fix candidate for {inc['prompt']}")
            if v and v["pass_rate"] >= floor:
                choice = (pv, v, f"{pv} has a clean live record on this workflow: {rec['flags']}/{rec['connected']} "
                                 f"flagged ({rec['flags'] / rec['connected']:.1%}), engagement {rec['engagement']:.1%}")
                break
        if choice is None:
            choice = self._llm_candidates(ctl, wid, inc, floor)
        if choice is None:
            return
        pv, v, why = choice
        ctl.drafts[wid].append(Draft(
            wid=wid, kind="prompt_fix", urgent=True, risk_reducing=True,
            summary=f"Replace prompt {inc['prompt']} with {pv}: hallucination flags {inc['rate']:.1%} vs normal {base:.1%}",
            patch={"prompt_version": pv},
            rationale=(f"Between {fmt_minute(inc.get('window_start', inc['opened']))} and {fmt_minute(inc['opened'])}, "
                       f"{inc['flags']} of {inc['connected']} connected calls on "
                       f"{inc['prompt']} were flagged (p={inc['p']:.1e} vs baseline {base:.2%}). {why}. Validation "
                       f"{v['validation_run_id']}: pass rate {v['pass_rate']:.3f} (policy bar {floor})."),
            dry_run={"incident": {k: inc[k] for k in ("prompt", "rate", "baseline", "connected", "flags")},
                     "replacement": pv, "validation": v,
                     "expected_flag_rate_after": round(base, 4)},
            lift=0.0, evidence={"validation_run_id": v["validation_run_id"]},
            meta={"incident_opened": inc["opened"]}))

    def _llm_candidates(self, ctl, wid, inc, floor):
        ws = ctl.store.wf_state(wid)
        if ws.get("llm_asked_for") == inc["opened"]:
            sugg = ws.get("llm_suggestions", [])
        else:
            if ctl.credits < 3 + S.VALIDATION_N / 100:
                return None
            try:
                r = ctl.sim.suggest_fixes(wid, f"Prompt {inc['prompt']} shows hallucination flags at {inc['rate']:.1%} "
                                              f"vs {inc['baseline']:.1%} normally; need a safer prompt that keeps engagement.")
            except SimError as e:
                log.warning("llm suggest failed: %s", e)
                return None
            ctl.credits = r.get("credits_remaining", ctl.credits - 3)
            sugg = r["suggestions"]
            ws["llm_asked_for"], ws["llm_suggestions"] = inc["opened"], sugg
            with ctl.store.tx():
                ctl.store.save_wf_state(wid, ws)
                ctl.store.spend(ctl.now, wid, "llm_suggest", 3, "incident: no prompt with a clean live record", sugg)
                ctl.store.decide(ctl.now, ctl.ts, wid, "llm_suggest",
                                 f"LLM suggested {[s['prompt_version'] for s in sugg]} (3 credits)", sugg)
        bad = set(ws.get("bad_prompts", []))
        best = None
        for s in sugg:
            pv = s["prompt_version"]
            if pv in bad:
                continue
            v = self._validation(ctl, wid, pv, "LLM candidate (no live record)", n=500)
            if v and v["pass_rate"] >= max(floor, S.STRICT_PASS_RATE) and (best is None or v["pass_rate"] > best[1]["pass_rate"]):
                best = (pv, v, f"{pv} suggested by LLM ('{s['summary']}'); no live record, so held to the stricter "
                               f"{S.STRICT_PASS_RATE} bar")
        return best

    def _verify(self, ctl, wid, ws, inc, pv, conn, flags, base):
        """Fix applied (prompt changed away from the bad one). Close when the new prompt looks normal."""
        if conn < S.HAL_MIN_CONNECTED:
            return
        rate = flags / conn
        ok = binom_sf(flags, conn, base * 1.25) >= 0.01
        inc["status"] = "resolved" if ok else "open"
        inc["verified_at"] = ctl.now
        inc["after"] = {"prompt": pv, "connected": conn, "flags": flags, "rate": round(rate, 4)}
        ws["incident"] = inc
        if not ok:
            ws["bad_prompts"] = sorted(set(ws.get("bad_prompts", [])) | {pv})
            inc["prompt"] = pv
        with ctl.store.tx():
            ctl.store.save_wf_state(wid, ws)
            ctl.store.decide(ctl.now, ctl.ts, wid, "incident_verified" if ok else "incident_persisting",
                             f"after fix: {pv} {flags}/{conn} flagged ({rate:.1%}) vs baseline {base:.1%} -> "
                             f"{'resolved' if ok else 'still elevated'}; open for "
                             f"{(ctl.now - inc['opened']) / 60:.1f}h", inc)
            if ok:
                ctl.store.resolve_alert(ctl.now, wid, "hallucination")

    # ------------------------------------------------------------------
    def _engagement_collapse(self, ctl, wid: str, ws: dict):
        prev, pv, since = ws.get("prev_prompt"), ws.get("prompt"), ws.get("prompt_since", -10**9)
        if not prev or since < 0 or ws.get("collapse_checked_for") == since:
            return
        if ws.get("prompt_actor") != "supervisor":
            return  # only second-guess supervisor prompt changes; our own fixes are verified elsewhere
        w = ctl.agg.wf[wid]
        ip, iq = w.ix["prompt_version"][pv], w.ix["prompt_version"][prev]
        cn = en = co = eo = 0
        d_since = since // 1440
        for d, E in w.eng.items():
            if d >= d_since:
                cn += int(E[:, :, :, ip, 0].sum()); en += int(E[:, :, :, ip, 1].sum())
            if d_since - 3 <= d <= d_since:
                co += int(E[:, :, :, iq, 0].sum()); eo += int(E[:, :, :, iq, 1].sum())
        if cn < 400 or co < 400:
            return
        z = two_prop_z(en, cn, eo, co)
        rel = (en / cn) / (eo / co) - 1
        ws["collapse_checked_for"] = since if (cn >= 1500) else None  # keep checking until well powered
        with ctl.store.tx():
            ctl.store.save_wf_state(wid, ws)
        if rel < -S.ENGAGE_COLLAPSE_REL and z < -3:
            v = self._validation(ctl, wid, prev, f"revert after engagement drop on {pv}")
            if not v or v["pass_rate"] < ctl.wfs[wid]["governance"].get("min_validation_pass_rate", 0.97):
                return
            ws["collapse_checked_for"] = since
            with ctl.store.tx():
                ctl.store.save_wf_state(wid, ws)
                ctl.store.decide(ctl.now, ctl.ts, wid, "detected",
                                 f"engagement under {pv} {en / cn:.1%} vs {eo / co:.1%} under {prev} ({rel:+.1%}, z={z:.1f})",
                                 {"new": [en, cn], "old": [eo, co]})
            ctl.drafts[wid].append(Draft(
                wid=wid, kind="prompt_revert_engagement", urgent=True, risk_reducing=True,
                summary=f"Revert prompt {pv} -> {prev}: engagement fell {rel:+.0%} after the change",
                patch={"prompt_version": prev},
                rationale=(f"Engagement on {pv} is {en / cn:.1%} ({cn} connected) vs {eo / co:.1%} on {prev} "
                           f"({co} connected) in the 3 days before; z={z:.1f}. Hallucination is not the issue; "
                           f"people are hanging up. Validation {v['validation_run_id']} pass {v['pass_rate']:.3f}."),
                dry_run={"engagement_new": round(en / cn, 4), "engagement_prev": round(eo / co, 4),
                         "connected": [cn, co], "validation": v},
                lift=-rel, evidence={"validation_run_id": v["validation_run_id"]}))

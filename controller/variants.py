"""Welcome-message x voice experimentation (C6), winner promotion and decay rollback (C7).

Evidence    Only calls that actually happened, read from the events with the variant each call really used. Calls
            made under a prompt the incident module marked as bad are excluded (they depress every arm equally
            during the incident, but allocations may have moved meanwhile). Recent days weigh more
            (half-life HALF_LIFE_DAYS) so a winner that stops winning loses posterior mass on its own.
Two dims    welcome_message and voice are drawn independently per call, so each is analysed marginally over
            the other without bias (a factorial design for free). We don't model the W x V interaction; with
            independent draws, a strong interaction would show up as an arm whose marginal moves when the
            other dim's allocation moves, which the decay check would catch.
Posterior   per cohort c and option o: Beta(x + k*m_o, n - x + k*(1-m_o)) where m_o is the workflow-wide rate of
            option o (itself shrunk to the workflow mean) and k = PRIOR_STRENGTH. That partial pooling is how we
            handle many cohorts x options without a Bonferroni wall: a cohort's arm only departs from the
            workflow-wide arm when its own data insists (Gelman, Hill & Yajima 2012).
Allocation  deterministic function of the posterior (fixed-seed Monte Carlo for P(best)):
              * eliminated: P(best) < ELIMINATE_PBEST with >= MIN_ARM_N connected  -> 0
              * winner:     P(best) >= WINNER_PBEST with >= MIN_ARM_N connected    -> WINNER_SHARE, runner-up the rest
              * otherwise:  leader (highest posterior mean) keeps the rest; every plausible challenger gets
                            max(EXPLORE_FLOOR, P(best)/2), capped so the leader keeps >= 40%.
            A challenger at 10% can cost at most ~1% expected engagement even if it is 10% worse — the line the
            business would call harm. Every proposal must also pass a harm check: posterior-mean engagement of the
            new allocation >= current - HARM_TOLERANCE.
Decay       every 2h, per dim, for each option that leads (>= 50%) in some cohorts: a stratified comparison
            (leader vs the other arms within each cohort, inverse-variance pooled across the cohorts it leads) over
            the last ~1.5 days vs the 3 days before. Decay = it WAS ahead (z >= 1), its advantage FELL by
            >= DECAY_DROP_Z standard errors, and it is NOW behind (z <= -DECAY_BEHIND_Z). Rollbacks are urgent and
            risk-reducing (auto-applied where policy allows, else an urgent approval) and the decayed option is
            held out for 3 days. The daily plan's recency weighting is the slower, second line of defence.
"""
from __future__ import annotations

import hashlib
import logging

import numpy as np

from . import settings as S
from .aggregates import FEAT_NAMES, FEATURES, NCODE, cells_where
from .cfg import cohort_ids, dist, fmt_minute, tv
from .cohorts import code_shares, membership
from .governance import Draft
from .incidents import protected
from .stats import beta_draws, prob_best, prob_less

log = logging.getLogger("controller.variants")
DIMS = ("welcome_message", "voice")


def _salt(*parts) -> int:
    return int.from_bytes(hashlib.sha256("|".join(map(str, parts)).encode()).digest()[:4], "little")


def arm_counts(w, dim: str, day_weights: dict[int, float], bad_prompts=()) -> tuple[np.ndarray, np.ndarray]:
    """(N, X): weighted connected and engaged per (feature code, option of `dim`)."""
    k = w.nW if dim == "welcome_message" else w.nV
    N = np.zeros((NCODE, k))
    X = np.zeros_like(N)
    good = np.array([p not in bad_prompts for p in w.opts["prompt_version"]])
    for d, E in w.eng.items():
        wt = day_weights.get(d, 0.0)
        if wt <= 0:
            continue
        Eg = E[:, :, :, good, :]
        if dim == "welcome_message":
            A = Eg.sum(axis=(2, 3))
        else:
            A = Eg.sum(axis=(1, 3))
        N += wt * A[..., 0]
        X += wt * A[..., 1]
    return N, X


def recency(today: int, days, half_life: float = None) -> dict[int, float]:
    hl = half_life or S.HALF_LIFE_DAYS
    return {d: 0.5 ** ((today - d) / hl) for d in days if d <= today}


class Posterior:
    """Arm posteriors for one cohort x dim."""

    def __init__(self, n, x, prior_mean, kappa=None, salt=0):
        k = S.PRIOR_STRENGTH if kappa is None else kappa
        self.n, self.x = np.asarray(n, float), np.asarray(x, float)
        self.a = self.x + k * prior_mean
        self.b = np.maximum(self.n - self.x, 0) + k * (1 - prior_mean)
        self.mean = self.a / (self.a + self.b)
        self.pbest = prob_best(self.a, self.b, salt=salt)


def prior_means(N, X) -> np.ndarray:
    """Workflow-wide rate per option, shrunk to the workflow mean."""
    n, x = N.sum(0), X.sum(0)
    rbar = x.sum() / max(n.sum(), 1e-9)
    return (x + S.PRIOR_STRENGTH * rbar) / (n + S.PRIOR_STRENGTH)


def allocate(post: Posterior, opts: list[str], current: dict, decayed=(), wf: Posterior | None = None
             ) -> tuple[dict, dict]:
    """Deterministic allocation from a cohort posterior. `wf` is the workflow-wide posterior for the same dim:
    an arm already shown to lose workflow-wide is not re-explored just because a (new, small) cohort has little
    data of its own, and "first exposure" means first exposure for the workflow. Returns (distribution, diag)."""
    k = len(opts)
    wf = wf or post

    def dead(i):
        if opts[i] in decayed:
            return True
        if post.pbest[i] < S.ELIMINATE_PBEST and post.n[i] >= S.MIN_ARM_N:
            return True
        # lost workflow-wide and this cohort has no real evidence to the contrary
        return wf.pbest[i] < S.ELIMINATE_PBEST and wf.n[i] >= S.MIN_ARM_N and post.pbest[i] < 0.10

    alive = [i for i in range(k) if not dead(i)]
    if not alive:
        alive = list(range(k))
    # leader: highest posterior mean among arms with real evidence (an untested arm never leads);
    # ties go to whatever currently carries the most traffic
    proven = [i for i in alive if post.n[i] >= S.MIN_ARM_N] or alive
    leader = max(proven, key=lambda i: (round(post.mean[i], 6), current.get(opts[i], 0)))
    # sticky leader: the option carrying most traffic now keeps the lead unless a challenger is probably
    # better (P >= LEADER_SWITCH_P). Flipping 70% of traffic on a noisy mean difference is how small cohorts
    # get hurt.
    inc = opts.index(max(current, key=current.get)) if current and max(current, key=current.get) in opts else None
    if inc is not None and inc in alive and leader != inc:
        if prob_less(post.a[inc], post.b[inc], post.a[leader], post.b[leader]) < S.LEADER_SWITCH_P:
            leader = inc
    best = int(np.argmax(post.pbest))
    diag = {"leader": opts[leader], "pbest": {o: round(float(p), 3) for o, p in zip(opts, post.pbest)},
            "n": {o: round(float(v)) for o, v in zip(opts, post.n)},
            "mean": {o: round(float(v), 4) for o, v in zip(opts, post.mean)},
            "eliminated": [opts[i] for i in range(k) if i not in alive]}
    if post.pbest[best] >= S.WINNER_PBEST and post.n[best] >= S.MIN_ARM_N and best in alive:
        # the monitor arm may be an eliminated one: decay detection needs a live comparator
        others = [i for i in alive if i != best] or [i for i in range(k) if i != best and opts[i] not in decayed]
        diag["winner"] = opts[best]
        if not others:
            return {opts[best]: 1.0}, diag
        runner = max(others, key=lambda i: post.pbest[i] + 1e-6 * post.mean[i])
        return {opts[best]: S.WINNER_SHARE, opts[runner]: round(1 - S.WINNER_SHARE, 4)}, diag
    share = {}
    for i in alive:
        if i == leader:
            continue
        if wf.n[i] < S.MIN_ARM_N:
            share[opts[i]] = S.EXPLORE_FLOOR  # first exposure for the workflow: exactly the floor
        elif post.n[i] >= S.MIN_ARM_N:
            share[opts[i]] = max(S.EXPLORE_FLOOR, float(post.pbest[i]) / 2)
        else:  # tested elsewhere, thin here: keep a little traffic in proportion to how plausible it is
            share[opts[i]] = max(0.03, min(S.EXPLORE_FLOOR, float(post.pbest[i])))
    tot = sum(share.values())
    if tot > 0.6:
        share = {o: v * 0.6 / tot for o, v in share.items()}
    share[opts[leader]] = 1 - sum(share.values())
    return {o: round(v, 4) for o, v in share.items() if v > 1e-4}, diag


def expected_rate(post: Posterior, opts: list[str], d: dict) -> float:
    return float(sum(d.get(o, 0) * post.mean[i] for i, o in enumerate(opts)))


class Variants:
    name = "variants"

    # ---------------------------------------------------------------- cohort preference (cross-fitted)
    def feature_gains(self, ctl, wid: str) -> dict[str, float]:
        ws = ctl.store.wf_state(wid)
        day = ctl.now // 1440
        c = ws.get("variant_pref")
        if c and c["day"] == day:
            return c["gains"]
        w = ctl.agg.wf[wid]
        bad = set(ws.get("bad_prompts", []))
        live_days = [d for d in w.eng if 0 <= d < day]  # history only ever ran the default arms
        gains: dict[str, float] = {}
        if len(live_days) >= 2:
            shares = code_shares(w)
            folds = [{d: 1.0 for d in live_days[i::2]} for i in (0, 1)]
            for dim in DIMS:
                opts = w.opts[dim]
                F = [arm_counts(w, dim, f, bad) for f in folds]
                rbar = sum(x.sum() for _, x in F) / max(sum(n.sum() for n, _ in F), 1)
                for f in FEAT_NAMES:
                    g, fold_g = 0.0, []
                    for a, b in ((0, 1), (1, 0)):
                        gf = 0.0
                        Na, Xa = F[a]
                        Nb, Xb = F[b]
                        pa, pb_ = prior_means(Na, Xa), prior_means(Nb, Xb)
                        glob = int(np.argmax(pa))
                        for v in FEATURES[f]:
                            m = cells_where({f: [v]})
                            s = float(shares[m].sum())
                            if s < S.MIN_COHORT_SHARE:
                                continue
                            ma = (Xa[m].sum(0) + S.PRIOR_STRENGTH * pa) / (Na[m].sum(0) + S.PRIOR_STRENGTH)
                            mb = (Xb[m].sum(0) + S.PRIOR_STRENGTH * pb_) / (Nb[m].sum(0) + S.PRIOR_STRENGTH)
                            pick = int(np.argmax(ma))
                            gf += s * (mb[pick] - mb[glob])
                        fold_g.append(gf)
                        g += gf / 2
                    rel = g / max(rbar, 1e-9)
                    # a split is real only if it helps in BOTH directions of the cross-fit
                    if min(fold_g) > 0 and g >= S.COHORT_GAIN_MIN:
                        gains[f] = gains.get(f, 0.0) + rel
        ws["variant_pref"] = {"day": day, "gains": {f: round(v, 4) for f, v in gains.items()}}
        with ctl.store.tx():
            ctl.store.save_wf_state(wid, ws)
        return ws["variant_pref"]["gains"]

    # ---------------------------------------------------------------- tick
    def tick(self, ctl):
        for wid in ctl.wfs:
            try:
                if ctl.now % 120 == 0:
                    self._decay(ctl, wid)
                self._continue_rollbacks(ctl, wid)
                self._plan(ctl, wid)
            except Exception:  # optimiser bugs must never take down the loop or the safety modules
                log.exception("variants planner failed for %s", wid)

    # ---------------------------------------------------------------- daily plan
    def posteriors(self, ctl, wid: str, cohorts: list[dict], dim: str, day_weights: dict[int, float]):
        w = ctl.agg.wf[wid]
        bad = set(ctl.store.wf_state(wid).get("bad_prompts", []))
        N, X = arm_counts(w, dim, day_weights, bad)
        pm = prior_means(N, X)
        ids, idx = membership(cohorts)
        out = {}
        for g, cid in enumerate(ids):
            m = idx == g
            out[cid] = Posterior(N[m].sum(0), X[m].sum(0), pm, salt=_salt(wid, cid, dim))
        return out, pm

    def _plan(self, ctl, wid: str):
        ws = ctl.store.wf_state(wid)
        day = ctl.now // 1440
        if ws.get("variants_done_day") == day:
            return
        if any(p["created_minute"] // 1440 == day and "variant_allocation" in (p["meta"].get("kinds") or [])
               for p in ctl.store.proposals(workflow_id=wid)):
            ws["variants_done_day"] = day
            with ctl.store.tx():
                ctl.store.save_wf_state(wid, ws)
            return
        gov = ctl.wfs[wid]["governance"]
        h = (ctl.now % 1440) / 60
        if not (gov["approver_hours"][0] + S.ROUTINE_START_OFFSET_H <= h < gov["approver_hours"][1] - S.ROUTINE_LAST_SEND_BEFORE_END_H):
            return  # plan with the freshest data, at the time a routine request could actually go out
        w = ctl.agg.wf[wid]
        cfg = ctl.current[wid]["config"]
        cohorts = ctl.cohort_target.get(wid, cfg.get("cohorts") or [])
        ids, idx = membership(cohorts)
        shares = code_shares(w)
        cshare = {cid: float(shares[idx == g].sum()) for g, cid in enumerate(ids)}
        weights = recency(day, w.eng.keys())
        decayed = {o for o, until in (ws.get("decayed") or {}).items() if until > ctl.now}
        patch: dict = {"allocations": {}}
        report, notes, rel = {}, [], {}
        changed = False
        for dim in DIMS:
            if protected(ctl, wid, dim):
                notes.append(f"{dim} frozen after a supervisor edit")
                continue
            if dim in (ws.get("rollbacks") or {}):
                notes.append(f"{dim}: rollback in progress, left to it")
                continue
            hold = (ws.get("sup_hold") or {}).get(dim)
            held = set(hold["options"]) if hold and hold["until"] > ctl.now else set()
            if held:
                notes.append(f"{dim}: {', '.join(sorted(held))} removed by the supervisor (v{hold['version']}), not re-tested")
            retesting = (ws.get("retest") or {}).get(dim, -1) > ctl.now
            opts = w.opts[dim]
            dw = weights
            if (ws.get("retest") or {}).get(dim, -1) > ctl.now:
                dw = recency(day, w.eng.keys(), half_life=1.0)  # a re-test decides on fresh evidence
                notes.append(f"{dim}: re-test after a lost lead, deciding on the last ~2 days")
            posts, _ = self.posteriors(ctl, wid, cohorts, dim, dw)
            wf_post = self.posteriors(ctl, wid, [], dim, dw)[0]["default"]
            new_default = None
            before = after = 0.0
            for cid in ids:
                post = posts[cid]
                cur_d = dist(cfg, cid if cid in cohort_ids(cfg) else "default", dim)
                # during a re-test the current leader gets no stickiness: it must re-earn the lead
                tgt, diag = allocate(post, opts, {} if retesting else cur_d, decayed | held, wf=wf_post)
                # harm guard: never propose something the posterior expects to be worse
                e_cur, e_new = expected_rate(post, opts, cur_d), expected_rate(post, opts, tgt)
                if e_new < e_cur - S.HARM_TOLERANCE:
                    tgt, diag["held"] = cur_d, f"harm guard: {e_new:.4f} < {e_cur:.4f}"
                    e_new = e_cur
                if tv(cur_d, tgt) < 0.03:
                    tgt = cur_d  # no churn for cosmetic moves
                    e_new = e_cur
                before += cshare[cid] * e_cur
                after += cshare[cid] * e_new
                report[f"{cid}/{dim}"] = {**diag, "before": cur_d, "after": tgt, "traffic_share": round(cshare[cid], 3)}
                if cid == "default":
                    new_default = tgt
                    patch["allocations"].setdefault("default", {})[dim] = tgt
                elif tv(tgt, new_default or {}) > 1e-6 or dim in ((cfg.get("allocations") or {}).get(cid) or {}):
                    patch["allocations"].setdefault(cid, {})[dim] = tgt
                changed |= tv(cur_d, tgt) > 1e-6
                if diag.get("winner") and (ws.get("winners") or {}).get(f"{cid}/{dim}") != diag["winner"]:
                    ws.setdefault("winners", {})[f"{cid}/{dim}"] = diag["winner"]
                    with ctl.store.tx():
                        ctl.store.decide(ctl.now, ctl.ts, wid, "winner_declared",
                                         f"{cid}: {dim}={diag['winner']} wins (P(best)={diag['pbest'][diag['winner']]}, "
                                         f"n={diag['n'][diag['winner']]})", diag)
            rel[dim] = {"before": round(before, 4), "after": round(after, 4),
                        "lift": round(after / before - 1, 4) if before else 0.0}
        cur_ids = [c["id"] for c in cfg.get("cohorts") or []]
        if [c["id"] for c in cohorts] != cur_ids:
            patch["cohorts"] = cohorts
            changed = True
        with ctl.store.tx():
            ctl.store.save_wf_state(wid, ws)
        if not changed or not patch["allocations"]:
            ws["variants_done_day"] = day
            with ctl.store.tx():
                ctl.store.save_wf_state(wid, ws)
            return
        lift = max(sum(r["lift"] for r in rel.values()), 0.0)  # dims multiply: relative lifts add (to 1st order)
        winners = [f"{k.split('/')[0]}: {v['winner']}" for k, v in report.items() if v.get("winner")]
        exploring = sorted({o for v in report.values() for o in v["after"] if o not in v["before"]})
        parts = []
        if winners:
            parts.append("promote " + ", ".join(winners))
        if exploring:
            parts.append("start testing " + ", ".join(exploring))
        if not parts:
            parts.append("rebalance test traffic toward the stronger options")
        sup = self._supervisor_note(ctl, wid)
        ctl.drafts[wid].append(Draft(
            wid=wid, kind="variant_allocation",
            summary=f"Welcome/voice: {'; '.join(parts)} (expected engagement {lift:+.1%})" + sup,
            patch=patch,
            rationale=("Allocation is a deterministic function of Beta posteriors per cohort (pooled toward the "
                       f"workflow-wide arm rate, k={S.PRIOR_STRENGTH:.0f}; recent days weigh more, half-life "
                       f"{S.HALF_LIFE_DAYS}d). Winners need P(best) >= {S.WINNER_PBEST} on >= {S.MIN_ARM_N} connected "
                       f"calls; challengers get >= {S.EXPLORE_FLOOR:.0%} each so a bad one costs <= ~1% engagement; "
                       f"the harm check requires the new mix not to be expected worse. Only calls that actually ran "
                       f"are evidence. {'; '.join(notes)}"),
            dry_run={"method": "posterior-mean engagement of the current vs proposed mix on the last "
                               "days' connected calls (recency weighted), per cohort",
                     "expected_engagement_by_dim": rel,
                     "per_cohort_dim": report},
            lift=lift, meta={"kpi": "engagement_rate", "winners": winners}))

    def _supervisor_note(self, ctl, wid) -> str:
        """Say so when we propose to move something a supervisor set (never silently)."""
        for v in reversed(ctl.store.config_versions(wid)):
            if set(v["changed_dims"]) & set(DIMS):
                if v["actor"] == "supervisor":
                    return f" — note: changes the supervisor's v{v['version']} welcome/voice setting"
                return ""
        return ""

    def _continue_rollbacks(self, ctl, wid: str):
        """Re-emit an unfinished rollback every cycle (auto-apply moves max_shift per step; approvals may be
        rejected). Gives up after 24h — by then the daily plan has absorbed the decay via recency weighting."""
        ws = ctl.store.wf_state(wid)
        live = ws.get("rollbacks") or {}
        if not live:
            return
        cfg = ctl.current[wid]["config"]
        keep = {}
        for key, r in live.items():
            dim = key
            if ctl.now - r["since"] > 1440:
                continue
            done = all(tv(dist(cfg, cid if cid in cohort_ids(cfg) else "default", dim), t) < 0.01
                       for cid, t in r["target"].items())
            if done:
                with ctl.store.tx():
                    ctl.store.decide(ctl.now, ctl.ts, wid, "rollback_complete", f"{key}: {r['target']} live", r)
                continue
            keep[key] = r
            if r["since"] != ctl.now:
                ctl.drafts[wid].append(Draft(**{**r["draft"], "wid": wid}))
        ws["rollbacks"] = keep
        with ctl.store.tx():
            ctl.store.save_wf_state(wid, ws)

    # ---------------------------------------------------------------- decay (C7)
    def _decay(self, ctl, wid: str):
        """Has a leading option stopped winning? Pooled over every cohort where it leads, stratified by cohort
        (each cohort compares the leader with the other arms *in that cohort*, so different allocations across
        cohorts can't confound the comparison). Per-cohort checks were too thin: monitor arms at 15% of a small
        cohort rarely reach a usable sample in 1.5 days."""
        w = ctl.agg.wf[wid]
        cfg = ctl.current[wid]["config"]
        ws = ctl.store.wf_state(wid)
        bad = set(ws.get("bad_prompts", []))
        day = ctl.now // 1440
        short = {d: 1.0 for d in range(day - 1, day + 1)}       # ~1.5 days ending now (today is partial)
        earlier = {d: 1.0 for d in range(day - 4, day - 1)}     # the 3 days before
        ids, idx = membership(cfg.get("cohorts") or [])
        for dim in DIMS:
            if dim in (ws.get("rollbacks") or {}):
                continue
            opts = w.opts[dim]
            Ns, Xs = arm_counts(w, dim, short, bad)
            Ne, Xe = arm_counts(w, dim, earlier, bad)
            leads: dict[str, list[int]] = {}
            for g, cid in enumerate(ids):
                d = dist(cfg, cid, dim)
                lead = max(d, key=d.get)
                if d[lead] >= 0.5 and len(d) >= 2:
                    leads.setdefault(lead, []).append(g)
            for lead, groups in leads.items():
                li = opts.index(lead)
                res = {}
                # Evidence comes from EVERY cohort where the leader and the other arms both ran (decay is a
                # property of the option: it wears off with exposure), stratified so allocation differences
                # between cohorts can't confound it. The rollback only touches cohorts where it leads.
                for name, N, X in (("recent", Ns, Xs), ("before", Ne, Xe)):
                    num = den = nl = na = xl = xa = 0.0
                    for g in range(len(ids)):
                        m = idx == g
                        n, x = N[m].sum(0), X[m].sum(0)
                        nL, xL = n[li], x[li]
                        nA, xA = n.sum() - nL, x.sum() - xL
                        if nL < 30 or nA < 30:
                            continue
                        rL, rA = xL / nL, xA / nA
                        var = rL * (1 - rL) / nL + rA * (1 - rA) / nA
                        if var <= 0:
                            continue
                        num += (rL - rA) / var
                        den += 1 / var
                        nl, na, xl, xa = nl + nL, na + nA, xl + xL, xa + xA
                    res[name] = {"diff": num / den if den else None, "z": num / den ** 0.5 if den else 0.0,
                                 "se": den ** -0.5 if den else None,
                                 "n_leader": int(nl), "n_others": int(na),
                                 "leader_rate": xl / nl if nl else None, "others_rate": xa / na if na else None}
                r, b = res["recent"], res["before"]
                if r["diff"] is None or b["diff"] is None or r["n_leader"] < 200 or r["n_others"] < 100:
                    continue
                # decay = a test of CHANGE: it was ahead (z>=1), its advantage fell significantly, and it is now
                # behind. Requiring all three keeps "a better arm was found" (the daily plan's job) and plain
                # noise out of the urgent rollback path.
                drop_z = (b["diff"] - r["diff"]) / (b["se"] ** 2 + r["se"] ** 2) ** 0.5
                r["drop_z"] = round(drop_z, 2)
                if not (b["z"] >= 1.0 and drop_z >= S.DECAY_DROP_Z and r["z"] <= 0):
                    continue
                # proportionate response: significantly BEHIND -> hard rollback + 3-day hold-out;
                # advantage significantly GONE but not yet clearly behind -> soft: re-open the test (leader 50%),
                # which is what buys the evidence for the next decision (monitor arms alone are too thin).
                hard = r["z"] <= -S.DECAY_BEHIND_Z
                # move each affected cohort to its best other arm (recent data), keep a monitor arm
                patch, per = {}, {}
                for g in groups:
                    cid = ids[g]
                    m = idx == g
                    n, x = Ns[m].sum(0), Xs[m].sum(0)
                    rbar = x.sum() / max(n.sum(), 1)
                    mean = (x + 10 * rbar) / (n + 10)
                    order = [i for i in np.argsort(-mean) if i != li and opts[i] not in (ws.get("decayed") or {})]
                    if not order:
                        continue
                    if hard:
                        new_d = {opts[order[0]]: S.WINNER_SHARE,
                                 opts[order[1]] if len(order) > 1 else lead: round(1 - S.WINNER_SHARE, 4)}
                    else:
                        new_d = {lead: 0.5, opts[order[0]]: 0.35 if len(order) > 1 else 0.5}
                        if len(order) > 1:
                            new_d[opts[order[1]]] = 0.15
                    patch[cid] = {dim: new_d}
                    per[cid] = new_d
                if not patch:
                    continue
                if hard:
                    ws.setdefault("decayed", {})[lead] = ctl.now + 3 * 1440
                else:
                    ws.setdefault("retest", {})[dim] = ctl.now + 2 * 1440
                info = {"leader": lead, "cohorts": [ids[g] for g in groups], "recent": r, "before": b,
                        "new_allocations": per, "response": "hard" if hard else "soft"}
                with ctl.store.tx():
                    ctl.store.decide(ctl.now, ctl.ts, wid, "decay_detected",
                                     f"{dim}={lead} stopped winning in {len(groups)} cohort(s): advantage "
                                     f"{b['diff']:+.1%} -> {r['diff']:+.1%} vs other arms (z={r['z']:.1f}, drop "
                                     f"z={drop_z:.1f}); {'rolling back' if hard else 're-opening the test'}",
                                     info)
                draft = dict(
                    kind="variant_rollback", urgent=True, risk_reducing=True,
                    summary=(f"Roll back {dim} {lead}: it stopped winning" if hard else
                             f"Re-test {dim} {lead}: its lead is gone") + f" ({b['diff']:+.1%} -> {r['diff']:+.1%} vs alternatives)",
                    patch={"allocations": patch},
                    rationale=(f"Stratified by cohort over the last ~1.5 days, {lead} engages {-r['diff']:.1%} below the "
                               f"other arms in the same cohorts (z={r['z']:.1f}, {r['n_leader']} vs {r['n_others']} "
                               f"connected); in the 3 days before it was {b['diff']:+.1%} (drop z={drop_z:.1f}). " +
                               (f"Traffic moves to each cohort's best alternative; {lead} is held out for 3 days."
                                if hard else f"{lead} drops to 50% so the alternatives gather evidence quickly; the "
                                             f"daily plan decides from there.")),
                    dry_run=info, lift=float(max(-r["diff"], 0) / max(r["leader_rate"] or 1, 1e-9)),
                    meta={"kpi": "engagement_rate", "decayed": lead})
                ctl.drafts[wid].append(Draft(wid=wid, **draft))
                ws.setdefault("rollbacks", {})[dim] = {"since": ctl.now, "target": per, "draft": draft}
                with ctl.store.tx():
                    ctl.store.save_wf_state(wid, ws)

# Outcome-First Agent Loop — Submission

**Name:** Pareen 
**Date:** 2026-11-09 
**Hours spent:**  14 hrs
**Language / storage:** Python 3.11+, SQLite (WAL) for decisions plus a pickled snapshot of event aggregates, No server-side infrastructure.

- Runbook for the engineer who owns this: [docs/CONTROLLER.md](docs/CONTROLLER.md)
- Reports: [reports/](reports/).

---

## 0. TL;DR

- An always-on controller (`./run.sh <SIM_URL>`). It folds events into feature-cell count tables, runs caller-ID health checks, call-time scheduling, welcome × voice experiments with decay rollback, and hallucination incident response. It sends **every change through one governance client** that batches per workflow, times requests to approver hours, and auto-applies only operational or risk-reducing changes.
- **public_01: +11.3% leads concluded, 0 harmful changes out of 31. public_02: +24.4%, ks_cancel prompt incident fixed (3 credits), flagged caller ID out within 1h of detection.** Across 4 seeds: **+14.2% ± 0.9 and +26.4% ± 1.5** (§11).
- **Crash safety, shown rather than claimed:** 12 random SIGKILLs reproduce the uninterrupted run's config and approval trail **exactly**. public_03: **23 s of controller time per simulated day, 92k events/s, 706 MB peak**.
- **Not built:** per-person call time (the data can't support it, §5), fair allocation across tenants (C15), prompt optimisation for engagement, and a served UI (the supervisor view is a CLI plus a static HTML page).

## 1. Assumptions

1. `guardrail.*_max_drop: 0.02` is an **absolute** drop in the rate (2 percentage points), not relative.
2. The hidden scenarios use the same simulator, so the API, event schema, time base (D0 = 2027-01-04 IST), the feature value sets and `person_id` stability hold. Nothing assumes a workflow, tenant or option name.
3. The event log is retained and replayable, which is how aggregates are rebuilt after a crash. In production that means a log with at least a few days' retention (Kafka or similar). We snapshot every 6 sim-hours, so a restart replays at most 6 hours of events.
4. Pickup is affected by caller ID and call time. Engagement is affected by welcome, voice and prompt. A change is only rolled back on the guardrail its dimensions can actually move.
5. The QA `hallucination_flag` is noisy, so we act on rates over populations. Validation pass rate measures **safety** only and says nothing about engagement (the contract says so).
6. Approvers judge what we send them. A request's impact score is used to rank a queue, so it must be comparable across kinds of change: 1.0 ≈ an expected +10% in leads concluded.
7. "Leads concluded" (the business outcome) is what proposals are ranked by. The KPIs are still reported and gated separately and are never blended into one score.
8. A client's stated preference that is *stricter* than its policy (Kestrel ops: at most 2 requests a day, none before 11:00) is honoured for routine work. Incident fixes may use the rest of the **policy** budget.
9. Per-call audit ("which variant did customer X hear") comes from the platform's call log, which carries the variant per call. Our store answers *which allocation was live, why, and who approved it* at any time. We don't duplicate 850k rows a day.
10. Credits matter more in production than in the simulator (CTO). We never call the LLM when a prompt with a clean live record exists, and never validate speculatively.

## 2. Scope decisions

| # | Capability | Built / Partial / Cut | Why | What it would take to finish |
|---|---|---|---|---|
| C1 | Event ingestion | **Built** | Everything else depends on it. Chunks streamed as raw bytes and folded into feature-cell cubes; idempotent by chunk; snapshot plus replay | Partition ingestion by workflow (§8) |
| C2 | One shared cohort object | **Built** | Product's definition; it makes every cohort question answerable from the same cubes | — |
| C3 | Bootstrapping from history | **Built** | Free evidence: caller-ID baselines, call-time model, incident baselines and incumbent-arm priors all come from pre-D0 history | — |
| C4 | Call-time optimisation | **Partial** | Per-cohort built, with a cross-fitted replay gate. **Per-person cut**: a lead gets ~3 dials and ~0.8–0.9 connects on average (measured, §5) | A model that pools persons by archetype (EM on hour-of-pickup); 2–3 days |
| C5 | Caller-ID health | **Built** | Biggest single lever in every scenario (load spreading plus flagged numbers) | — |
| C6 | Welcome × voice experiments | **Built** | Primary KPI for most workflows | Explicit W × V interaction model |
| C7 | Promotion, decay, rollback | **Built** | "Back out when something stops working" | Faster detection of mild decays (§13) |
| C8 | Hallucination incident response | **Built** | Regulated clients; graded on time to fix | Exercise the LLM fallback path in a scenario (it is unit-reachable, not scenario-tested) |
| C9 | Governance client | **Built** | Constraint 1; every proposal goes through it | — |
| C10 | Impact, affected leads, dry runs | **Built (honest approximations)** | Product: proposals are a product surface | Dry runs replay on history and posteriors, not a full simulator |
| C11 | Supervisor view | **Built (CLI + static HTML)** | Kestrel ops and Product asked for one screen | A served page with an approve button |
| C12 | Audit store | **Built** | Mandatory; `why --wf X --at T` | — |
| C13 | Crash recovery, demonstrated | **Built** | SRE non-negotiable | — |
| C14 | Observability of the loop | **Built** | SRE; metrics table, `health` view, paging rules | Export to Prometheus / Alertmanager |
| C15 | Fair allocation across tenants | **Cut** | Approver attention is already per workflow (policy limits plus our ceiling), and credits are spent only on incidents, ~3 per incident | A per-tenant credit ledger with a reserve, and weighted fair queuing of incident work |

**Five headline capabilities**, each built and proven end to end: **caller-ID health (C5)**, **experiments with
promotion and decay rollback (C6 + C7)**, **hallucination incident response (C8)**, **the governance client (C9)** and
**crash-safe ingestion and recovery (C1 + C13)**. C2, C3, C10, C11, C12 and C14 are the supporting pieces those five
require (shared cohorts, history priors, proposal contents, read-only views over the same store, the mandatory audit
trail). C4 is partial and C15 is cut.

**Principle:** build in order of *what changes an outcome or keeps us inside a constraint*, and make each piece verifiable. Safety and governance first (they gate everything). Then the two levers the data showed were large (caller-ID health, then experiments). Then the proof the SRE asked for (crash, load). Things that were P0 to someone but that the data shows are small (per-person call time) or not yet a problem (cross-tenant fairness at these credit levels) were cut, and the cut is quantified.

## 3. Stakeholders

Policies quoted below are from `GET /workflows/{id}` on the public scenarios.

### 01 — Sales (Nikhil)
- **Accepted:** speed where it's safe. Caller-ID and call-time changes auto-apply within policy at any hour. A flagged number was out within an hour of detection. Every proposal carries an impact score usable as a per-client "lift" number.
- **Declined:** a **single blended score** (breaks constraint 3 and Product's definition). "Winning by lunch, live by evening": winners need P(best) ≥ 0.95 on ≥ 250 connected calls, and our A/A runs show that's what keeps false winners at 0–1%. Rolling out the **"persuasive" prompt**: prompts are one per workflow, so they can't be A/B-tested within a workflow; a demo in another account isn't evidence here; and any prompt change needs validation evidence.
- **Wrong against policy:** "Kestrel turned on auto-apply for everything". `ks_cancel` allows `[schedule, caller_id, welcome_message, voice]` only, with `max_shift 0.2` per change; prompts and cohorts always need approval. "Northwind is fine with auto-apply on KYC". `nw_kyc` has `auto_apply.enabled: false`, and Northwind's Compliance head requires a named approval for anything customer-facing.
- **Reply:**
  > Nikhil, for the summit I can give you a per-client lift on leads concluded, with pickup and engagement shown separately. A single blended score isn't something we'll show; Product and our clients' contracts both rule it out. Speed: number rotation and call timing go live the same day without asking anyone. Voice and greeting changes need the client's approval. That's the policy Kestrel and Northwind actually signed (Kestrel's auto-apply covers four settings with a cap per change, and KYC has none). The "persuasive" prompt can't be tested inside one workflow and needs safety evidence first, so it isn't going out on a demo result. Happy to walk you through the numbers before the summit.

### 02 — Compliance, Northwind (Meera)
- **Accepted:** every customer-facing change (welcome, voice, prompt, cohorts) goes through a named approval, **on every workflow**, even where a policy would allow auto-apply. Prompt changes carry a validation run id. Requests go out only in approver hours (10:00–19:00; KYC 10:00–18:00), routine ones from 11:00. Operational changes made under the signed latitude are listed with their reasons in the supervisor view ("Changed without asking"). We prefer slow and right: a harm guard (never propose a mix the posterior expects to be worse), 10% exposure for an untested option, and a 0.95 bar for winners.
- **Pushed back:** "even briefly worse for a cohort" can't be promised as zero. Testing means some customers hear an unproven option. We cap it (≤10% per untested option, about 1% expected engagement at worst) and say so up front.
- **Wrong against policy:** the latitude for "call timing and caller-ID rotation" exists on renewal (`allowed_dims: [schedule, caller_id]`) but **not on KYC** (`auto_apply.enabled: false`). We route KYC caller-ID changes through approval.
- **Reply:**
  > Meera, confirmed. Nothing a customer hears changes on Northwind without a named approval, and script changes always carry a validation run. We only send requests in your approvers' hours. On renewal we use the signed latitude for call timing and number rotation; each such change is listed with its reason in the weekly view. On KYC your policy doesn't grant that latitude, so even number changes come to you for approval. For IRDAI questions we can say within minutes which settings were live for a workflow at any moment, why, who approved them, and what we measured afterwards. One honest caveat: testing means a small share of customers (at most 10% per new option) hears an unproven greeting or voice, and we stop it as soon as the data says it's worse.

### 03 — Platform & SRE (Kabir)
- **Accepted:** the kill/restart test (§9): 12 random SIGKILLs, an identical outcome, and no duplicates or lost approvals. Idempotency keys are written before every side effect. Paging only when actionable (§10). The loop's health is visible without reading logs (`controller health`). On load, one simulated day of public_03 takes **23 s** of controller time against a 10-minute target.
- **Pushed back:** "no new stateful infrastructure". We use **SQLite**: a file, not a service, with nothing to operate at 2 a.m. beyond its disk. The schema is plain SQL, so moving it to your Postgres is about a day of work (the `Store` class is the only module that touches it). The aggregates snapshot is a cache that can always be rebuilt from the event log.
- **Wrong:** nothing wrong; the 10× question is answered in §8.
- **Reply:**
  > Kabir, kill test: we SIGKILLed the controller 12 times at random points, including mid-incident, and the run finished with exactly the same config history and approvals as an uninterrupted one. Nothing was applied twice or lost. Load: one simulated day of public_03 takes about 20 s of controller time and ~700 MB. Storage is a SQLite file per deployment, so no new server; moving it to our Postgres is a day's work if you'd rather. At 10× the first thing to break is the in-memory person→features map; the fix is partitioning by workflow (details in §8). Pages fire only for an incident unfixed after 3h, credits low, stalled ingest, or a slow loop, and each tells you where to look.

### 04 — Operations supervisor, Kestrel (Anita)
- **Accepted:** we never silently overwrite her edits. Supervisor edits freeze the dimensions they touched for 24h. Options she removed are kept out of our experiments for 72h. Any later change to a setting she made is said explicitly in the request. Two routine requests a day maximum, never before 11:00 (her ask, stricter than policy's 4 from 10:00). One request bundles everything ("2 changes in one request: …"). One screen: `controller view`.
- **Pushed back:** two things. (a) If her prompt edit **hallucinates**, we propose a revert at once, urgently and with evidence; she still approves it. In public_02 a supervisor prompt edit took flags from 1.4% to 4.4%. (b) "Tell me before anything changes on a running campaign": greetings and voices always come to her first, but **number rotation and safety rollbacks** apply within the policy her company signed and are listed in the view afterwards.
- **Wrong against policy:** none in her memo. Her limits are stricter than the policy, and we honour them. (The Sales memo's claim that Kestrel auto-applies "everything" contradicts both her memo and the policy.)
- **Reply:**
  > Anita, your edits stay yours. If you change a setting, we leave it alone for a day, we don't reintroduce options you removed for three days, and if we ever propose changing something you set, the request says so. You'll get at most two requests a day, none before 11:00, with everything bundled into one. The one exception: if a prompt starts producing made-up claims, we'll send an urgent revert with the evidence; you still approve it. Number rotation and safety rollbacks happen automatically within your policy and show up in your one screen, next to what's being tested and what's winning. And we watch engagement, not just safety, so a "safe" script nobody listens to gets flagged too.

### 05 — Product (Tara)
- **Accepted:** KPIs separate everywhere. **One cohort object**: definition-derived IDs (`age_band=60+|lang=hi`) used by experiments, scheduling, the affected-lead counts and the supervisor view. Every proposal has an impact score, diff, affected-lead count and dry run, and the impact score has one meaning (≈ expected % lift in leads concluded ÷ 10%, capped at 1; incident fixes 0.9). Expired = rejected: re-planned on fresh data, never resubmitted. Only calls that actually ran are evidence. Winners update only their own workflow.
- **Minimum cohort size and window (her open question):** a cohort must expect **≥ 150 connected calls a day** (and ≥ 4% of leads). An option needs **≥ 250 connected calls** before it can win or be dropped. Evidence is recency-weighted with a **2.5-day half-life**. Cost: at 250 connected calls a day, a +22% option is found 62% of the time within 8 days (100% at 1,100 a day), and a +10% one rarely. In exchange the false-winner rate is 0–1.2% (§5).
- **Pushed back:** not everything is P0. **Per-person call time is cut.** Caller-ID health and the supervisor view are built.
- **Wrong:** "per-person call-time is the headline". In this data a lead gets ~3 dials and under one connect on average (0.8–0.9, measured), which isn't enough to learn a personal hour. Even per-cohort call time is worth only +0.4–1.4% (§5).
- **Reply:**
  > Tara, I picked: cohort size ≥ 150 connected calls a day per cohort, ≥ 250 per option before any verdict, and a 2.5-day recency half-life. That costs speed (a +10% option often isn't confirmed within a week at small volumes) and buys a measured false-winner rate of about 1%. One cohort definition is shared everywhere, and impact scores mean "expected % lift in leads concluded ÷ 10". I built caller-ID health and the one-screen view. I cut per-person call time: each person gives us a handful of dials and at most one pickup, which can't support a personal schedule, and even per-cohort timing measured below 1.5%. The numbers are in §5 if you want to challenge them.

### 06 — CTO (Aryamann)
- **Accepted:** explainability over lift. Every change has a stored rationale, evidence and a 24-hour after-check. "Worse for a cohort is worse than nothing" is why we have a harm guard, 10% exposure for an untested option, decay rollback and per-cohort posteriors. Credits: 3 credits per incident (a prompt with a clean live record beats calling the LLM). Scale and on-call docs: §8 and [docs/CONTROLLER.md](docs/CONTROLLER.md).
- **Wrong in other memos:** listed above (Sales on Kestrel and KYC auto-apply; Compliance's latitude on KYC; Product's per-person headline).
- **Reply:**
  > Aryamann, I chose five things and made each provable: caller-ID health, experiments with decay rollback, incident response, one governance client, and crash-safe ingestion. I cut per-person call time (the data can't support it) and cross-tenant fairness (not yet a bottleneck at these credit levels). The SUBMISSION has the reply I'd send each stakeholder and where their memo didn't match the policy. Results: +11–24% leads concluded on the public scenarios (+14% and +26% averaged over seeds), credits spent only on the one incident, and a kill test that reproduces the decision trail exactly. What breaks first at 10× is memory in the person map, and the fix is partitioning by workflow.

## 4. Architecture

```
 simulator / platform                       controller (one process, per deployment)
 ───────────────────                        ─────────────────────────────────────────────────────────────
 GET /events/chunks/{n} ──stream bytes──▶  ingest ─▶ Aggregates (per workflow, per day, per feature cell)
   (history + live)                          │          pick[h,cell,DID] · eng[cell,W,V,P] · hal[h,P] · leads
                                             │          (snapshot every 6 sim-h → aggregates.pkl)
 GET /workflows/{id}/config(/history) ──▶  sync_configs ─▶ config_versions (every version, full config)
                                             │
                                             ▼   modules read state, append Drafts (never call mutation APIs)
                         SupervisorWatch · Hallucination · CallerID · Verifier · CohortPlanner
                         · Variants · Schedule
                                             │ Drafts (patch, rationale, dry run, lift, urgent?, risk_reducing?)
                                             ▼
 POST /approvals, /apply, PUT /config ◀── Governance: reconcile in-flight → merge per workflow → route
 GET /approvals/{id}                        (auto if policy allows AND operational/risk-reducing; else ONE
                                             approval request in approver hours, within budget)
                                             outbox row written BEFORE every HTTP side effect
                                             │
 POST /llm, /validation (Hallucination) ◀──  LoopHealth → alerts (page / notify / ticket)
                                             ▼
                         SQLite controller.db: decisions (audit) · proposals (outbox) · config_versions
                                             · wf_state (memory) · credits · metrics · alerts
                         views: controller why / log / view / health (+ supervisor.html, health.html)
```

**Restart:** load `aggregates.pkl` (cursor = last chunk folded), replay later chunks, reload memory from SQLite, then **reconcile** every proposal still in `intent / pending / approved`. Each is looked up by `proposal_id` in the simulator's approvals or config history before anything is resent. Proposal IDs are deterministic (`wf.minute.kinds.config-hash`), so re-planning at the same minute after a crash produces the same ID and is skipped.

## 5. The learning loop

**Cohorts.** One object. A cohort is a union of feature cells, so any definition can be evaluated retrospectively from the cubes. Each optimiser scores each feature by **cross-fitting**: choose the best option per segment on even days, score it on odd days, and vice versa. A feature counts only if it helps in **both** directions and by ≥ 0.8pp of engagement. Up to the top 2 features form a product partition. Cells under max(4% of leads, 150 connected calls a day) fold into `default`. Live cohorts change at most every 3 days, and only if the new split is ≥ 25% better (hysteresis). Without these rules we observed cohorts changing daily on noise.

**Experiment design.** Welcome and voice are drawn independently per call, which makes a factorial design for free. Each is analysed marginally over the other, unbiased by construction. We don't fit a W × V interaction term; a strong interaction would surface as an arm whose result moves when the other dimension's allocation moves, which the decay check watches. **Allocation** is a deterministic function of Beta posteriors per cohort × option, partially pooled toward the workflow-wide option rate (k = 40 pseudo-calls):

- **First exposure:** an option untested *on this workflow* gets exactly 10%, so a challenger 10% worse costs ≤ ~1% expected engagement.
- **Ongoing:** each plausible challenger gets max(10%, P(best)/2).
- **Leader:** the option with the highest posterior mean among those with ≥ 250 connected calls. It's **sticky**: a challenger takes the lead only at P(better) ≥ 0.8.
- **Dropped:** P(best) < 2% with ≥ 250 connected calls, in the cohort or workflow-wide.
- **Harm guard:** never propose a mix whose posterior-mean engagement is below the current mix's.

**Deciding a winner.** A posterior stopping rule: P(best) ≥ 0.95 with ≥ 250 connected calls. The winner gets 85%; the runner-up keeps 15% as a decay monitor. Multiple comparisons are handled by partial pooling (a cohort's option departs from the workflow-wide estimate only when its own data insists) rather than Bonferroni. `scripts/aa_test.py` runs the real `allocate()` day by day on synthetic arms (400 sims):

| connected calls per cohort per day | A/A false-winner rate | +10% option found (median day) | +15% | +22% | wrong winner | cost of a −20% challenger |
|---|---|---|---|---|---|---|
| 1,100 | **1.2%** (3 arms), 0% (4) | 68% (day 5) | 96% (day 4) | 100% (day 3) | **0%** | −1.3% engagement |
| 250 | **0%** | 3% | 19% | 63% (day 6) | **0%** | −1.8% |

Even without a declared winner, adaptive allocation captures +4.5% to +14.5% versus the incumbent at 1,100 calls a day. Required sample by classical power, for reference: `stats.required_n(0.55, 0.15)` ≈ 570 per arm. The Bayesian rule with pooling gets there with less because it stops adaptively.

**Decay and rollback.** Every 2h, per dimension, for each option that leads somewhere: a **stratified** comparison (leader vs the other options *within each cohort*, inverse-variance pooled over every cohort where both ran), last ~1.5 days vs the 3 days before. Decay requires a **change**: the option was ahead (z ≥ 1), its advantage fell by ≥ 2.5 SE, and it is no longer ahead.

- **Soft response** (lead gone, not yet significantly behind): re-open the test with the leader at 50% and decide on 1-day half-life data.
- **Hard response** (behind, z ≤ −1.64): move each affected cohort to its best alternative at 85% and hold the option out for 3 days.

Rollbacks are urgent and risk-reducing, and they win any merge with routine plans. Measured: public_02's W3 (+18% → −10%) went from 91% share to 16% by day 8 and 2% by day 9. public_01's mild decay (−7% at the end of the horizon) is caught later (§13).

**Call time and caller ID.**
- **Caller ID:** each number's pickup is compared with the *other* numbers in the same (day, hour) cells. The dialler draws caller ID independently of the lead, so this removes time-of-day and lead-mix effects without a model. Two passes, so a bad number can't flatter the others. Flagged when below 0.8× with z < −3. Healthy numbers share traffic equally (load spreading turned out to be the largest lever). Unproven numbers are probed at 5% (they can't move expected health by more than 2%), numbers we rotated out are re-tested at 2% with doubling backoff, and no number exceeds 60% while an alternative looks good.
- **Call time:** history ran every hour under the default config, a natural experiment. Per-cohort hour weights = (relative pickup)^β, clipped [0.25, 4], so nobody is starved. They are scored by a **fluid replay of a dialling day** (pool, attempts, 2h gap, capacity) with cross-fitting. Result: per-cohort call time is worth **+0.4%** in leads concluded (replay); a forced-on calibration run measured about +1.4pp pickup, so the replay is conservative. Gate: 1% to create cohorts just for call time, 0.3% when cohorts exist anyway. **Per-person** was cut because a lead gets ~3 dials and 0.8–0.9 connects on average (measured on public_02), and the pickup archetype is only partly explained by features.

**Guardrail KPI.** Welcome and voice can't move pickup in this contract; caller ID and schedule can't move engagement. Each applied change gets a **24h before/after check on both KPIs**. If the guardrail fell by more than its allowed drop with z < −2.5 *and* the change touched a dimension that can move it, we propose an urgent rollback of those dimensions. Before/after comparisons are confounded by drift, which is why the bar is high and why randomised arms, not this check, decide winners.

## 6. Governance

**State machine** (proposals.status): `intent → pending → approved → applied`, with `approved → stale` (base moved, typically a supervisor edit: not applied, re-planned from the new base) and `approved → lapsed`. `pending → rejected | expired`; expired is treated as a rejection and re-planned with fresh data, never resubmitted. `intent → stale_at_submit | auto_applied | auto_rejected`. A crash at any point resumes from `intent` by lookup (§9).

**Batching and timing.** All of a cycle's drafts for a workflow merge into **one** change ("3 changes in one request: …"); urgent drafts merge last so they win. Routine requests go out only between approver start + 1h and approver end − 3h (so they're decided the same day), at most `min(2, policy − 1)` per 24h, keeping one policy slot for incidents. At most one routine request is in flight per workflow. Urgent requests wait for approver hours (not sent at night) and may use the full policy budget. Approved changes are applied in the first cycle that sees them (30-minute steps against a 2h apply window), so **0 lapsed** in every run. TTL is the policy's.

**Auto-apply.** Used only when the policy allows the dimensions **and** the change is operational (caller ID, schedule) or risk-reducing (moving traffic off something measured as bad). Voluntarily *not* used for anything that adds new customer-facing exposure, even where Kestrel's policy permits it. `max_shift` is treated as a rate limit (one step per cycle, with a rounding margin). Routine auto-applies are held while an approval is in flight so they don't make it stale. If the boundary refuses something our pre-check allowed, the workflow uses approvals for 2h and a ticket is raised. That fallback is what caught a 4-decimal rounding bug during development.

**Impact, affected leads, dry run.**
- `impact_score` = min(1, expected relative lift in leads concluded / 0.10): from the cross-fitted replay for schedules, posterior means for allocations, measured ratios for caller IDs (unmeasured numbers assumed at 0.9×). Incident fixes are 0.9.
- `affected_leads` = leads uploaded within the cadence horizon and not concluded, in the cohorts whose experience changes (all of them for prompt or cohort changes).
- `dry_run` carries the evidence table behind the decision (per cohort × option: n, rate, P(best), before/after) or the replay numbers.

They are honest approximations, labelled as such, and each change is checked against reality after 24h.

**Supervisor edit mid-experiment.** It is recorded and never overwritten. The edited dimensions are frozen 24h for routine changes, options she removed are excluded from exploration for 72h, and any approved request built on the old base is marked `stale` instead of applied. Learning is unaffected because evidence is per call (what actually ran), not per config. public_02, nw_renewal D5 15:00: the voice edit to V2 was respected, and the in-flight request went stale and was re-planned.

## 7. Incidents

**Thresholds.** Hallucination: flag rate of the current prompt since it went live (≤ 6h window) ≥ 1.6× the workflow's baseline (pooled over prompts with a clean record), on ≥ 80 connected calls, with a binomial tail p < 1e-4. With 48 checks a day that bounds false alarms at about 1 per 200 workflow-days (the ratio condition makes it rarer). A false alarm costs 3 credits and one approval request to revert to a known-good prompt. Caller ID: below 0.8× with z < −3 on ≥ 250 dials. Engagement collapse after a supervisor prompt edit: −8% with z < −3.

**Picking a fix.** First, a prompt with a **clean live record on this workflow**: it has real evidence for both safety and engagement, and costs no LLM call. Only if none exists, ask the LLM (3 credits) and hold its never-live suggestions to a stricter 0.98 validation bar. Validation n = 300 (3 credits), cached and reused as evidence.

**Verification.** After the fix is live, the new prompt's flag rate over the next window must be back near baseline before the incident closes. The 24h after-check records both KPIs.

**public_02 timeline (ks_cancel), from our audit log:**

| sim time | event |
|---|---|
| D2 13:30 | caller ID D2 flagged: 0.79× the other numbers in the same hours, z = −5.0 (the number was degraded at D2 10:00) |
| D2 13:30–14:00 | D2 rotated out by auto-apply in two `max_shift` steps (0.2 each); share of dials 50% → 0% |
| D3 11:30 | supervisor ships prompt P2; edit recorded and respected; our pending request went stale and was not applied |
| D3 15:00 | **incident opened:** 17/390 flagged (4.4%) vs 1.45% baseline, p = 7.4e-5; P1 validated (pass 0.987, 3 credits); urgent request sent |
| D3 16:30 | approved → applied (v14) |
| D3 17:30 | **verified:** P1 1/104 flagged (1.0%); incident closed. Grader time to fix: 300 min (210 detection, 90 approver) |

Credits used in the whole scenario: **3**.

## 8. Scale

Measured on public_03_load (12 workflows, 9.7M events including 5.5M history). Details in [reports/public_03_load.md](reports/public_03_load.md).

| measure | value |
|---|---|
| ingest throughput (parse + fold) | **92k events/s** (122–137k in earlier runs on an idle machine) |
| controller time per simulated day (ingest + decide) | **23 s** (14–16 s idle; target under 10 min, ≥ 26× headroom) |
| decision lag per 30-min cycle, median / p95 | 0.36 s / 1.15 s |
| history bootstrap (5.5M events) | 62 s (42 s idle) |
| peak memory | **706 MB** (was 5.4 GB before chunks were streamed as raw bytes) |
| outcomes at scale | **+16.4% leads concluded, 0 harmful out of 81 changes** |

**What breaks first at 10×** (8.5M events a day, ~120 workflows):

1. **Memory in the person→cell map** (a Python dict of ~1.3M entries now, ~13M at 10×, roughly 3–4 GB). Fix: per-workflow numpy arrays indexed by person number, and evicting people outside the cadence and returning-lead horizon.
2. **Single-process ingest:** about 70 s a day at 10×, still fine, but one core.
3. **Approver attention**, a business limit rather than a technical one.

**Partitioning:** aggregates, cohorts, experiments and governance are already independent per workflow. Shard workflows across worker processes by `hash(tenant_id)` so a tenant's approver budget lives in one place. Each worker owns its SQLite or Postgres schema and event-log consumer offsets. Credits become a per-tenant ledger with a shared incident reserve (the C15 work).

## 9. Reliability

**Method** (`scripts/crash_test.py`). Run A is uninterrupted. In run B the controller is SIGKILLed **12 times** at random wall-clock moments across D0–D8, including three during the hallucination incident (D3 14:30, D3 16:30 ×2), and restarted with the same state directory each time. Result ([reports/crash_recovery.md](reports/crash_recovery.md)):

| check | result |
|---|---|
| config history identical to the uninterrupted run (43 versions: actor, dims, proposal_id, time) | PASS |
| approval trail identical (31 approvals) | PASS |
| no proposal applied twice | PASS |
| no approval lost (approved ⇒ applied / lapsed / stale in our store) | PASS |
| every request the approver saw is in our outbox | PASS |

**State lost on a crash:** none that matters. Up to 6 sim-hours of aggregates are rebuilt by replaying events. Decision-log entries made during the killed cycle may be written twice (they are log lines; every side effect is keyed).

**Idempotency keys:** `proposal_id` = `wf.minute.kinds.config-hash` for approvals and auto-applies (written to the outbox before the call; recovered by lookup on restart). Chunks are folded whole, with an `event_id` guard. `advance` is never retried blindly; on an unknown outcome we re-read `/sim/state`.

**Simulator 4xx/5xx:** reads retry with backoff. Mutations are never retried blindly: an unknown outcome stays `intent` and is reconciled. Mapping: 409 duplicate → lookup; 409 stale → re-plan; 410 lapsed → re-plan; 403 auto-apply refused → ticket plus approval path for 2h; 402 → page (credits). One module failing doesn't stop the loop or the safety modules.

## 10. Audit & observability

**"Why did ks_cancel change at D3 16:30, who approved it, and what happened after?"**

```
$ python -m controller why --state-dir state/public_02_incidents --wf ks_cancel --at "D3 17:00"
# ks_cancel at D3 17:00
Live config: v14 by controller_approved, applied D3 16:30; changed: prompt_version
## Why (proposal ks_cancel.D3T1500.prompt_fix.0c01cd8666)
Route: approval, approval apr_00011
Summary: Replace prompt P2 with P1: hallucination flags 4.4% vs normal 1.5%
Impact score: 0.9  affected leads: 3475
Rationale: [prompt_fix] Between D3 11:30 and D3 15:00, 17 of 390 connected calls on P2 were flagged (p=7.4e-05
  vs baseline 1.45%). P1 has a clean live record on this workflow: 291/20088 flagged (1.4%), engagement 50.9%.
  Validation val_00001: pass rate 0.987 (policy bar 0.97).
Diff: {"prompt_version": {"after": "P1", "before": "P2"}}
Evidence: {"validation_run_id": "val_00001"}
## Lifecycle
- D3 15:00  proposed           [approval, urgent] Replace prompt P2 with P1: hallucination flags 4.4% vs normal 1.5%
- D3 15:00  requested          approval apr_00011 requested, expires 01-08T03:00
- D3 16:30  approval_approved  apr_00011 approved
- D3 16:30  applied            v14 live (controller_approved): Replace prompt P2 with P1 ...
- D4 16:30  post_apply_check   v14 (prompt_fix) 24h after vs before: pickup 26.4% -> 25.9% (z=-0.5), engagement 51.3% -> 50.7% (z=-0.3)
## What happened next
- D3 17:30  incident_verified  after fix: P1 1/104 flagged (1.0%) vs baseline 1.5% -> resolved; open for 2.5h
```

Also: `controller log --wf X [--kind …]`, `controller view` (the supervisor's screen) and `controller health` (on-call).

**Page on** (each message says where to look): a hallucination still live 3h after detection; credits below the reserve; no call events for 60 sim-minutes inside a dialling window; a cycle slower than 10 minutes.
**Notify (business hours):** an incident detected and being fixed; a caller ID rotated out; a guardrail rollback; an expired or lapsed request.
**Ticket:** the boundary refused a change our pre-check allowed.
**Never page:** judgement-call rejections, approver latency, exploration results, expected decay rollbacks.

## 11. Results

Canonical runs (each scenario's own seed). The full reports are `reports/public_01_dev.md` and `reports/public_02_incidents.md`.

**public_01** — 31 changes applied: 10 beneficial, 21 neutral, **0 harmful**; 18 requests, 15 approved, 3 judgement-call rejections, 0 lapsed, 0 apply conflicts; 0 credits used.

| workflow | conclusion rate | Δ | pickup Δ | engagement Δ | engagement gap captured |
|---|---|---|---|---|---|
| ks_cancel | 0.384 | +12.4% | +9.3% | +2.3% | 10% |
| nw_kyc | 0.456 | +21.2% | +18.2% | +0.5% | 16% |
| nw_renewal | 0.439 | +4.4% | +6.6% | −0.9% | 7% |
| **ALL** | 0.418 | **+11.3%** | +10.0% | +0.9% | 10% |

**public_02** — 40 changes applied: 11 beneficial, 26 neutral, **3 harmful**; 29 requests, 25 approved, 4 rejected, 0 lapsed; 3 credits used.

| workflow | conclusion rate | Δ | pickup Δ | engagement Δ | engagement gap captured |
|---|---|---|---|---|---|
| ks_cancel | 0.367 | +50.5% | +35.2% | +7.5% | 43% |
| nw_kyc | 0.456 | +18.7% | +17.6% | −1.6% | −5% |
| nw_renewal | 0.462 | +7.5% | +8.9% | −2.0% | 1% |
| **ALL** | 0.418 | **+24.4%** | +20.7% | +1.0% | 23% |

**Across seeds** (`scripts/eval_seeds.py`: same scenario and truth, seeds 1–4). One seed is one draw, and every decision changes the random path:

| scenario | leads concluded | pickup | engagement | harmful per run | decayed variant's share, last 3 days |
|---|---|---|---|---|---|
| public_01 | **+14.2% ± 0.9** | +9.5% | +3.2% | 0.25 (of ~24 changes) | 54% |
| public_02 | **+26.4% ± 1.5** | +22.4% | +1.3% | **0.00** (of ~31) | 9% |

**Where we helped.** Caller-ID load spreading and flagged-number rotation (pickup +7% to +35% on every workflow); the ks_cancel prompt fix (+10% expected engagement at apply); per-cohort voice and welcome winners on ks_cancel and nw_kyc.

**Where we were harmful, and why.** The canonical public_02 seed had 3 harmful changes:
- **ks_cancel v15 (−1.0%):** re-testing welcome options after the cohort split.
- **nw_renewal v10 (−1.5%):** a cohort change onto `city_tier × value_band`, which is noise in this scenario, plus a re-allocation on thin per-cohort data.
- **nw_renewal v11 (−2.2%):** a voice re-allocation inside those small cohorts.

All three come from cohort splits that passed the cross-fit bar by chance on small data. Across 4 seeds the average is 0 per run. nw_renewal's engagement (−2%) is also held back by the decaying W3 being caught late on this seed, and nw_kyc's guardrail held (−1.6% relative, about 1pp absolute, against a 2pp limit).

**What I'd change.** Require a cohort split to be stable on two consecutive days before using it, and scale `MIN_ARM_N` with the cohort count. Both are one-line settings, testable with `eval_seeds`.

## 12. Trade-offs & rejected alternatives

| Option | Why considered | Why rejected / what instead |
|---|---|---|
| Thompson sampling (random draws) | Standard bandit, good regret | Constraint 4: winners must be reproducible. We use posterior probability-matching computed with a **fixed-seed** Monte Carlo: the same counts always give the same allocation |
| Frequentist tests with Bonferroni across cohorts × options | Familiar, easy to audit | Too conservative at these volumes (~570 per arm for +15%); partial pooling plus a P(best) rule had 0–1.2% false winners in A/A |
| Store every call event | Per-call audit, ad-hoc queries | 850k rows a day for data the platform already keeps; feature-cell cubes answer every question the loop asks, at ~170–330 KB per workflow-day |
| Postgres / Redis | SRE already runs them | SQLite is a file, not a service. `Store` is the only module touching it, so the move is about a day |
| Uniform exploration (1/k per option) | Fastest learning | One bad challenger at 25% costs ~2.5% engagement, a "harmful" change; 10% per untested option instead |
| Auto-apply everything the policy allows (Kestrel welcome/voice) | Faster, which Sales wanted | Compliance and the Kestrel supervisor want customer-facing changes seen first. Auto-apply only operational or risk-reducing changes |
| Per-cohort decay checks | Simple | Too thin: comparison options at 15% of a small cohort never reached a usable sample. Stratified pooling across cohorts instead |
| LLM-first incident response | It's in the brief | Costs credits and suggests never-live prompts. A known-good prompt has better evidence; the LLM is the fallback |
| One run per settings change | Fast iteration | Single seeds misled us twice; settings were chosen by 4-seed comparisons |

## 13. Known weaknesses

1. **Mild late decays are caught late.** public_01's W3 (+15% → −7% around days 3–5 of an 8-day scenario) is detected 1–2 days after it becomes worse; the decayed share in the last 3 days is 54% averaged over seeds. A larger, earlier decay (public_02) is rolled back to 9%.
2. **Cohort splits on noise still happen** on small workflows (nw_renewal `city_tier × value_band` in the canonical public_02 seed). They cause most of our harmful changes.
3. **Incident detection takes 1.5–3.5h** at a 3× flag rate, because the threshold (p < 1e-4) favours few false alarms. Time to fix is detection plus approver latency; we can't shorten the latter.
4. **The LLM fallback path is unit-reachable but not exercised** by the public scenarios (a known-good prompt always existed).
5. **Dry runs are models:** a fluid replay of history and posterior means. They're conservative where measured (the call-time calibration), and every change is checked after 24h, but they aren't a simulator.
6. **A false decay on ks_cancel D2:** the incumbent V1 was flagged on a +6.5% → −9.3% swing. The verdict was neutral; the "was ahead" test needs more history than 3 days at small n.
7. **No W × V interaction model**, and no per-person call time.
8. **The 24h before/after guardrail check** is confounded by day-to-day drift, so it only acts on large, significant drops.

## 14. If this went to production at Formi next month

1. **Week 1, shadow mode.** Run against live events with governance in dry-run (write proposals, send none). Compare our proposals with what supervisors actually did and calibrate the impact scores against outcomes.
2. Move `Store` to Postgres; feed `metrics` and `alerts` into the existing Prometheus and Alertmanager; put the paging rules through on-call review.
3. Turn on caller-ID health (operational, policy-permitted, largest lever) for one tenant with auto-apply, and the weekly "changed without asking" report for its compliance team.
4. **Week 2:** experiments on one workflow per tenant via approvals, with the supervisor view as a served page with an approve button.
5. Replace the person dict with per-workflow arrays and shard workers by tenant, before onboarding the second large tenant.
6. Add a per-tenant credit ledger (C15) and the cohort-stability rule from §11.

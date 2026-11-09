# Controller runbook

For the engineer who owns this loop, or who gets paged for it. The design reasoning is in
[SUBMISSION.md](../SUBMISSION.md); this file covers running it, reading it, debugging it and extending it.

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # Python 3.11+; numpy, httpx (+ sim's deps)
./run.sh http://127.0.0.1:8700                                       # the contract: runs until the scenario ends
scripts/run_scenario.sh public_02_incidents [port]                   # baseline + fresh sim + controller + grade
.venv/bin/python -m pytest -q tests                                  # ~7s
```

State goes to `state/<scenario name>/` (override with `CONTROLLER_STATE_DIR`). **Re-running against the same
state dir resumes**: that is the crash-recovery path, not a bug. Use a fresh dir for a fresh run.

| file in the state dir | what it is | can I delete it? |
|---|---|---|
| `controller.db` | SQLite (WAL): audit log, proposal outbox, config mirror, per-workflow memory, credits, metrics, alerts | No. It's the source of truth for every decision |
| `aggregates.pkl` | snapshot of the event count cubes plus the chunk cursor | Yes. It is rebuilt by replaying the event log (slower restart) |
| `supervisor.html`, `health.html` | static views written at the end of a run | Yes |

## Read it

```bash
python -m controller view   --state-dir state/X [--wf W] [--html out.html]   # supervisor's one screen
python -m controller health --state-dir state/X                              # on-call: lag, credits, alerts
python -m controller why    --state-dir state/X --wf W --at "D3 14:00"       # what was live, why, who approved, what next
python -m controller log    --state-dir state/X [--wf W] [--kind proposed|applied|decay_detected|...]
```

Every row in `decisions` has `sim_minute`, `workflow_id`, `kind`, a human `summary` and a JSON `detail`.
Proposals carry the full request body (diff, dry run, impact, rationale, evidence), so `why` never needs the simulator.

## How a cycle works (controller/loop.py)

Every `STEP_MINUTES` (30) of simulated time:

1. **ingest**: fold every chunk after the cursor into per-workflow count cubes (`aggregates.py`). Chunks are the unit
   of idempotence, and chunk bodies are streamed as raw bytes.
2. **sync_configs**: mirror any new config version (ours or a supervisor's) into `config_versions`.
3. **modules**, in order (`modules.py`). Each reads state and may append a `Draft` to `ctl.drafts[wid]`:
   `SupervisorWatch` → `Hallucination` → `CallerID` → `Verifier` → `CohortPlanner` → `Variants` → `Schedule` →
   **`Governance`** (the only module that changes configs) → `LoopHealth`.
4. Governance merges the cycle's drafts per workflow into **one** change, routes it (auto-apply or approval), and
   writes the proposal row **before** the HTTP call. Then it reconciles in-flight proposals.
5. Snapshot the aggregates every 6 sim-hours; advance the simulator.

Planners never call mutation endpoints. If you add a planner, emit `Draft`s; don't call `ctl.sim` to change anything.

## Debugging

| symptom | where to look |
|---|---|
| a change you didn't expect | `why --wf W --at T` → the proposal's rationale and dry run; `log --kind proposed` |
| a change that didn't happen | `log --wf W` for `deferred` (request budget), `stale`, `approval_rejected`, `auto_rejected` |
| `auto_rejected` alert (ticket) | our pre-check (`cfg.changed_dims`/`max_shift`) disagreed with the boundary. Workflow falls back to approvals for 2h |
| incident open for a long time (page) | `why` at the incident time; `health` for credits; the `prompt_fix` proposals and their reasons |
| controller slow | `health`, then the `module_seconds:*` metrics in the `metrics` table |
| restart replays a lot | the snapshot is older than you think; check `SNAPSHOT_EVERY_MINUTES` and the snapshot file's mtime |

Simulator 4xx/5xx: reads are retried with backoff. Mutations are never blindly retried. An unknown outcome leaves
the proposal in `intent`, and the next cycle looks it up by `proposal_id` (`Governance._recover_intent`).

## Extending

* **Tunables** are all in `controller/settings.py`, each with its reason. Try one without editing code:
  `CONTROLLER_SET="LEADER_SWITCH_P=0.7" ./run.sh URL`.
* **Compare policies across seeds**, not on one run: `scripts/eval_seeds.py public_02_incidents --seeds 1 2 3 4 --set K=V --label x`.
* **New optimiser**: a module with `tick(ctl)` that emits `Draft(kind=..., patch=..., rationale=..., dry_run=..., lift=...)`.
  If it wants cohorts, give it `name` and `feature_gains(ctl, wid)` and add it to `CohortPlanner([...])`.
* **New alert**: add a rule to `health.py`. Page only if a human must act now, and say what to look at.
* Statistics live in `stats.py` and use fixed seeds. Keep it that way: winners must be reproducible from the store.

## Scripts

| script | proves |
|---|---|
| `scripts/crash_test.py` | N random SIGKILLs reproduce the uninterrupted run's config and approval trail exactly |
| `scripts/load_test.py` | public_03 throughput, decision lag and memory → `reports/public_03_load.md` |
| `scripts/eval_seeds.py` | mean ± sd of outcomes across seeds (policy comparisons) |
| `scripts/aa_test.py` | false-winner rate, power and cost of the experiment policy on synthetic arms |

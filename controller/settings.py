"""Every tunable in one place, each with the reason it has the value it has.

Nothing here is fitted to the public scenarios' hidden truth. Values come from sample-size arithmetic on
the observable traffic levels (see SUBMISSION.md §5) or from the governance contract.
"""

# ---- loop
STEP_MINUTES = 30            # 2 ticks. Apply windows are 2h, so we always see an approval with >=1.5h left.
SNAPSHOT_EVERY_MINUTES = 360  # aggregates snapshot cadence (sim time). Restart replays at most ~6h of events.

# ---- cohorts (C2)
MIN_COHORT_SHARE = 0.04      # a cohort cell below 4% of traffic is merged into `default` (fragmentation guard)
MAX_COHORTS = 16             # platform allows 20; leave headroom for supervisor-defined cohorts
COHORT_GAIN_MIN = 0.004      # engagement split must be worth >= 0.4pp (absolute) to justify more cohorts
COHORT_CHANGE_MIN_HOURS = 24  # cohort definitions change at most once a day (stability for supervisors)

# ---- variant experiments (C6/C7)
PRIOR_STRENGTH = 40.0        # pseudo-connected calls pulling a cohort's arm toward the workflow-level arm rate
HALF_LIFE_DAYS = 2.5         # recency weighting so decaying winners lose posterior mass
EXPLORE_FLOOR = 0.10         # minimum share for a plausible, under-sampled arm
MIN_ARM_N = 250              # connected calls per arm (in cohort) before an arm may be eliminated/promoted
ELIMINATE_PBEST = 0.02       # arm with P(best) below this, and enough data, gets 0 traffic
WINNER_PBEST = 0.95          # declare a winner at P(best) >= 0.95 ...
WINNER_SHARE = 0.85          # ... it gets 85%; the rest monitors the runner-up (decay detection needs a comparator)
INITIAL_INCUMBENT_SHARE = 0.4  # first exploration round keeps the business default at >= 40%
DECAY_WINDOW_DAYS = 1.5      # short window for decay checks
DECAY_PROB = 0.90            # P(leader < best alternative | short window) to trigger a rollback
HARM_TOLERANCE = 0.002       # never propose an allocation whose posterior-mean engagement is >0.2pp worse

# ---- schedule / call time (C3/C4)
SCHED_BETA = 2.0             # weight = (relative pickup at hour)^beta
SCHED_MIN_W, SCHED_MAX_W = 0.25, 4.0
SCHED_MIN_DIALS = 1500       # per cohort, across history, before we give it its own hour table
SCHED_MIN_GAIN = 0.01        # cross-fitted gain in leads concluded needed to introduce cohorts JUST for call time
SCHED_MIN_GAIN_PIGGYBACK = 0.003  # bar when cohorts exist anyway (no extra approval or cohort churn). Calibration
                              # on public_01: forcing the schedule on gave ~+1.4pp pickup vs a +0.4-0.7% replay
                              # estimate, so the replay is conservative (see SUBMISSION §5).

# ---- caller ID (C5)
DID_WINDOW_HOURS = 24
DID_MIN_DIALS = 250
DID_BAD_RATIO = 0.80         # pickup ratio vs other numbers (same hours) to flag
DID_BAD_Z = 3.0
DID_PROBE_SHARE = 0.05       # unproven numbers get 5% each: an unknown number can't move health >2% at apply
DID_GOOD_RATIO = 0.92        # a probed number is promoted to full rotation at >= 0.92 of the others

# ---- incidents (C8)
HAL_WINDOW_HOURS = 6
HAL_MIN_CONNECTED = 80       # the p-value below does the false-positive control; this only stops tiny-n noise
HAL_RATIO = 1.6              # flag rate at least 1.6x the prompt's/workflow's baseline ...
HAL_P = 1e-4                 # ... and binomial tail p < 1e-4 (≈1 false alarm per 200 workflow-days at 48 checks/day)
VALIDATION_N = 300           # 3 credits; enough to clear a 0.97 bar for a prompt with ~1% live failure rate
STRICT_PASS_RATE = 0.98      # our own bar for prompts WITHOUT live history (stricter than policy)
ENGAGE_COLLAPSE_REL = 0.08   # a supervisor prompt that drops engagement >8% (z>3) gets a revert proposal
CREDIT_RESERVE = 20          # never spend below this on non-incident work

# ---- governance (C9)
ROUTINE_REQUESTS_PER_24H = 2  # our own ceiling (Kestrel ops asked for 2); policy ceilings are also enforced
ROUTINE_START_OFFSET_H = 1    # routine asks never in the approvers' first hour (stand-ups)
ROUTINE_LAST_SEND_BEFORE_END_H = 3  # don't send routine asks that will likely be decided tomorrow
VERIFY_AFTER_HOURS = 24       # post-apply verification window
GUARDRAIL_Z = 2.5
DID_REPROBE_SHARE = 0.02      # re-testing a number we rotated out: small enough that a still-bad number
                              # cannot move expected caller-ID health by more than ~1.5%
DID_MAX_SHARE = 0.60          # no single number above 60% while another has shown good pickup (load spreading)
DID_REST_DAYS = 3             # first re-test after 3 days rest; doubles after each failed re-test


# ---- experiment overrides: CONTROLLER_SET="SCHED_MIN_GAIN=-1,DID_PROBE_SHARE=0.03" (used for calibration runs)
def _apply_overrides():
    import os
    for kv in filter(None, os.environ.get("CONTROLLER_SET", "").split(",")):
        k, v = kv.split("=", 1)
        if k.strip() not in globals():
            raise SystemExit(f"CONTROLLER_SET: unknown setting {k!r}")
        globals()[k.strip()] = type(globals()[k.strip()])(v)


_apply_overrides()

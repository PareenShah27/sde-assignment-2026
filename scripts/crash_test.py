"""Crash-recovery demonstration (C13).

Runs the same scenario twice against fresh simulators:
  A) uninterrupted;
  B) the controller is SIGKILLed at random moments (no graceful shutdown, mid-write, mid-HTTP-call) and restarted
     with the same state dir, N times, then allowed to finish.

Because every decision is a pure function of (event log, our SQLite store) and every side effect carries an
idempotency key written before the call, B must end with exactly the same config history and approval trail as A.
It also checks the platform's invariants directly against the simulator:
  * no proposal applied twice      (config versions with a repeated proposal_id)
  * no lost approval               (approved in the simulator but not applied/lapsed/stale in our store)
  * no duplicate request           (the simulator rejects reused proposal_ids; we count 409s in our store)

  python scripts/crash_test.py public_02_incidents --kills 8 [--seed 7] [--out reports/crash_recovery.md]
"""
import argparse
import json
import os
import random
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def start_sim(scenario, run_dir):
    port = free_port()
    p = subprocess.Popen([PY, "-m", "sim", "serve", "--scenario", scenario, "--run-dir", str(run_dir), "--force",
                          "--port", str(port)], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    for _ in range(240):
        try:
            if httpx.get(url + "/health", timeout=1).status_code == 200:
                return p, url
        except httpx.HTTPError:
            time.sleep(0.5)
    raise RuntimeError("simulator did not start")


def controller(url, state, log):
    return subprocess.Popen([PY, "-m", "controller", "run", "--url", url, "--state-dir", str(state)], cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=log)


def trail(url):
    c = httpx.Client(base_url=url, timeout=60)
    out = {"configs": {}, "approvals": []}
    for w in c.get("/workflows").json():
        wid = w["workflow_id"]
        out["configs"][wid] = [(v["version"], v["actor"], tuple(v["changed_dims"]), v["proposal_id"], v["applied_at"])
                               for v in c.get(f"/workflows/{wid}/config/history").json()]
    out["approvals"] = sorted((a["proposal_id"], a["status"], a["created_at"]) for a in c.get("/approvals").json())
    out["state"] = c.get("/sim/state").json()
    return out


def invariants(t, state_dir):
    problems = []
    for wid, vs in t["configs"].items():
        pids = [v[3] for v in vs if v[3]]
        if len(pids) != len(set(pids)):
            problems.append(f"{wid}: a proposal was applied twice")
    db = sqlite3.connect(Path(state_dir) / "controller.db")
    ours = dict(db.execute("SELECT proposal_id, status FROM proposals"))
    for pid, status, _ in t["approvals"]:
        if status == "approved" and ours.get(pid) not in ("stale", "lapsed", "applied"):
            problems.append(f"lost approval: {pid} approved in simulator, ours={ours.get(pid)}")
        if pid not in ours:
            problems.append(f"unknown request {pid} reached the approver (not in our outbox)")
    dup = db.execute("SELECT COUNT(*) FROM decisions WHERE summary LIKE '%duplicate_proposal_id%'").fetchone()[0]
    restarts = db.execute("SELECT COUNT(*) FROM decisions WHERE kind='controller_restart'").fetchone()[0]
    return problems, dup, restarts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario")
    ap.add_argument("--kills", type=int, default=8)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="reports/crash_recovery.md")
    a = ap.parse_args()
    sc = str(ROOT / "scenarios" / f"{a.scenario}.yaml")
    work = ROOT / "logs" / "crash"
    work.mkdir(parents=True, exist_ok=True)
    rng = random.Random(a.seed)

    # A: uninterrupted
    sim, url = start_sim(sc, work / "run_A")
    subprocess.run(["rm", "-rf", str(work / "state_A")])
    t0 = time.time()
    with open(work / "controller_A.log", "w") as log:
        assert controller(url, work / "state_A", log).wait() == 0
    wall_a = time.time() - t0
    A = trail(url)
    sim.terminate(); sim.wait()

    # B: killed N times
    sim, url = start_sim(sc, work / "run_B")
    subprocess.run(["rm", "-rf", str(work / "state_B")])
    kills = []
    t0 = time.time()
    with open(work / "controller_B.log", "w") as log:
        for i in range(a.kills):
            p = controller(url, work / "state_B", log)
            delay = rng.uniform(0.3, max(1.0, 2.2 * wall_a / a.kills))  # spread kills over the whole run
            time.sleep(delay)
            if p.poll() is not None:
                break
            minute = httpx.get(url + "/sim/state").json()["sim_minute"]
            os.kill(p.pid, signal.SIGKILL)
            p.wait()
            kills.append({"after_s": round(delay, 2), "sim_minute": minute})
        assert controller(url, work / "state_B", log).wait() == 0
    wall_b = time.time() - t0
    B = trail(url)
    sim.terminate(); sim.wait()

    same_cfg = A["configs"] == B["configs"]
    same_apr = A["approvals"] == B["approvals"]
    probs, dup, restarts = invariants(B, work / "state_B")
    from controller.cfg import fmt_minute  # noqa: E402  (display only)
    L = [f"# Crash-recovery test — {a.scenario}", "",
         f"Run A: uninterrupted ({wall_a:.0f}s). Run B: controller SIGKILLed {len(kills)} times at random moments, "
         f"restarted with the same state dir each time ({wall_b:.0f}s, {restarts} restarts recorded in the audit log).",
         "", "Kills (wall-clock after start → simulated time when killed):", ""]
    L += [f"- {k['after_s']}s → {fmt_minute(k['sim_minute'])}" for k in kills]
    L += ["", "| check | result |", "|---|---|",
          f"| config history identical to uninterrupted run (all workflows: version, actor, dims, proposal_id, time) | {'PASS' if same_cfg else 'FAIL'} |",
          f"| approval trail identical (proposal_id, status, time) | {'PASS' if same_apr else 'FAIL'} |",
          f"| no proposal applied twice | {'PASS' if not any('twice' in p for p in probs) else 'FAIL'} |",
          f"| no approval lost (approved ⇒ applied / lapsed / stale in our store) | {'PASS' if not any('lost' in p for p in probs) else 'FAIL'} |",
          f"| every request that reached the approver is in our outbox | {'PASS' if not any('unknown' in p for p in probs) else 'FAIL'} |",
          f"| duplicate-proposal 409s observed (recovered by lookup) | {dup} |",
          "", f"Config versions: A={sum(len(v) for v in A['configs'].values())}, "
              f"B={sum(len(v) for v in B['configs'].values())}; approvals: A={len(A['approvals'])}, B={len(B['approvals'])}; "
              f"credits left: A={A['state']['credits_remaining']}, B={B['state']['credits_remaining']}"]
    if probs:
        L += ["", "Problems:", ""] + [f"- {p}" for p in probs]
    if not same_cfg:
        for wid in A["configs"]:
            if A["configs"][wid] != B["configs"][wid]:
                L += ["", f"First divergence in {wid}:"]
                for x, y in zip(A["configs"][wid] + [None] * 50, B["configs"][wid] + [None] * 50):
                    if x != y:
                        L += [f"- A: {x}", f"- B: {y}"]
                        break
    Path(ROOT / a.out).write_text("\n".join(L) + "\n")
    print("\n".join(L))
    sys.exit(0 if same_cfg and same_apr and not probs else 1)


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()

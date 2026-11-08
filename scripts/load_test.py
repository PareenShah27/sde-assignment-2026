"""Load test on public_03_load (C1/C14): runs the scenario, then reports from the controller's OWN metrics.

The simulator's own compute (inside POST /sim/advance) is reported separately: in production the clock doesn't
wait for a simulator, only for us, so the number that matters is ingest + decide time per simulated day.

  python scripts/load_test.py [--scenario public_03_load] [--out reports/public_03_load.md]
"""
import argparse
import json
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="public_03_load")
    ap.add_argument("--out", default="reports/public_03_load.md")
    a = ap.parse_args()
    sc = ROOT / "scenarios" / f"{a.scenario}.yaml"
    base, run, state = ROOT / f"runs/base_{a.scenario}", ROOT / f"runs/mine_{a.scenario}", ROOT / f"state/{a.scenario}"
    bl = None
    if not (base / "_truth" / "aggregates.json").exists():
        bl = subprocess.Popen([PY, "-m", "sim", "baseline", "--scenario", str(sc), "--run-dir", str(base)], cwd=ROOT,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["rm", "-rf", str(state)], check=True)
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    t_gen = time.time()
    sim = subprocess.Popen([PY, "-m", "sim", "serve", "--scenario", str(sc), "--run-dir", str(run), "--force",
                            "--port", str(port)], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    while True:
        try:
            if httpx.get(url + "/health", timeout=2).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(1)
    t_gen = time.time() - t_gen
    t0 = time.time()
    with open(ROOT / "logs" / f"controller_{a.scenario}.log", "w") as log:
        rc = subprocess.run(["/usr/bin/time", "-v", PY, "-m", "controller", "run", "--url", url, "--state-dir",
                             str(state)], cwd=ROOT, stderr=log).returncode
    wall = time.time() - t0
    sim.terminate(); sim.wait()
    if rc:
        sys.exit(f"controller exited {rc}")
    timev = (ROOT / "logs" / f"controller_{a.scenario}.log").read_text()
    peak = next((int(l.split(":")[1]) / 1024 for l in timev.splitlines() if "Maximum resident set size" in l), None)

    db = sqlite3.connect(state / "controller.db")
    m = lambda k: np.array([v for (v,) in db.execute("SELECT value FROM metrics WHERE key=? ORDER BY sim_minute", (k,))])
    events = m("ingest_events").sum()
    ingest_s = m("ingest_seconds").sum()
    cycle, decide, adv = m("cycle_seconds"), m("decide_seconds"), m("advance_seconds")
    hist_events = db.execute("SELECT value FROM metrics WHERE key='ingest_events' ORDER BY sim_minute LIMIT 1").fetchone()[0]
    days = int(db.execute("SELECT MAX(sim_minute) FROM metrics").fetchone()[0] // 1440)
    mods = {}
    for k, v in db.execute("SELECT key, SUM(value) FROM metrics WHERE key LIKE 'module_seconds:%' GROUP BY key"):
        mods[k.split(":")[1]] = v
    ctl_per_day = (cycle.sum() - cycle[0]) / days  # excluding the history bootstrap cycle
    L = [f"# Load test — {a.scenario}", "",
         f"Machine: {subprocess.run(['nproc'], capture_output=True, text=True).stdout.strip()} vCPU, "
         f"{subprocess.run(['sh', '-c', 'free -g | awk \"/Mem/{print \\$2}\"'], capture_output=True, text=True).stdout.strip()} GB; "
         f"simulator world generation {t_gen:.0f}s (not ours).", "",
         "| measure | value |", "|---|---|",
         f"| events ingested (history + {days} days) | {int(events):,} ({int(hist_events):,} history) |",
         f"| ingest throughput (parse + fold into cubes) | **{events / ingest_s:,.0f} events/s** |",
         f"| controller time per simulated day (ingest + decide, all cycles) | **{ctl_per_day:.1f} s** |",
         f"| decision lag per cycle (ingest + decide), median / p95 / max | {np.median(cycle[1:]):.2f} / "
         f"{np.percentile(cycle[1:], 95):.2f} / {cycle[1:].max():.2f} s |",
         f"| decide-only time per cycle, median / p95 | {np.median(decide[1:]):.2f} / {np.percentile(decide[1:], 95):.2f} s |",
         f"| history bootstrap cycle (all pre-D0 events) | {cycle[0]:.1f} s |",
         f"| simulator time inside /sim/advance per day (not ours) | {adv.sum() / days:.1f} s |",
         f"| end-to-end wall-clock (controller process) | {wall:.0f} s for {days} days = {wall / days:.0f} s/day |",
         f"| peak memory (controller RSS) | **{peak:.0f} MB** |" if peak else "| peak memory | n/a |",
         "", "Time by module (whole run, s): " + ", ".join(f"{k} {v:.0f}" for k, v in sorted(mods.items(), key=lambda x: -x[1])),
         "", "SRE target: one simulated day of public_03 in < 10 wall-clock minutes on 4 vCPU / 8 GB."]
    if bl:
        bl.wait()
    if (base / "_truth" / "aggregates.json").exists():
        g = subprocess.run([PY, "-m", "sim", "grade", "--run", str(run), "--baseline", str(base), "--out",
                            str(ROOT / f"reports/{a.scenario}_grade.md")], cwd=ROOT, capture_output=True, text=True)
        L += ["", f"Grade report: reports/{a.scenario}_grade.md"]
    Path(ROOT / a.out).write_text("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()

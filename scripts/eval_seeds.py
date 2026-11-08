"""Multi-seed evaluation: the same scenario (same truth, same policy) under different random seeds.

One run per variant can't separate a better policy from a lucky path, because every decision changes the
random stream. This runs N seeds in parallel and reports mean and spread of what the grader measures.
It changes ONLY `seed` in a copy of the scenario; it never reads hidden truth into the controller.

  python scripts/eval_seeds.py public_01_dev --seeds 1 2 3 4 [--set LEADER_SWITCH_P=0.5] [--label x]
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def one(name: str, seed: int, overrides: str, label: str) -> dict:
    out = ROOT / "logs" / "eval" / f"{name}_s{seed}"
    out.mkdir(parents=True, exist_ok=True)
    sc = yaml.safe_load(open(ROOT / "scenarios" / f"{name}.yaml"))
    sc["seed"] = seed
    scp = out / "scenario.yaml"
    scp.write_text(yaml.safe_dump(sc))
    base = out / "base"
    if not (base / "_truth" / "aggregates.json").exists():
        subprocess.run([PY, "-m", "sim", "baseline", "--scenario", str(scp), "--run-dir", str(base)],
                       cwd=ROOT, check=True, capture_output=True)
    run, state = out / f"run_{label}", out / f"state_{label}"
    subprocess.run(["rm", "-rf", str(state)], check=True)
    port = free_port()
    sim = subprocess.Popen([PY, "-m", "sim", "serve", "--scenario", str(scp), "--run-dir", str(run), "--force",
                            "--port", str(port)], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        import httpx
        for _ in range(240):
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        env = {**os.environ, "CONTROLLER_SET": overrides}
        t0 = time.time()
        with open(out / f"controller_{label}.log", "w") as log:
            subprocess.run([PY, "-m", "controller", "run", "--url", f"http://127.0.0.1:{port}", "--state-dir",
                            str(state)], cwd=ROOT, env=env, check=True, stderr=log)
        wall = time.time() - t0
    finally:
        sim.terminate()
        sim.wait()
    g = json.loads(subprocess.run([PY, "-m", "sim", "grade", "--run", str(run), "--baseline", str(base), "--json"],
                                  cwd=ROOT, check=True, capture_output=True, text=True).stdout)
    v = g["controller_changes"]["verdicts"]
    decay = [max(d.values()) if d else 0 for d in g["decaying_variant_share_after_onset"].values()]
    late = [np.mean(sorted(d.items(), key=lambda kv: int(kv[0]))[-3:] and
                    [x for _, x in sorted(d.items(), key=lambda kv: int(kv[0]))[-3:]]) if d else 0
            for d in g["decaying_variant_share_after_onset"].values()]
    return {"seed": seed, "wall_s": round(wall), "conclusion": g["delta_vs_baseline"]["ALL"]["conclusion_rate"],
            "engagement": g["delta_vs_baseline"]["ALL"]["engagement_rate"],
            "pickup": g["delta_vs_baseline"]["ALL"]["pickup_rate"],
            "gap": g["kpis"]["ALL"]["engagement_gap_captured"],
            "harmful": v.get("harmful", 0), "changes": g["controller_changes"]["count"],
            "decay_late_share": float(np.mean(late)) if late else None,
            "per_wf": {w: g["delta_vs_baseline"][w]["conclusion_rate"] for w in g["kpis"] if w != "ALL"}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario")
    ap.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3, 4])
    ap.add_argument("--set", default="", help="CONTROLLER_SET overrides, e.g. LEADER_SWITCH_P=0.5")
    ap.add_argument("--label", default="default")
    ap.add_argument("--parallel", type=int, default=4)
    a = ap.parse_args()
    with ThreadPoolExecutor(a.parallel) as ex:
        rs = list(ex.map(lambda s: one(a.scenario, s, a.set, a.label), a.seeds))
    for r in rs:
        print(json.dumps(r))
    m = lambda k: (np.mean([r[k] for r in rs if r[k] is not None]), np.std([r[k] for r in rs if r[k] is not None]))
    print(f"\n{a.scenario} [{a.label}] {a.set or '(defaults)'}  n={len(rs)}")
    for k in ("conclusion", "pickup", "engagement", "gap"):
        mu, sd = m(k)
        print(f"  {k:<11} {mu:+.1%} ± {sd:.1%}")
    print(f"  harmful     {np.mean([r['harmful'] for r in rs]):.2f}/run (of {np.mean([r['changes'] for r in rs]):.0f} changes)")
    dl = [r["decay_late_share"] for r in rs if r["decay_late_share"] is not None]
    if dl:
        print(f"  decayed-variant share, last 3 days: {np.mean(dl):.0%}")


if __name__ == "__main__":
    main()

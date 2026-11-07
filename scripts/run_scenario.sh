#!/usr/bin/env bash
# Full local run of one scenario: baseline -> fresh simulator -> controller -> grade report.
#   scripts/run_scenario.sh public_02_incidents [port]
set -euo pipefail
cd "$(dirname "$0")/.."
NAME="${1:?usage: scripts/run_scenario.sh <scenario name> [port]}"
PORT="${2:-8700}"
PY="${PYTHON:-python3}"
[ -x .venv/bin/python ] && PY=.venv/bin/python
SC="scenarios/${NAME}.yaml"
mkdir -p reports runs logs
[ -f "runs/base_${NAME}/_truth/aggregates.json" ] || "$PY" -m sim baseline --scenario "$SC" --run-dir "runs/base_${NAME}"
rm -rf "state/${NAME}"
"$PY" -m sim serve --scenario "$SC" --run-dir "runs/mine_${NAME}" --force --port "$PORT" > "logs/sim_${NAME}.log" 2>&1 &
SIM_PID=$!
trap 'kill $SIM_PID 2>/dev/null || true' EXIT
for _ in $(seq 1 120); do curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null && break; sleep 1; done
START=$(date +%s)
CONTROLLER_STATE_DIR="state/${NAME}" ./run.sh "http://127.0.0.1:${PORT}" 2> "logs/controller_${NAME}.log"
echo "controller wall-clock: $(( $(date +%s) - START ))s"
"$PY" -m sim grade --run "runs/mine_${NAME}" --baseline "runs/base_${NAME}" --out "reports/${NAME}.md" > /dev/null
echo "report: reports/${NAME}.md"

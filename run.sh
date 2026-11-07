#!/usr/bin/env bash
# Contract: ./run.sh <SIM_URL> starts the controller and runs until the scenario finishes. No Docker needed:
# Python 3.11+ with requirements.txt (numpy, httpx). State goes to $CONTROLLER_STATE_DIR (default
# state/<scenario name>); re-running with the same state dir RESUMES (crash recovery), so use a fresh dir per run.
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
[ -x .venv/bin/python ] && PY=.venv/bin/python
exec "$PY" -m controller run --url "$SIM_URL" ${CONTROLLER_STATE_DIR:+--state-dir "$CONTROLLER_STATE_DIR"}

"""CLI.

  python -m controller run    --url http://127.0.0.1:8700 [--state-dir state/<scenario>]
"""
from __future__ import annotations

import argparse
import logging
import os
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(prog="controller")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="drive the simulator until the scenario finishes")
    r.add_argument("--url", default=os.environ.get("SIM_URL", "http://127.0.0.1:8700"))
    r.add_argument("--state-dir", default=os.environ.get("CONTROLLER_STATE_DIR"))
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if a.cmd == "run":
        from .client import SimClient
        from .loop import Controller
        from .modules import build_modules
        state_dir = a.state_dir or f"state/{SimClient(a.url).state()['scenario']}"
        ctl = Controller(a.url, state_dir)
        ctl.modules = build_modules(ctl)
        ctl.run()


if __name__ == "__main__":
    main()

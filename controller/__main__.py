"""CLI.

  python -m controller run  --url http://127.0.0.1:8700 [--state-dir state/<scenario>]
  python -m controller why  --state-dir state/<scenario> --wf <workflow> --at "D3 14:00"
  python -m controller log  --state-dir state/<scenario> [--wf <workflow>] [--since "D3 00:00"] [--kind proposed]
  python -m controller view   --state-dir state/<scenario> [--wf <workflow>] [--html out.html]
  python -m controller health --state-dir state/<scenario>
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
    w = sub.add_parser("why", help="why was workflow X configured the way it was at time T")
    w.add_argument("--state-dir", required=True)
    w.add_argument("--wf", required=True)
    w.add_argument("--at", required=True, help='"D3 14:00", sim minute, or ISO timestamp')
    w.add_argument("--after-hours", type=int, default=48)
    v = sub.add_parser("view", help="consolidated supervisor view (one screen per workflow)")
    v.add_argument("--state-dir", required=True)
    v.add_argument("--wf")
    v.add_argument("--html", help="also write an HTML file")
    hp = sub.add_parser("health", help="loop health for the on-call engineer")
    hp.add_argument("--state-dir", required=True)
    lg = sub.add_parser("log", help="audit log")
    lg.add_argument("--state-dir", required=True)
    lg.add_argument("--wf")
    lg.add_argument("--since")
    lg.add_argument("--kind")
    lg.add_argument("--limit", type=int, default=500)
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
        return
    from .audit import log_lines, why
    from .cfg import parse_when
    from .store import Store
    store = Store(os.path.join(a.state_dir, "controller.db"))
    if a.cmd == "view":
        from .view import supervisor_view, to_html
        md = supervisor_view(a.state_dir, a.wf)  # "now"; for a past moment use `why --at`
        print(md)
        if a.html:
            open(a.html, "w").write(to_html(md, "Supervisor view"))
        return
    if a.cmd == "health":
        from .view import health
        print(health(a.state_dir))
        return
    if a.cmd == "why":
        print(why(store, a.wf, parse_when(a.at), a.after_hours))
    elif a.cmd == "log":
        print(log_lines(store, a.wf, parse_when(a.since) if a.since else None, a.kind, a.limit))


if __name__ == "__main__":
    main()

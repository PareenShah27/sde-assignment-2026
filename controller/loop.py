"""The always-on loop: ingest -> sync configs -> reconcile governance -> detect -> plan -> advance.

Restart contract (constraint 8): every step is either a pure function of (event log, SQLite state) or an
HTTP side effect guarded by an idempotency key written to SQLite *before* the call. So killing the process
at any line and restarting converges to the same state without double-applying or losing an approval.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

from . import settings as S
from .aggregates import Aggregates
from .cfg import fmt_minute
from .client import SimClient, SimError, TransportError
from .store import Store

log = logging.getLogger("controller")


class Controller:
    def __init__(self, url: str, state_dir: str | Path):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.sim = SimClient(url)
        self.store = Store(self.state_dir / "controller.db")
        self.snap_path = self.state_dir / "aggregates.pkl"
        self.agg = Aggregates.load(self.snap_path)
        self.wfs: dict[str, dict] = {}
        self.now = 0
        self.ts = ""
        self.finished = False
        self._last_snapshot_minute = None
        self._notifications: list[dict] = []
        self.modules: list = []  # filled by build_modules(); each has .tick(ctl)

    # ------------------------------------------------------------------ setup
    def bind_world(self):
        st = self.sim.state()
        scenario = self.store.get("scenario")
        if scenario and scenario != st["scenario"]:
            raise SystemExit(f"state dir belongs to scenario {scenario!r}, simulator runs {st['scenario']!r}. "
                             f"Use a fresh --state-dir.")
        if self.store.get("last_chunk", -1) > self.agg.last_chunk and self.agg.last_chunk >= 0:
            log.info("snapshot at chunk %d older than store cursor; replay will catch up", self.agg.last_chunk)
        for w in self.sim.workflows():
            self.wfs[w["workflow_id"]] = w
            self.agg.ensure(w["workflow_id"], w["options"])
        with self.store.tx():
            self.store.put("scenario", st["scenario"])
            self.store.put("workflows", self.wfs)
        self._set_time(st)
        restarted = self.store.get("started", False)
        with self.store.tx():
            self.store.decide(self.now, self.ts, None, "controller_restart" if restarted else "controller_start",
                              f"controller {'resumed' if restarted else 'started'} at {fmt_minute(self.now)}",
                              {"agg_last_chunk": self.agg.last_chunk, "credits": st["credits_remaining"]})
            self.store.put("started", True)

    def _set_time(self, st: dict):
        self.now = st["sim_minute"]
        self.ts = st["ts"]
        self.finished = st["finished"]
        self.credits = st.get("credits_remaining", getattr(self, "credits", 0))

    # ------------------------------------------------------------------ ingestion (C1)
    def ingest(self) -> int:
        """Fold every chunk after our cursor, in global chunk order. Returns events folded."""
        t0 = time.time()
        pending = sorted(self.sim.chunks("history", self.agg.last_chunk) +
                         self.sim.chunks("events", self.agg.last_chunk), key=lambda c: c["chunk"])
        n = 0
        for ch in pending:
            lines = self.sim.chunk_lines(ch["chunk"])
            n += self.agg.ingest_lines(lines, on_event=self._notifications.append)
            self.agg.last_chunk = ch["chunk"]
        dt = time.time() - t0
        if pending:
            with self.store.tx():
                self.store.put("last_chunk", self.agg.last_chunk)
                self.store.metric(self.now, "ingest_events", n)
                self.store.metric(self.now, "ingest_seconds", dt)
                lag = [self.now - w.last_call_minute for w in self.agg.wf.values() if w.last_call_minute is not None]
                if lag:
                    self.store.metric(self.now, "event_lag_minutes", min(lag))
        return n

    def maybe_snapshot(self, force: bool = False):
        if force or self._last_snapshot_minute is None or self.now - self._last_snapshot_minute >= S.SNAPSHOT_EVERY_MINUTES:
            self.agg.save(self.snap_path)
            self._last_snapshot_minute = self.now

    # ------------------------------------------------------------------ config mirror
    def sync_configs(self) -> dict[str, dict]:
        """Mirror every config version into our store; return {wid: current config payload}.
        Newly-seen supervisor versions are logged and handed to modules via self.new_versions."""
        self.current: dict[str, dict] = {}
        self.new_versions: dict[str, list[dict]] = {}
        for wid in self.wfs:
            cur = self.sim.config(wid)
            self.current[wid] = cur
            known = self.store.max_config_version(wid)
            if cur["version"] <= known:
                continue
            hist = self.sim.config_history(wid)
            fresh = [v for v in hist if v["version"] > known]
            with self.store.tx():
                for v in fresh:
                    cfg = cur["config"] if v["version"] == cur["version"] else self._known_config(wid, v)
                    self.store.record_config(wid, v, cfg, self.now)
                    if known > 0 or v["version"] > 1:
                        self.store.decide(self.now, self.ts, wid, "config_changed",
                                          f"config v{v['version']} by {v['actor']}: {', '.join(v['changed_dims']) or '-'}",
                                          {"applied_at": v["applied_at"], "changed_dims": v["changed_dims"]},
                                          proposal_id=v.get("proposal_id"), approval_id=v.get("approval_id"),
                                          config_version=v["version"])
            self.new_versions[wid] = fresh
        return self.current

    def _known_config(self, wid: str, v: dict):
        if v.get("proposal_id"):
            p = self.store.proposal(v["proposal_id"])
            if p:
                return p["body"]["proposed_config"]
        return None

    # ------------------------------------------------------------------ main loop
    def step_once(self):
        """One cycle. Its wall time is the controller's decision lag: in production, the delay between events
        landing and the decisions that react to them (on top of the STEP_MINUTES batching)."""
        t0 = time.time()
        n = self.ingest()
        t_ing = time.time() - t0
        self.sync_configs()
        self.drafts = defaultdict(list)
        timings = {}
        for m in self.modules:
            tm = time.time()
            try:
                m.tick(self)
            except (SimError, TransportError) as e:
                # One module's failure (simulator 4xx/5xx) must not stop the loop or the other modules.
                log.warning("%s failed at %s: %s", type(m).__name__, fmt_minute(self.now), e)
                with self.store.tx():
                    self.store.alert(self.now, "warn", None, f"module_error:{type(m).__name__}", str(e)[:300])
            timings[type(m).__name__] = time.time() - tm
        self._notifications.clear()
        self.maybe_snapshot()
        cycle = time.time() - t0
        with self.store.tx():
            self.store.metric(self.now, "cycle_seconds", cycle)
            self.store.metric(self.now, "decide_seconds", cycle - t_ing)
            if n and t_ing > 0:
                self.store.metric(self.now, "ingest_events_per_s", n / t_ing)
            self.store.metric(self.now, "rss_mb", _rss_mb())
            self.store.metric(self.now, "decisions_total", self.store.db.execute(
                "SELECT COUNT(*) FROM decisions").fetchone()[0])
            for k, v in timings.items():
                if v > 0.05:
                    self.store.metric(self.now, f"module_seconds:{k}", v)

    def run(self):
        self._run_t0 = time.time()
        self.bind_world()
        while True:
            self.step_once()
            if self.finished:
                break
            t0 = time.time()
            try:
                st = self.sim.advance(S.STEP_MINUTES)
            except TransportError:
                # We don't know whether the advance happened. Re-read state; the next ingest catches up.
                st = self.sim.state()
            except SimError as e:
                if e.code == "finished":
                    st = self.sim.state()
                else:
                    raise
            self._set_time(st)
            with self.store.tx():
                self.store.metric(self.now, "advance_seconds", time.time() - t0)
                self.store.metric(self.now, "credits_remaining", self.credits)
            if self.now % 1440 == 0:
                day_wall = time.time() - getattr(self, "_day_t0", self._run_t0)
                self._day_t0 = time.time()
                with self.store.tx():
                    self.store.metric(self.now, "wall_seconds_per_sim_day", day_wall)
                log.info("%s  events=%d  credits=%.0f  day took %.1fs  rss=%.0fMB", fmt_minute(self.now),
                         self.agg.events, self.credits, day_wall, _rss_mb())
        # final pass so the last chunk's events and decisions are recorded
        self.step_once()
        self.maybe_snapshot(force=True)
        with self.store.tx():
            self.store.decide(self.now, self.ts, None, "controller_finished", "scenario finished",
                              {"events": self.agg.events, "credits_remaining": self.credits,
                               "wall_seconds": round(time.time() - self._run_t0, 1), "peak_rss_mb": round(_peak_mb())})


def _rss_mb() -> float:
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 2**20
    except (OSError, ValueError):
        return _peak_mb()


def _peak_mb() -> float:
    import resource
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / 1024 if sys.platform != "darwin" else r / 2**20

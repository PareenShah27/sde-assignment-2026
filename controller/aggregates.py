"""Event ingestion into compact per-workflow count cubes.

Design
------
Raw events (up to ~850k/day) are never stored. Each call is reduced into a handful of dense numpy count
cubes keyed by the lead's *feature code* (lang x city_tier x age_band x value_band = 252 cells). Because every
cohort definition the controller can express is a union of feature cells, any cohort — today's, a candidate
one, or one a supervisor defines tomorrow — can be evaluated retrospectively from these cubes. That is what
makes "one shared cohort object" cheap: cohorts are views over cells, not separate pipelines.

Cubes, per workflow, per sim day (day 0 == D0, history days are negative):
  pick[day]  (24 hour, 252 code, n_caller_id, 2)          dials, connected            -> pickup / caller-ID
  eng[day]   (252 code, n_welcome, n_voice, n_prompt, 3)  connected, engaged, halluc_flag -> engagement
  hal[day]   (24 hour, n_prompt, 2)                       connected, halluc_flag        -> incident detection
  leads[day] (252,) leads uploaded;  concl[campaign day] (252,) leads concluded          -> affected leads

Crash safety: aggregates are a pure function of the ordered event log. We snapshot them (atomic rename)
together with the last chunk number folded in; on restart we load the snapshot and replay later chunks.
Chunks are processed whole, so a chunk is either fully in a snapshot or fully replayed: no double counting.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pickle
from pathlib import Path

import numpy as np

FEATURES: dict[str, list[str]] = {
    # The value sets come from the API contract (docs/SIMULATOR.md + lead events). Unknown values map to
    # the first entry's slot only if they ever appear; we count them so it is visible.
    "lang": ["hi", "en", "ta", "te", "kn", "mr", "bn"],
    "city_tier": ["1", "2", "3"],
    "age_band": ["18-30", "31-45", "46-60", "60+"],
    "value_band": ["low", "mid", "high"],
}
FEAT_NAMES = list(FEATURES)
FEAT_SIZES = [len(FEATURES[f]) for f in FEAT_NAMES]
NCODE = int(np.prod(FEAT_SIZES))
_FIDX = {f: {v: i for i, v in enumerate(vals)} for f, vals in FEATURES.items()}
# code -> per-feature value index, shape (NCODE, 4)
CODE_FEATS = np.array(np.unravel_index(np.arange(NCODE), FEAT_SIZES)).T

D0 = dt.date(2027, 1, 4)  # contract: D0 00:00 == 2027-01-04T00:00+05:30


def encode(features: dict) -> int:
    idx = []
    for f in FEAT_NAMES:
        idx.append(_FIDX[f].get(str(features.get(f)), 0))
    return int(np.ravel_multi_index(idx, FEAT_SIZES))


def cells_where(where: dict) -> np.ndarray:
    """Boolean mask over codes for a cohort `where` clause (AND across features, OR within)."""
    m = np.ones(NCODE, dtype=bool)
    for f, vals in (where or {}).items():
        fi = FEAT_NAMES.index(f)
        ok = [_FIDX[f][v] for v in vals if v in _FIDX[f]]
        m &= np.isin(CODE_FEATS[:, fi], ok)
    return m


class WfAgg:
    def __init__(self, wid: str, options: dict):
        self.wid = wid
        self.opts = {d: list(v) for d, v in options.items()}
        self.ix = {d: {o: i for i, o in enumerate(v)} for d, v in self.opts.items()}
        self.nD = len(self.opts["caller_id"])
        self.nW = len(self.opts["welcome_message"])
        self.nV = len(self.opts["voice"])
        self.nP = len(self.opts["prompt_version"])
        self.pick: dict[int, np.ndarray] = {}
        self.eng: dict[int, np.ndarray] = {}
        self.hal: dict[int, np.ndarray] = {}
        self.leads: dict[int, np.ndarray] = {}
        self.concl: dict[int, np.ndarray] = {}
        self.calls = 0
        self.unknown_person = 0
        self.last_call_minute = None

    def _arr(self, store: dict, day: int, shape):
        a = store.get(day)
        if a is None:
            a = store[day] = np.zeros(shape, dtype=np.int32)
        return a

    # --- windowed views
    def days(self) -> list[int]:
        return sorted(self.pick)

    def pick_sum(self, d0: int, d1: int) -> np.ndarray:
        """Sum of pick cube over days [d0, d1]."""
        out = np.zeros((24, NCODE, self.nD, 2), dtype=np.int64)
        for d, a in self.pick.items():
            if d0 <= d <= d1:
                out += a
        return out

    def eng_sum(self, d0: int, d1: int, weights: dict[int, float] | None = None) -> np.ndarray:
        out = np.zeros((NCODE, self.nW, self.nV, self.nP, 3), dtype=np.float64)
        for d, a in self.eng.items():
            if d0 <= d <= d1:
                out += a * (weights.get(d, 0.0) if weights else 1.0)
        return out


class Aggregates:
    def __init__(self):
        self.wf: dict[str, WfAgg] = {}
        self.persons: dict[str, int] = {}
        self.last_chunk = -1
        self.last_event_id = 0
        self.events = 0
        self._tscache: dict[str, tuple[int, int, int]] = {}

    def ensure(self, wid: str, options: dict) -> WfAgg:
        if wid not in self.wf:
            self.wf[wid] = WfAgg(wid, options)
        return self.wf[wid]

    def ts_parts(self, ts: str) -> tuple[int, int, int]:
        """'2027-01-04T09:07:00+05:30' -> (day, hour, minute_of_sim). Cached per hour string."""
        key = ts[:13]
        r = self._tscache.get(key)
        if r is None:
            d = dt.date(int(ts[0:4]), int(ts[5:7]), int(ts[8:10]))
            day = (d - D0).days
            hour = int(ts[11:13])
            r = self._tscache[key] = (day, hour, day * 1440 + hour * 60)
        return r[0], r[1], r[2] + int(ts[14:16])

    def ingest_lines(self, lines, on_event=None) -> int:
        """Fold NDJSON lines into the cubes. Non-call/lead events are handed to `on_event`.
        Returns number of events processed. Idempotence is by chunk (see module doc), plus an event_id guard."""
        per_wf: dict[str, list] = {}
        n = 0
        persons = self.persons
        for line in lines:
            if not line:
                continue
            ev = json.loads(line)
            eid = ev.get("event_id", 0)
            if eid and eid <= self.last_event_id:
                continue  # already folded (defensive; chunks are the unit of idempotence)
            self.last_event_id = max(self.last_event_id, eid)
            n += 1
            t = ev["type"]
            if t == "call":
                wid = ev["workflow_id"]
                w = self.wf.get(wid)
                if w is None:
                    continue
                code = persons.get(ev["person_id"])
                if code is None:
                    w.unknown_person += 1
                    continue
                day, hour, minute = self.ts_parts(ev["ts"])
                var = ev["variant"]
                conn = ev["connected"]
                qa = ev.get("qa") or {}
                per_wf.setdefault(wid, []).append((
                    day, hour, code, w.ix["caller_id"].get(var["caller_id"], 0),
                    w.ix["welcome_message"].get(var["welcome_message"], 0), w.ix["voice"].get(var["voice"], 0),
                    w.ix["prompt_version"].get(var["prompt_version"], 0), 1 if conn else 0,
                    1 if ev["engaged"] else 0, 1 if qa.get("hallucination_flag") else 0,
                    day - int(ev.get("campaign_day_index", 0)), minute))
            elif t == "lead":
                code = encode(ev["features"])
                persons[ev["person_id"]] = code
                w = self.wf.get(ev["workflow_id"])
                if w is not None:
                    day, _, _ = self.ts_parts(ev["ts"])
                    w._arr(w.leads, day, (NCODE,))[code] += 1
            elif on_event is not None:
                on_event(ev)
        for wid, rows in per_wf.items():
            self._fold(self.wf[wid], np.array(rows, dtype=np.int64))
        self.events += n
        return n

    def _fold(self, w: WfAgg, R: np.ndarray):
        day, hour, code, di, wi, vi, pi, conn, eng, flag, cday, minute = R.T
        w.calls += len(R)
        w.last_call_minute = int(minute.max()) if w.last_call_minute is None else max(w.last_call_minute, int(minute.max()))
        for d in np.unique(day):
            s = day == d
            d = int(d)
            P = w._arr(w.pick, d, (24, NCODE, w.nD, 2))
            np.add.at(P, (hour[s], code[s], di[s], 0), 1)
            np.add.at(P, (hour[s], code[s], di[s], 1), conn[s])
            c = s & (conn == 1)
            if c.any():
                E = w._arr(w.eng, d, (NCODE, w.nW, w.nV, w.nP, 3))
                np.add.at(E, (code[c], wi[c], vi[c], pi[c], 0), 1)
                np.add.at(E, (code[c], wi[c], vi[c], pi[c], 1), eng[c])
                np.add.at(E, (code[c], wi[c], vi[c], pi[c], 2), flag[c])
                H = w._arr(w.hal, d, (24, w.nP, 2))
                np.add.at(H, (hour[c], pi[c], 0), 1)
                np.add.at(H, (hour[c], pi[c], 1), flag[c])
        e = eng == 1
        for cd in np.unique(cday[e]):
            s = e & (cday == cd)
            np.add.at(w._arr(w.concl, int(cd), (NCODE,)), code[s], 1)

    # ---- snapshot
    def save(self, path: str | Path):
        path = Path(path)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            pickle.dump({k: v for k, v in self.__dict__.items() if k != "_tscache"}, f,
                        protocol=pickle.HIGHEST_PROTOCOL)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: str | Path) -> "Aggregates":
        a = cls()
        p = Path(path)
        if p.exists():
            with open(p, "rb") as f:
                a.__dict__.update(pickle.load(f))
        return a

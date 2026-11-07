"""Durable controller state in one SQLite file (WAL mode).

What lives here (and therefore survives a kill -9):
  * kv            — cursors and small scalars
  * decisions     — the append-only audit log: every detection, plan, proposal, decision, apply, rollback
  * proposals     — the proposal outbox/state machine (written BEFORE any HTTP side effect)
  * config_versions — our own copy of every workflow config version we observed, with the full config,
                    so "what did customer X hear on day D and why" is answerable without the simulator
  * wf_state      — per-workflow controller memory (experiment state, DID health, incidents) as JSON
  * credits       — ledger of every credit we spent and why
  * metrics       — loop health time-series (ingest rate, lag, decisions, credits)
  * alerts        — what we would page/notify on

Event aggregates are NOT here: they are a deterministic function of the event log and are rebuilt from a
snapshot + replay (see aggregates.py). That keeps per-event writes out of SQLite.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sim_minute INTEGER NOT NULL, ts TEXT NOT NULL, workflow_id TEXT, kind TEXT NOT NULL,
  summary TEXT NOT NULL, detail TEXT, proposal_id TEXT, approval_id TEXT, config_version INTEGER,
  wall_time REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS decisions_wf ON decisions(workflow_id, sim_minute);
CREATE INDEX IF NOT EXISTS decisions_pid ON decisions(proposal_id);
CREATE TABLE IF NOT EXISTS proposals (
  proposal_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, kind TEXT NOT NULL, route TEXT NOT NULL,
  status TEXT NOT NULL, base_version INTEGER NOT NULL, dims TEXT NOT NULL, body TEXT NOT NULL,
  approval_id TEXT, reasons TEXT, applied_version INTEGER, meta TEXT,
  created_minute INTEGER NOT NULL, updated_minute INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS proposals_wf ON proposals(workflow_id, status);
CREATE TABLE IF NOT EXISTS config_versions (
  workflow_id TEXT NOT NULL, version INTEGER NOT NULL, actor TEXT, changed_dims TEXT, applied_at TEXT,
  proposal_id TEXT, approval_id TEXT, config TEXT, seen_minute INTEGER,
  PRIMARY KEY (workflow_id, version)
);
CREATE TABLE IF NOT EXISTS wf_state (workflow_id TEXT PRIMARY KEY, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS credits (
  id INTEGER PRIMARY KEY AUTOINCREMENT, sim_minute INTEGER, workflow_id TEXT, what TEXT, cost REAL,
  purpose TEXT, result TEXT
);
CREATE TABLE IF NOT EXISTS metrics (sim_minute INTEGER NOT NULL, key TEXT NOT NULL, value REAL,
  PRIMARY KEY (sim_minute, key));
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, sim_minute INTEGER, severity TEXT, workflow_id TEXT,
  code TEXT, message TEXT, resolved_minute INTEGER
);
"""


def _j(o) -> str:
    return json.dumps(o, sort_keys=True, default=str)


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")  # durable across process crashes (our failure model)
        self.db.executescript(SCHEMA)
        self._in_tx = False

    # ---- transactions
    def tx(self):
        store = self

        class _Tx:
            def __enter__(self):
                if not store._in_tx:
                    store.db.execute("BEGIN IMMEDIATE")
                    store._in_tx = True
                    self.outer = True
                else:
                    self.outer = False
                return store

            def __exit__(self, et, e, tb):
                if self.outer:
                    store.db.execute("COMMIT" if et is None else "ROLLBACK")
                    store._in_tx = False
                return False
        return _Tx()

    # ---- kv
    def get(self, key: str, default=None):
        r = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def put(self, key: str, value):
        self.db.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, _j(value)))

    # ---- audit
    def decide(self, sim_minute: int, ts: str, workflow_id: str | None, kind: str, summary: str,
               detail=None, proposal_id=None, approval_id=None, config_version=None) -> int:
        cur = self.db.execute(
            "INSERT INTO decisions(sim_minute,ts,workflow_id,kind,summary,detail,proposal_id,approval_id,"
            "config_version,wall_time) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (sim_minute, ts, workflow_id, kind, summary, _j(detail) if detail is not None else None,
             proposal_id, approval_id, config_version, time.time()))
        return cur.lastrowid

    def decisions(self, workflow_id=None, since=None, until=None, kind=None, limit=500) -> list[dict]:
        q, a = "SELECT * FROM decisions WHERE 1=1", []
        if workflow_id:
            q += " AND workflow_id=?"; a.append(workflow_id)
        if since is not None:
            q += " AND sim_minute>=?"; a.append(since)
        if until is not None:
            q += " AND sim_minute<=?"; a.append(until)
        if kind:
            q += " AND kind=?"; a.append(kind)
        q += " ORDER BY id LIMIT ?"; a.append(limit)
        return [dict(r) for r in self.db.execute(q, a)]

    # ---- proposals
    def add_proposal(self, p: dict):
        self.db.execute(
            "INSERT INTO proposals(proposal_id,workflow_id,kind,route,status,base_version,dims,body,approval_id,"
            "reasons,applied_version,meta,created_minute,updated_minute) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p["proposal_id"], p["workflow_id"], p["kind"], p["route"], p["status"], p["base_version"],
             _j(p["dims"]), _j(p["body"]), p.get("approval_id"), _j(p.get("reasons") or []),
             p.get("applied_version"), _j(p.get("meta") or {}), p["created_minute"], p["created_minute"]))

    def update_proposal(self, pid: str, minute: int, **fields):
        sets, args = [], []
        for k, v in fields.items():
            sets.append(f"{k}=?")
            args.append(_j(v) if k in ("reasons", "meta", "dims", "body") else v)
        sets.append("updated_minute=?"); args.append(minute)
        args.append(pid)
        self.db.execute(f"UPDATE proposals SET {', '.join(sets)} WHERE proposal_id=?", args)

    def proposal(self, pid: str) -> dict | None:
        r = self.db.execute("SELECT * FROM proposals WHERE proposal_id=?", (pid,)).fetchone()
        return _prop(r) if r else None

    def proposals(self, workflow_id=None, statuses=None) -> list[dict]:
        q, a = "SELECT * FROM proposals WHERE 1=1", []
        if workflow_id:
            q += " AND workflow_id=?"; a.append(workflow_id)
        if statuses:
            q += f" AND status IN ({','.join('?' * len(statuses))})"; a += list(statuses)
        return [_prop(r) for r in self.db.execute(q + " ORDER BY created_minute, proposal_id", a)]

    # ---- config mirror
    def record_config(self, wid: str, v: dict, config: dict | None, minute: int):
        self.db.execute(
            "INSERT OR IGNORE INTO config_versions(workflow_id,version,actor,changed_dims,applied_at,proposal_id,"
            "approval_id,config,seen_minute) VALUES(?,?,?,?,?,?,?,?,?)",
            (wid, v["version"], v.get("actor"), _j(v.get("changed_dims") or []), v.get("applied_at"),
             v.get("proposal_id"), v.get("approval_id"), _j(config) if config is not None else None, minute))
        if config is not None:
            self.db.execute("UPDATE config_versions SET config=? WHERE workflow_id=? AND version=? AND config IS NULL",
                            (_j(config), wid, v["version"]))

    def config_versions(self, wid: str) -> list[dict]:
        out = []
        for r in self.db.execute("SELECT * FROM config_versions WHERE workflow_id=? ORDER BY version", (wid,)):
            d = dict(r)
            d["changed_dims"] = json.loads(d["changed_dims"] or "[]")
            d["config"] = json.loads(d["config"]) if d["config"] else None
            out.append(d)
        return out

    def max_config_version(self, wid: str) -> int:
        r = self.db.execute("SELECT MAX(version) FROM config_versions WHERE workflow_id=?", (wid,)).fetchone()
        return r[0] or 0

    # ---- per-workflow memory
    def wf_state(self, wid: str) -> dict:
        r = self.db.execute("SELECT state FROM wf_state WHERE workflow_id=?", (wid,)).fetchone()
        return json.loads(r[0]) if r else {}

    def save_wf_state(self, wid: str, state: dict):
        self.db.execute("INSERT INTO wf_state(workflow_id,state) VALUES(?,?) ON CONFLICT(workflow_id) "
                        "DO UPDATE SET state=excluded.state", (wid, _j(state)))

    # ---- credits / metrics / alerts
    def spend(self, minute: int, wid: str, what: str, cost: float, purpose: str, result=None):
        self.db.execute("INSERT INTO credits(sim_minute,workflow_id,what,cost,purpose,result) VALUES(?,?,?,?,?,?)",
                        (minute, wid, what, cost, purpose, _j(result)))

    def credits_spent(self, wid: str | None = None) -> float:
        if wid:
            r = self.db.execute("SELECT COALESCE(SUM(cost),0) FROM credits WHERE workflow_id=?", (wid,)).fetchone()
        else:
            r = self.db.execute("SELECT COALESCE(SUM(cost),0) FROM credits").fetchone()
        return float(r[0])

    def metric(self, minute: int, key: str, value: float):
        self.db.execute("INSERT INTO metrics(sim_minute,key,value) VALUES(?,?,?) ON CONFLICT(sim_minute,key) "
                        "DO UPDATE SET value=excluded.value", (minute, key, float(value)))

    def alert(self, minute: int, severity: str, wid: str | None, code: str, message: str):
        open_ = self.db.execute("SELECT id FROM alerts WHERE workflow_id IS ? AND code=? AND resolved_minute IS NULL",
                                (wid, code)).fetchone()
        if open_:
            return open_[0]
        return self.db.execute("INSERT INTO alerts(sim_minute,severity,workflow_id,code,message) VALUES(?,?,?,?,?)",
                               (minute, severity, wid, code, message)).lastrowid

    def resolve_alert(self, minute: int, wid: str | None, code: str):
        self.db.execute("UPDATE alerts SET resolved_minute=? WHERE workflow_id IS ? AND code=? AND resolved_minute IS NULL",
                        (minute, wid, code))


def _prop(r) -> dict:
    d = dict(r)
    for k in ("dims", "body", "reasons", "meta"):
        d[k] = json.loads(d[k]) if d[k] else None
    return d

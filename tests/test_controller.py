"""Controller tests. Tests (unlike the controller) may import sim/ to drive a real in-process world.

Run: python -m pytest -q tests
"""
import copy
import random

import pytest
import yaml
from fastapi.testclient import TestClient

from controller import cfg as C
from controller.governance import Draft, Governance, apply_patch
from controller.loop import Controller
from sim import server
from sim.world import changed_dims as sim_changed_dims, max_shift as sim_max_shift, normalise_config


# ---------------------------------------------------------------- fixtures
@pytest.fixture()
def ctl(tmp_path):
    sc = yaml.safe_load(open("scenarios/public_02_incidents.yaml"))
    sc["days"], sc["history_days"] = 3, 2
    sc["incidents"] = []
    p = tmp_path / "sc.yaml"
    p.write_text(yaml.safe_dump(sc))
    server.init_world(str(p), str(tmp_path / "run"))
    c = Controller("http://testserver", tmp_path / "state")
    c.sim.http = TestClient(server.app)
    c.modules = []
    c.bind_world()
    c.step_once()
    return c


def world():
    return server._world


def advance_to(ctl, minute):
    while ctl.now < minute:
        ctl._set_time(ctl.sim.advance(15))
    ctl.step_once()


# ---------------------------------------------------------------- config parity with the contract
def test_changed_dims_and_shift_match_contract():
    wf = next(w for t in yaml.safe_load(open("scenarios/public_01_dev.yaml"))["tenants"] for w in t["workflows"]
              if w["id"] == "nw_renewal")
    rnd = random.Random(7)
    base = normalise_config(wf, {**wf["default_config"], "cohorts": [], "schedule": {}})
    for _ in range(200):
        new = copy.deepcopy(base)
        if rnd.random() < 0.5:
            new["cohorts"] = [{"id": "old", "where": {"age_band": ["60+"]}}]
            new["allocations"]["old"] = {"voice": {"V1": rnd.random(), "V3": rnd.random() + 0.01}}
        if rnd.random() < 0.5:
            new["allocations"]["default"]["caller_id"] = {"D1": rnd.random() + 0.01, "D3": rnd.random()}
        if rnd.random() < 0.3:
            new["prompt_version"] = "P3"
        if rnd.random() < 0.3:
            new["schedule"] = {"hour_weights": {"default": {str(h): 1 + (h % 3) for h in range(9, 21)}}}
        n = normalise_config(wf, new)
        assert C.changed_dims(base, n) == sim_changed_dims(base, n)
        assert abs(C.max_shift(base, n) - sim_max_shift(base, n)) < 1e-9


def test_step_toward_respects_max_shift():
    a = {"allocations": {"default": {"voice": {"V1": 1.0}, "welcome_message": {"W1": 1}, "caller_id": {"D1": 1}}}}
    b = copy.deepcopy(a)
    b["allocations"]["default"]["voice"] = {"V2": 1.0}
    s = C.step_toward(a, b, ["default"], C.AGENT_DIMS, 0.2)
    assert abs(C.max_shift(a, s) - 0.2) < 1e-3


def test_auto_step_never_exceeds_max_shift_after_rounding():
    """Regression: a 0.2 step rounded to 4dp came out at 0.2000x and the boundary refused it every cycle."""
    rnd = random.Random(3)
    for _ in range(300):
        k = rnd.randint(2, 4)
        cur = {"allocations": {"default": {"voice": {"V1": 1}, "welcome_message": {"W1": 1},
                                           "caller_id": C.normalised({f"D{i}": rnd.random() + 0.01 for i in range(k)})}}}
        tgt = copy.deepcopy(cur)
        tgt["allocations"]["default"]["caller_id"] = C.normalised({f"D{i}": rnd.random() for i in range(k)} | {"D0": 0})
        if not tgt["allocations"]["default"]["caller_id"]:
            continue
        margin = 0.002
        s = C.clean(C.step_toward(C.clean(cur), tgt, ["default"], C.AGENT_DIMS, 0.2 - margin))
        assert C.max_shift(C.clean(cur), s) <= 0.2 + 1e-9


# ---------------------------------------------------------------- governance crash safety
def _voice_draft(ctl, wid="nw_renewal"):
    return Draft(wid=wid, kind="variant_allocation", summary="test voices",
                 patch={"allocations": {"default": {"voice": {"V1": 0.5, "V2": 0.5}}}},
                 rationale="test", dry_run={"x": 1})


def test_unsent_intent_is_sent_on_recovery(ctl, monkeypatch):
    advance_to(ctl, 11 * 60 + 30)
    gov = Governance(ctl)
    # crash right after writing the intent: the HTTP call never happens
    monkeypatch.setattr(gov, "_send", lambda *a, **k: None)
    gov.route(ctl, "nw_renewal", [_voice_draft(ctl)])
    [p] = ctl.store.proposals(workflow_id="nw_renewal")
    assert p["status"] == "intent" and not world().approvals
    # restart: a fresh Governance reconciles
    Governance(ctl).reconcile(ctl)
    p = ctl.store.proposal(p["proposal_id"])
    assert p["status"] == "pending" and len(world().approvals) == 1


def test_sent_but_response_lost_is_not_resent(ctl, monkeypatch):
    advance_to(ctl, 11 * 60 + 30)
    gov = Governance(ctl)
    real_send = ctl.sim.request_approval

    def lost(body):
        real_send(body)  # reaches the simulator ...
        from controller.client import TransportError
        raise TransportError("connection reset")  # ... but we never see the response

    monkeypatch.setattr(ctl.sim, "request_approval", lost)
    gov.route(ctl, "nw_renewal", [_voice_draft(ctl)])
    [p] = ctl.store.proposals(workflow_id="nw_renewal")
    assert p["status"] == "intent"
    monkeypatch.setattr(ctl.sim, "request_approval", real_send)
    Governance(ctl).reconcile(ctl)
    assert ctl.store.proposal(p["proposal_id"])["status"] == "pending"
    assert len(world().approvals) == 1  # exactly one request reached the approver


def test_approved_change_is_not_applied_over_supervisor_edit(ctl):
    advance_to(ctl, 11 * 60 + 30)
    gov = Governance(ctl)
    gov.route(ctl, "nw_renewal", [_voice_draft(ctl)])
    [p] = ctl.store.proposals(workflow_id="nw_renewal")
    a = world().approvals[p["approval_id"]]
    a["_decision"], a["_reasons"] = "approved", []
    # supervisor edits the config while the request is pending
    w = world().wfs["nw_renewal"]
    edited = copy.deepcopy(w.current["config"])
    edited["prompt_version"] = "P3"
    world()._apply(w, normalise_config(w.spec, edited), "supervisor", None, None)
    advance_to(ctl, a["decide_minute"] + 15)
    gov.reconcile(ctl)
    p = ctl.store.proposal(p["proposal_id"])
    assert p["status"] == "stale"
    assert w.current["config"]["prompt_version"] == "P3"  # the supervisor's edit survives


def test_routine_requests_respect_budget_and_hours(ctl):
    gov = Governance(ctl)
    advance_to(ctl, 9 * 60)  # before approvers' hours + offset: nothing is sent
    gov.route(ctl, "nw_renewal", [_voice_draft(ctl)])
    assert not ctl.store.proposals(workflow_id="nw_renewal")
    advance_to(ctl, 11 * 60 + 30)
    for i in range(4):
        d = _voice_draft(ctl)
        d.patch = {"allocations": {"default": {"voice": {"V1": 0.5, f"V{2 + i % 3}": 0.5}}}}
        # pretend each earlier request was decided so the next is not blocked by "one in flight"
        for p in ctl.store.proposals(workflow_id="nw_renewal", statuses=("pending",)):
            ctl.store.update_proposal(p["proposal_id"], ctl.now, status="rejected")
        gov.route(ctl, "nw_renewal", [d])
    sent = [p for p in ctl.store.proposals(workflow_id="nw_renewal") if p["status"] != "intent"]
    assert len(sent) == 2  # our routine ceiling, below the policy's 3


def test_apply_patch_drops_tables_of_removed_cohorts():
    cfg = {"cohorts": [{"id": "a", "where": {"lang": ["hi"]}}],
           "allocations": {"default": {"voice": {"V1": 1}, "welcome_message": {"W1": 1}, "caller_id": {"D1": 1}},
                           "a": {"voice": {"V2": 1}}},
           "prompt_version": "P1", "schedule": {"hour_weights": {"a": {"9": 1}}, "lead_preferred_hour": {}}}
    out = apply_patch(cfg, {"cohorts": []})
    assert "a" not in out["allocations"] and "a" not in out["schedule"]["hour_weights"]


# ---------------------------------------------------------------- caller ID
def test_did_target_excludes_unhealthy_caps_and_probes():
    from controller.callerid import CallerID
    dids = {"D1": {"state": "healthy"}, "D2": {"state": "unhealthy"},
            "D3": {"state": "probing", "last": {"dials": 300, "ratio": 1.1}}, "D4": {"state": "unknown"}}
    t = CallerID().target(dids, {"D1": 0.5, "D2": 0.5})
    assert "D2" not in t
    assert t["D1"] <= 0.6 + 1e-9                 # load-spreading cap
    assert abs(t["D4"] - 0.05) < 1e-9            # unproven number probed at 5%
    assert abs(sum(t.values()) - 1) < 1e-3
    dids["D2"] = {"state": "probing", "reprobe": True}
    assert abs(CallerID().target(dids, {"D1": 1})["D2"] - 0.02) < 1e-9  # re-test at 2%


def test_did_stats_flags_a_bad_number_within_hour_cells():
    import numpy as np
    from controller.aggregates import WfAgg
    from controller.callerid import did_stats, robust_stats
    w = WfAgg("x", {"caller_id": ["D1", "D2", "D3"], "welcome_message": ["W1"], "voice": ["V1"],
                    "prompt_version": ["P1"]})
    rng = np.random.default_rng(0)
    P = np.zeros((24, 252, 3, 2), dtype=np.int32)
    for h in range(9, 21):
        base = 0.15 if h < 15 else 0.35          # strong time-of-day effect, same for all numbers
        for i, health in enumerate([1.0, 0.4, 1.0]):
            P[h, 0, i, 0] = 100
            P[h, 0, i, 1] = rng.binomial(100, base * health)
    w.pick[0] = P
    naive = did_stats(w, 0, 1440)
    assert naive["D1"]["ratio"] > 1.3            # a bad number flatters the others in a naive comparison ...
    s = robust_stats(w, 0, 1440)
    assert s["D2"]["ratio"] < 0.6 and s["D2"]["z"] < -3
    assert 0.8 < s["D1"]["ratio"] < 1.2          # ... which the second pass removes


# ---------------------------------------------------------------- experiments
def _post(n, x, prior=0.55):
    import numpy as np
    from controller.variants import Posterior
    return Posterior(np.array(n, float), np.array(x, float), np.full(len(n), prior))


def test_first_exposure_gets_exactly_the_explore_floor():
    from controller import settings as S
    from controller.variants import allocate
    d, diag = allocate(_post([3000, 0, 0, 0], [1650, 0, 0, 0]), ["V1", "V2", "V3", "V4"], {"V1": 1.0})
    assert diag["leader"] == "V1"                       # an untested arm never leads
    assert all(abs(d[o] - S.EXPLORE_FLOOR) < 1e-9 for o in ("V2", "V3", "V4"))
    assert abs(d["V1"] - (1 - 3 * S.EXPLORE_FLOOR)) < 1e-9


def test_clear_winner_is_promoted_and_runner_up_kept_for_decay_monitoring():
    from controller import settings as S
    from controller.variants import allocate
    d, diag = allocate(_post([2000, 2000, 2000], [1100, 1100, 1350]), ["W1", "W2", "W3"], {"W1": 0.8, "W2": 0.1, "W3": 0.1})
    assert diag["winner"] == "W3" and abs(d["W3"] - S.WINNER_SHARE) < 1e-9 and len(d) == 2


def test_allocation_is_deterministic():
    from controller.variants import allocate
    args = (_post([900, 400, 380], [500, 230, 200]), ["W1", "W2", "W3"], {"W1": 0.8, "W2": 0.1, "W3": 0.1})
    assert allocate(*args) == allocate(*args)


def test_aa_false_winner_rate_is_low():
    import numpy as np
    import importlib.util
    spec = importlib.util.spec_from_file_location("aa", "scripts/aa_test.py")
    aa = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(aa)
    winners, _, _ = aa.run(np.full(3, 0.55), sims=60, daily=1100, days=8, seed=11)
    assert np.mean([w is not None for w in winners]) <= 0.05


def _decay_ctl(tmp_path, rates_before, rates_after, now_day=6):
    """Synthetic workflow: welcome W1/W2/W3, W3 leads at 80%; engagement per arm before/after day now-2."""
    import types
    from collections import defaultdict
    import numpy as np
    from controller.aggregates import WfAgg
    from controller.store import Store
    opts = {"welcome_message": ["W1", "W2", "W3"], "voice": ["V1"], "caller_id": ["D1"], "prompt_version": ["P1"]}
    w = WfAgg("wf", opts)
    rng = np.random.default_rng(5)
    for d in range(now_day - 5, now_day + 1):
        E = np.zeros((252, 3, 1, 1, 3), dtype=np.int32)
        rates = rates_before if d < now_day - 1 else rates_after
        for i, (share, r) in enumerate(zip((0.1, 0.1, 0.8), rates)):
            n = int(1000 * share)
            E[0, i, 0, 0, 0], E[0, i, 0, 0, 1] = n, rng.binomial(n, r)
        w.eng[d] = E
    cfg = {"cohorts": [], "prompt_version": "P1", "schedule": {},
           "allocations": {"default": {"welcome_message": {"W1": 0.1, "W2": 0.1, "W3": 0.8},
                                       "voice": {"V1": 1}, "caller_id": {"D1": 1}}}}
    return types.SimpleNamespace(now=now_day * 1440 + 12 * 60, ts="t", store=Store(tmp_path / "db.sqlite"),
                                 agg=types.SimpleNamespace(wf={"wf": w}), current={"wf": {"config": cfg}},
                                 wfs={"wf": {}}, drafts=defaultdict(list))


def test_decay_fires_when_a_winner_stops_winning(tmp_path):
    from controller.variants import Variants
    ctl = _decay_ctl(tmp_path, rates_before=(0.50, 0.50, 0.60), rates_after=(0.55, 0.55, 0.45))
    Variants()._decay(ctl, "wf")
    [d] = ctl.drafts["wf"]
    assert d.kind == "variant_rollback" and d.urgent and d.risk_reducing
    assert d.patch["allocations"]["default"]["welcome_message"].get("W3", 0) <= 0.15


def test_decay_does_not_fire_for_an_arm_that_never_led(tmp_path):
    from controller.variants import Variants
    ctl = _decay_ctl(tmp_path, rates_before=(0.55, 0.55, 0.55), rates_after=(0.55, 0.55, 0.50))
    Variants()._decay(ctl, "wf")
    assert not ctl.drafts["wf"]   # never significantly ahead: the daily plan handles it, not an urgent rollback


# ---------------------------------------------------------------- views
def test_supervisor_and_health_views_render_from_store_only(ctl):
    from controller.modules import build_modules
    from controller.view import health, supervisor_view, to_html
    ctl.modules = build_modules(ctl)
    advance_to(ctl, 13 * 60)
    ctl.maybe_snapshot(force=True)
    md = supervisor_view(ctl.state_dir)
    for section in ("How it's doing", "What we need from you", "What's being tested", "Changed without asking"):
        assert section in md
    assert "pickup" in md and "engagement" in md          # both KPIs, separately
    h = health(ctl.state_dir)
    assert "decision lag" in h and "Alerts" in h
    assert to_html(md, "x").startswith("<!doctype html>")


def test_unfixed_incident_escalates_to_a_page_and_clears_when_resolved(tmp_path):
    import types
    from controller.health import INCIDENT_PAGE_AFTER_MIN, LoopHealth
    from controller.store import Store
    st = Store(tmp_path / "db.sqlite")
    agg = types.SimpleNamespace(wf={"wf": types.SimpleNamespace(last_call_minute=None)})
    ctl = types.SimpleNamespace(store=st, agg=agg, credits=150, wfs={"wf": {"dialling_window": [9, 21]}}, now=600)
    with st.tx():
        st.save_wf_state("wf", {"incident": {"status": "open", "prompt": "P2", "opened": 600}})
    LoopHealth().tick(ctl)
    assert not st.db.execute("SELECT * FROM alerts WHERE severity='page'").fetchall()   # we're on it: no page yet
    ctl.now = 600 + INCIDENT_PAGE_AFTER_MIN
    LoopHealth().tick(ctl)
    [(msg,)] = st.db.execute("SELECT message FROM alerts WHERE code='hallucination_unfixed' AND resolved_minute IS NULL")
    assert "why --wf wf" in msg                                    # a page says what to look at
    with st.tx():
        st.save_wf_state("wf", {"incident": {"status": "resolved", "prompt": "P2", "opened": 600}})
    ctl.now += 30
    LoopHealth().tick(ctl)
    assert not st.db.execute("SELECT * FROM alerts WHERE resolved_minute IS NULL").fetchall()

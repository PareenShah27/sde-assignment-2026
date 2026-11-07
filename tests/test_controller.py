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

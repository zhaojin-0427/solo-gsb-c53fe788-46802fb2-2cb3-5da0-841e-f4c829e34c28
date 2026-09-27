"""组合修订行为等价审计的端到端测试（TestClient）。

通过两个模块构造同一组合的两个修订：
* v1：audit_buf1@1 直通，固定延迟 1；
* v2：inv@1 + inv2@1 两级反相，组合逻辑仍为直通，但多一级延迟，因此 t=0..H
  的输出波形不等价；
* v3：buf_chain 为同构直通网络，和 v1 固定延迟均为 1，输出波形等价。
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["HAZARD_DB"] = _tmp.name
if os.path.exists(_tmp.name):
    os.remove(_tmp.name)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)

BUF = {
    "name": "audit_buf1", "version": "1", "inputs": ["a"], "outputs": ["y"],
    "gates": [{"id": "y", "type": "buf", "inputs": ["a"], "delay": [1, 1]}],
}
OR2 = {
    "name": "audit_or2", "version": "1", "inputs": ["a", "b"], "outputs": ["y"],
    "gates": [{"id": "y", "type": "or", "inputs": ["a", "b"],
               "delay": [1, 1]}],
}
INV = {
    "name": "audit_inv", "version": "1", "inputs": ["a"], "outputs": ["y"],
    "gates": [{"id": "y", "type": "not", "inputs": ["a"], "delay": [1, 1]}],
}
INV2 = {
    "name": "audit_inv2", "version": "1", "inputs": ["a"], "outputs": ["y"],
    "gates": [{"id": "y", "type": "not", "inputs": ["a"], "delay": [1, 1]}],
}
BUF_CHAIN = {
    "name": "audit_buf_chain", "version": "1", "inputs": ["a"], "outputs": ["y"],
    "gates": [{"id": "x", "type": "buf", "inputs": ["a"], "delay": [1, 1]},
              {"id": "y", "type": "buf", "inputs": ["x"], "delay": [0, 0]}],
}

PARAMS = {
    "initial": {"a": 1},
    "final": {"a": 0},
    "windows": {"a": [2, 2]},
    "outputs": [{"name": "z", "max_transitions": 0}],
    "horizon": 8,
}


def _save_revision(doc: dict) -> int:
    r = client.put("/api/hierarchy/compositions/audit_behavior_demo", json=doc)
    assert r.status_code == 200, r.text
    return r.json()["composition"]["version"]


def test_revision_snapshots_inequivalence_equivalence_and_replay():
    for doc in (BUF, OR2, INV, INV2, BUF_CHAIN):
        r = client.post("/api/hierarchy/modules", json=doc)
        assert r.status_code == 200, r.text

    rev1 = _save_revision({"inputs": ["a"], "outputs": ["z"], "instances": [
        {"id": "b", "module": "audit_buf1", "version": "1",
         "connections": {"a": "a", "y": "z"}},
    ]})
    rev2 = _save_revision({"inputs": ["a"], "outputs": ["z"], "instances": [
        {"id": "i1", "module": "audit_inv", "version": "1",
         "connections": {"a": "a", "y": "w"}},
        {"id": "i2", "module": "audit_inv2", "version": "1",
         "connections": {"a": "w", "y": "z"}},
    ]})
    rev3 = _save_revision({"inputs": ["a"], "outputs": ["z"], "instances": [
        {"id": "c", "module": "audit_buf_chain", "version": "1",
         "connections": {"a": "a", "y": "z"}},
    ]})
    assert (rev1, rev2, rev3) == (1, 2, 3)

    revs = client.get(
        "/api/hierarchy/compositions/audit_behavior_demo/revisions").json()["revisions"]
    assert [r["revision"] for r in revs] == [3, 2, 1]
    assert all(r["flat_hash"] for r in revs)

    d = client.post("/api/hierarchy/compositions/audit_behavior_demo/audits", json={
        "left_revision": rev2, "right_revision": rev1, "params": PARAMS,
    }).json()
    assert d["ok"] is True
    result = d["result"]
    assert result["status"] == "inequivalent"
    assert result["earliest_divergence"]["time"] == 3
    assert result["earliest_divergence"]["trigger"][
        "input_transition_times"]["a"] == 2
    full_waves = result["earliest_divergence"]["full_waveforms"]
    assert {w["node"] for w in full_waves["revision_1"]} == {"a", "b/y"}
    assert result["revisions"] == {"left": 1, "right": 2}
    assert d["record"]["reused"] is False

    # 同一修订对（顺序相反）与同一参数哈希必须去重。
    again = client.post("/api/hierarchy/compositions/audit_behavior_demo/audits", json={
        "left_revision": rev1, "right_revision": rev2, "params": PARAMS,
    }).json()
    assert again["record"]["id"] == d["record"]["id"]
    assert again["record"]["reused"] is True

    # 等价修订：固定总延迟一致，所有可行轨迹的输出波形集合相同。
    eq = client.post("/api/hierarchy/compositions/audit_behavior_demo/audits", json={
        "left_revision": rev1, "right_revision": rev3, "params": PARAMS,
    }).json()
    assert eq["result"]["status"] == "equivalent"
    assert eq["result"]["coverage"]["covered_time"] == 8
    assert eq["result"]["coverage"]["settling_bound"] == 3
    assert eq["result"]["statistics"]["left_trajectories"] == 1

    rid = d["record"]["id"]
    one = client.get(f"/api/hierarchy/audits/{rid}").json()["record"]
    assert one["params_json"]["outputs"] == [
        {"name": "z", "max_transitions": 0}]
    assert one["left_flat_json"]["inputs"] == ["a"]

    replay = client.post(f"/api/hierarchy/audits/{rid}/replay").json()
    assert replay["result"]["status"] == "inequivalent"
    assert replay["result"]["earliest_divergence"]["time"] == 3

    exported = client.get("/api/hierarchy/audits/export").json()
    assert exported["format"] == "behavior-audit-records/v1"
    assert any(r["id"] == rid for r in exported["records"])


def test_same_revision_and_missing_revision_are_rejected():
    r = client.post("/api/hierarchy/compositions/audit_behavior_demo/audits", json={
        "left_revision": 1, "right_revision": 1, "params": PARAMS,
    })
    assert r.status_code == 400 and "不同修订" in r.json()["error"]

    r = client.post("/api/hierarchy/compositions/audit_behavior_demo/audits", json={
        "left_revision": 1, "right_revision": 99, "params": PARAMS,
    })
    assert r.status_code == 404


def test_audit_scale_limit_returns_unknown_record():
    params = dict(PARAMS, horizon=121)
    r = client.post("/api/hierarchy/compositions/audit_behavior_demo/audits", json={
        "left_revision": 1, "right_revision": 2, "params": params,
    })
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result["status"] == "unknown"
    assert result["reason"] == "scale_limit"
    assert "121" in result["message"]

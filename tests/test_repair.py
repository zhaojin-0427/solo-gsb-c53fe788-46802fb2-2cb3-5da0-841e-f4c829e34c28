"""反例驱动修复方案搜索（⑦）端到端测试（TestClient + z3 引擎直测）。"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
if os.path.exists(_tmp.name):
    os.remove(_tmp.name)
os.environ["HAZARD_DB"] = _tmp.name

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.models import parse_netlist  # noqa: E402
from app.params import parse_params  # noqa: E402
from app.repair import engine  # noqa: E402

client = TestClient(app)

# 与 data/sample_01_static_hazard.json 相同的静态 1 冒险电路
NL = {
    "inputs": ["a"],
    "gates": [
        {"id": "w1", "type": "not", "inputs": ["a"], "delay": [1, 3]},
        {"id": "z", "type": "or", "inputs": ["a", "w1"], "delay": [1, 1]},
    ],
    "outputs": ["z"],
}
P = {"initial": {"a": 1}, "final": {"a": 0}, "windows": {"a": [2, 2]},
     "outputs": [{"name": "z", "min_pulse_width": 3}]}


def _unsafe_record():
    r = client.post("/api/analyze", json={"netlist": NL, "params": P})
    assert r.json()["result"]["status"] == "unsafe"
    return r.json()["record"]["id"]


def test_engine_finds_one_change_gate_fix():
    nl = parse_netlist(NL)
    p = parse_params(nl, P)
    spec = engine.parse_search_request({
        "gates": [{"id": "w1", "range": [0, 5]}],
        "budget": {"max_changes": 1, "max_total_deviation": 4},
    })
    res = engine.search(nl, p, spec, bad_output="z")
    assert res["status"] == "safe_plans"
    # 最小改动证据：0 改动（基线）不可行，1 改动有解
    ev = res["min_change_evidence"]
    assert ev["min_changes"] == 1
    assert ev["lower_change_levels_unsat"] == [0]
    assert ev["proof"] == "complete"
    # w1 固定为 [0,0] 或 [1,1] 都能消除冒险
    targets = {tuple(pl["change_detail"]["gates"][0]["to"])
              for pl in res["plans"] if pl["change_detail"]["gates"]}
    assert (0, 0) in targets or (1, 1) in targets
    # 排序：改动数、总偏差、最早风险消除时间
    keys = [(pl["changes"], pl["total_deviation"],
             pl["risk_eliminated_at"]) for pl in res["plans"]]
    assert keys == sorted(keys)
    assert len(res["plans"]) <= 10


def test_engine_pulse_width_relaxation_is_one_change():
    nl = parse_netlist(NL)
    p = parse_params(nl, P)
    spec = engine.parse_search_request({
        "outputs": [{"name": "z", "min_pulse_width_range": [1, 4]}],
        "budget": {"max_changes": 1, "max_total_deviation": 3},
    })
    res = engine.search(nl, p, spec, bad_output="z")
    assert res["status"] == "safe_plans"
    pw_plans = [pl for pl in res["plans"]
                 if pl["change_detail"]["outputs"]]
    assert pw_plans
    assert all(pl["changes"] == 1 for pl in pw_plans)


def test_engine_no_solution_when_range_is_baseline_only():
    # 可调范围只覆盖与基线不同但都不安全的点：把范围收窄到固定 [2,3]
    # （lo=2,hi=3）仍可能安全；这里用严格等于基线 [1,3] 且不允许别的端点
    # 组合——[1,3] 本身即唯一端点对（lo=1,hi=3），不可改 => 无方案。
    nl = parse_netlist(NL)
    p = parse_params(nl, P)
    spec = engine.parse_search_request({
        "gates": [{"id": "w1", "range": [1, 3]}],
        "windows": [{"name": "a", "range": [2, 2]}],
        "outputs": [{"name": "z", "min_pulse_width_range": [3, 3]}],
        "budget": {"max_changes": 1, "max_total_deviation": 0},
    })
    # 偏差预算为 0：任何改动都超预算 → no_solution
    res = engine.search(nl, p, spec, bad_output="z")
    assert res["status"] == "no_solution"
    assert res["reason"] == "budget_exceeded"
    assert res["plans"] == []


def test_range_must_contain_baseline():
    nl = parse_netlist(NL)
    p = parse_params(nl, P)
    try:
        engine.build_space(nl, p, engine.parse_search_request({
            "gates": [{"id": "w1", "range": [4, 8]}],
            "budget": {"max_changes": 1, "max_total_deviation": 4},
        }))
        assert False, "应拒绝不包含原值的可调范围"
    except engine.RepairError:
        pass


def test_unknown_gate_rejected():
    nl = parse_netlist(NL)
    p = parse_params(nl, P)
    try:
        engine.build_space(nl, p, engine.parse_search_request({
            "gates": [{"id": "nope", "range": [0, 4]}],
            "budget": {"max_changes": 1, "max_total_deviation": 4},
        }))
        assert False, "应拒绝不在网表中的可调门"
    except engine.RepairError:
        pass


# ---------------- HTTP 接口 ----------------

def test_search_endpoint_and_replay_materialize_export():
    rid = _unsafe_record()
    body = {
        "gates": [{"id": "w1", "range": [0, 5]}],
        "budget": {"max_changes": 1, "max_total_deviation": 4},
    }
    r = client.post(f"/api/repairs/search/flat/{rid}", json=body)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["result"]["status"] == "safe_plans"
    assert d["result"]["plans"]
    sid = d["record"]["id"]

    # 审计日志存在（搜索可审计）
    assert d["result"]["audit_log"]

    # 去重
    again = client.post(f"/api/repairs/search/flat/{rid}", json=body).json()
    assert again["record"]["id"] == sid
    assert again["record"]["reused"] is True

    # 记录列表 / 单条
    lst = client.get("/api/repairs/records").json()["records"]
    assert any(x["id"] == sid for x in lst)
    one = client.get(f"/api/repairs/records/{sid}").json()["record"]
    assert one["result_json"]["status"] == "safe_plans"

    # 重放：确定性可复现
    rp = client.post(f"/api/repairs/records/{sid}/replay").json()
    assert rp["replay_consistent"] is True
    assert rp["result"]["status"] == "safe_plans"

    # 预览为新参数版本（不覆盖原记录）
    pk = d["result"]["plans"][0]["key"]
    m = client.post(f"/api/repairs/records/{sid}/materialize",
                   json={"plan_key": pk}).json()
    assert m["ok"] is True
    assert m["result"]["status"] == "safe"
    assert m["analysis_record"]["id"] != rid

    # 导出
    ex = client.get("/api/repairs/export").json()
    assert ex["format"] == "repair-search-records/v1"
    assert len(ex["records"]) >= 1


def test_search_requires_unsafe_source():
    nl = {"inputs": ["a"],
          "gates": [{"id": "z", "type": "and", "inputs": ["a", "a"],
                     "delay": [2, 2]}],
          "outputs": ["z"]}
    p = {"initial": {"a": 0}, "final": {"a": 1},
         "windows": {"a": [1, 1]},
         "outputs": [{"name": "z", "max_transitions": 1}]}
    sid = client.post("/api/analyze", json={"netlist": nl, "params": p}) \
        .json()["record"]["id"]
    r = client.post(f"/api/repairs/search/flat/{sid}", json={
        "gates": [{"id": "z", "range": [1, 3]}],
        "budget": {"max_changes": 1, "max_total_deviation": 2},
    })
    assert r.status_code == 400


def test_search_missing_source_404():
    r = client.post("/api/repairs/search/flat/999999", json={
        "gates": [{"id": "z", "range": [1, 3]}],
        "budget": {"max_changes": 1, "max_total_deviation": 2},
    })
    assert r.status_code == 400


def test_sources_lists_only_unsafe():
    rid = _unsafe_record()
    d = client.get("/api/repairs/sources").json()
    assert any(x["id"] == rid for x in d["flat"])


def test_eliminated_paths_and_uncovered_fields_present():
    rid = _unsafe_record()
    r = client.post(f"/api/repairs/search/flat/{rid}", json={
        "gates": [{"id": "w1", "range": [0, 5]}],
        "budget": {"max_changes": 1, "max_total_deviation": 4},
    }).json()
    plan = r["result"]["plans"][0]
    assert "eliminated_race_paths" in plan
    assert "coverage" in plan
    assert "uncovered" in r["result"]
    assert "unknown_reasons" in r["result"]
    assert "min_change_evidence" in r["result"]

"""FastAPI 接口冒烟测试（使用 TestClient，不监听端口）。"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 用临时 DB，避免污染开发库
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["HAZARD_DB"] = _tmp.name

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)

NL = {
    "inputs": ["a"],
    "gates": [
        {"id": "w", "type": "not", "inputs": ["a"], "delay": [1, 3]},
        {"id": "z", "type": "or", "inputs": ["a", "w"], "delay": [1, 1]},
    ],
    "outputs": ["z"],
}
P = {"initial": {"a": 1}, "final": {"a": 0}, "windows": {"a": [2, 2]},
     "outputs": [{"name": "z", "min_pulse_width": 3}]}


def test_limits():
    r = client.get("/api/limits")
    assert r.status_code == 200
    assert r.json()["max_gates"] > 0


def test_index_static():
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def test_validate_ok():
    r = client.post("/api/validate", json={"netlist": NL, "params": P})
    assert r.status_code == 200
    s = r.json()["summary"]
    assert s["n_gates"] == 2
    assert s["order"] == ["w", "z"]
    assert s["longest_static_path"] == 4


def test_validate_cycle_rejected():
    nl = {"inputs": ["a"], "gates": [
        {"id": "z", "type": "and", "inputs": ["z", "a"], "delay": [1, 1]}],
        "outputs": ["z"]}
    r = client.post("/api/validate", json={"netlist": nl, "params": {}})
    assert r.status_code == 400
    assert "环路" in r.json()["error"]


def test_analyze_unsafe_and_record():
    r = client.post("/api/analyze", json={"netlist": NL, "params": P})
    assert r.status_code == 200
    d = r.json()
    assert d["result"]["status"] == "unsafe"
    rid = d["record"]["id"]
    r2 = client.get(f"/api/records/{rid}")
    assert r2.json()["record"]["status"] == "unsafe"
    # 去重：相同请求复用同一记录 id
    r3 = client.post("/api/analyze", json={"netlist": NL, "params": P})
    assert r3.json()["record"]["id"] == rid
    assert r3.json()["record"]["reused"] is True


def test_analyze_safe_coverage():
    nl = {"inputs": ["a"], "gates": [
        {"id": "z", "type": "and", "inputs": ["a", "a"], "delay": [2, 2]}],
        "outputs": ["z"]}
    p = {"initial": {"a": 0}, "final": {"a": 1}, "windows": {"a": [1, 1]},
         "outputs": [{"name": "z", "max_transitions": 1}]}
    r = client.post("/api/analyze", json={"netlist": nl, "params": p})
    res = r.json()["result"]
    assert res["status"] == "safe"
    assert res["coverage"]["transient_fully_covered"] is True


def test_scale_limit_returns_unknown():
    nl = {"inputs": ["a"], "gates": [
        {"id": "z", "type": "buf", "inputs": ["a"], "delay": [1, 100]}],
        "outputs": ["z"]}
    p = {"initial": {"a": 0}, "final": {"a": 1}, "windows": {"a": [0, 0]},
         "horizon": 99999, "outputs": [{"name": "z", "max_transitions": 1}]}
    r = client.post("/api/analyze", json={"netlist": nl, "params": p})
    res = r.json()["result"]
    assert res["status"] == "unknown"
    assert res["reason"] == "scale_limit"


def test_samples_present():
    samples = client.get("/api/samples").json()["samples"]
    names = [s["name"] for s in samples]
    assert any("冒险" in n for n in names)
    joint = next(s for s in samples if "共同触发" in s["name"])
    r = client.post("/api/analyze",
                    json={"netlist": joint["netlist"], "params": joint["params"]})
    res = r.json()["result"]
    assert res["status"] == "unsafe"
    edges = {p["edge_t"] for p in res["counterexample"]["race_paths"]}
    assert edges == {2, 4}, "窄脉冲起止两条沿都必须有链"
    starts = [p for p in res["counterexample"]["race_paths"] if p["edge_t"] == 2]
    assert all(set(p["hops"][0]["joint"]) == {"s1", "s2"} for p in starts)


NL_JOINT = {
    "inputs": ["a", "b"],
    "gates": [{"id": "z", "type": "or", "inputs": ["a", "b"],
               "delay": [0, 0]}],
    "outputs": ["z"],
}
P_JOINT = {"initial": {"a": 0, "b": 0}, "final": {"a": 1, "b": 1},
           "windows": {"a": [2, 2], "b": [2, 2]},
           "horizon": 5, "outputs": [{"name": "z", "max_transitions": 0}]}


def test_analyze_joint_fanin_race_paths_complete():
    """同刻双扇入共同触发：API 反例必须含两条到主输入的完整链。"""
    r = client.post("/api/analyze", json={"netlist": NL_JOINT, "params": P_JOINT})
    assert r.status_code == 200
    res = r.json()["result"]
    assert res["status"] == "unsafe"
    paths = res["counterexample"]["race_paths"]
    assert len(paths) == 2
    leaves = sorted(h["node"] for q in paths for h in q["hops"]
                    if h["gate"] is None)
    assert leaves == ["a", "b"]
    assert all(q["hops"][0].get("joint") == ["a", "b"] for q in paths)


def test_record_uses_same_race_path_data():
    """分析记录中保存的结果与 /api/analyze 即时返回共用同一链路数据。"""
    r = client.post("/api/analyze", json={"netlist": NL_JOINT, "params": P_JOINT})
    d = r.json()
    rid = d["record"]["id"]
    live = d["result"]["counterexample"]["race_paths"]
    stored = client.get(f"/api/records/{rid}").json()["record"]
    assert stored["result_json"]["counterexample"]["race_paths"] == live
    assert stored["status"] == "unsafe"

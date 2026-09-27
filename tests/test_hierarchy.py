"""层次化模块库与组合验证：端到端测试（TestClient）。

覆盖：
* 模块版本登记、不可修改、端口缺失/重复/未知端口、多驱动、未声明信号；
* 实例递归（自引用/同包互引）与组合环拒绝保存；
* 确定性展开：实例路径、局部门延迟、跨实例 wire、同模块多次实例化、嵌套；
* 层次分析反例的 race_paths/波形节点带实例路径与端口映射；
* 模块版本固定后的分析记录重放，组合更新不改写旧记录；
* JSON 整包导入（幂等/冲突）与导出。
"""

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

INV = {"name": "inv", "version": "1", "inputs": ["a"], "outputs": ["y"],
       "gates": [{"id": "y", "type": "not", "inputs": ["a"],
                  "delay": [1, 3]}]}
OR2 = {"name": "or2", "version": "1", "inputs": ["a", "b"],
       "outputs": ["y"],
       "gates": [{"id": "y", "type": "or", "inputs": ["a", "b"],
                  "delay": [1, 1]}]}


def _post(path, body):
    return client.post(path, json=body)


def _register(doc):
    return _post("/api/hierarchy/modules", doc)


def test_01_module_crud_and_immutable():
    r = _register(INV)
    assert r.status_code == 200, r.text
    assert r.json()["module"]["version"] == "1"
    r = _register(INV)
    assert r.status_code == 409 and "不可修改" in r.json()["error"]
    # 新版本可登记
    inv2 = dict(INV, version="2")
    inv2["gates"][0]["delay"] = [0, 5]
    assert _register(inv2).status_code == 200
    assert _register(OR2).status_code == 200
    mods = client.get("/api/hierarchy/modules").json()["modules"]
    assert ("inv", "1") in {(m["name"], m["version"]) for m in mods}
    one = client.get("/api/hierarchy/modules/inv/1").json()["module"]
    assert one["gates"][0]["delay"] == [1, 3]
    assert client.get("/api/hierarchy/modules/inv/999").status_code == 404


def test_02_port_binding_errors():
    base = {"name": "c_bad", "inputs": ["a"], "outputs": ["z"], "instances": []}

    def put(inst):
        doc = dict(base, instances=[inst])
        return client.put("/api/hierarchy/compositions/c_bad", json=doc)

    r = put({"id": "i", "module": "inv", "version": "1",
             "connections": {"a": "a"}})
    assert r.status_code == 400 and "缺少端口映射" in r.json()["error"]

    r = put({"id": "i", "module": "inv", "version": "1",
             "connections": [["a", "a"], ["a", "b"], ["y", "z"]]})
    assert r.status_code == 400 and "重复映射" in r.json()["error"]

    r = put({"id": "i", "module": "inv", "version": "1",
             "connections": {"a": "a", "y": "z", "q": "x"}})
    assert r.status_code == 400 and "不存在的端口" in r.json()["error"]

    r = put({"id": "i", "module": "ghost", "version": "9",
             "connections": {"a": "a", "y": "z"}})
    assert r.status_code == 400 and "不存在" in r.json()["error"]


def test_03_driver_and_signal_errors():
    def save(name, doc):
        doc = dict(doc, name=name)
        return client.put(f"/api/hierarchy/compositions/{name}", json=doc)

    # 未声明的输入连接信号
    r = save("c_ghost", {"inputs": ["a"], "outputs": ["z"], "instances": [
        {"id": "i", "module": "inv", "version": "1",
         "connections": {"a": "ghost", "y": "z"}}]})
    assert r.status_code == 400 and "未声明" in r.json()["error"]

    # 多驱动：两个实例输出都驱动 z
    r = save("c_multi", {"inputs": ["a"], "outputs": ["z"], "instances": [
        {"id": "i0", "module": "inv", "version": "1",
         "connections": {"a": "a", "y": "z"}},
        {"id": "i1", "module": "inv", "version": "1",
         "connections": {"a": "a", "y": "z"}}]})
    assert r.status_code == 400 and "驱动冲突" in r.json()["error"]

    # 顶层组合不允许直接放门
    r = save("c_gate", {"inputs": ["a"], "outputs": ["z"],
                        "gates": [{"id": "z", "type": "buf",
                                   "inputs": ["a"]}]})
    assert r.status_code == 400 and "只能包含模块实例" in r.json()["error"]


def test_04_combinational_loop_rejected():
    doc = {"name": "c_loop", "inputs": ["a"], "outputs": ["z"], "instances": [
        {"id": "i0", "module": "inv", "version": "1",
         "connections": {"a": "w1", "y": "w0"}},
        {"id": "i1", "module": "inv", "version": "1",
         "connections": {"a": "w0", "y": "w1"}},
        {"id": "i2", "module": "inv", "version": "1",
         "connections": {"a": "a", "y": "z"}}]}
    r = client.put("/api/hierarchy/compositions/c_loop", json=doc)
    assert r.status_code == 400 and "组合环" in r.json()["error"]


def test_05_module_recursion_rejected():
    # 直接自引用
    r = _post("/api/hierarchy/modules", {
        "name": "selfloop", "version": "1", "inputs": ["a"], "outputs": ["y"],
        "instances": [{"id": "s", "module": "selfloop", "version": "1",
                       "connections": {"a": "a", "y": "y"}}]})
    assert r.status_code == 400 and "递归" in r.json()["error"]

    # 同包互引成环，整包拒绝
    bundle = {"modules": [
        {"name": "ra", "version": "1", "inputs": ["x"], "outputs": ["z"],
         "instances": [{"id": "i", "module": "rb", "version": "1",
                        "connections": {"x": "x", "z": "z"}}]},
        {"name": "rb", "version": "1", "inputs": ["x"], "outputs": ["z"],
         "instances": [{"id": "i", "module": "ra", "version": "1",
                        "connections": {"x": "x", "z": "z"}}]}]}
    r = _post("/api/hierarchy/import", bundle)
    assert r.status_code == 400 and "递归" in r.json()["error"]


def test_06_flatten_basic_paths_and_delays():
    doc = {"name": "haz", "inputs": ["a"], "outputs": ["z"], "instances": [
        {"id": "i0", "module": "inv", "version": "1",
         "connections": {"a": "a", "y": "w"}},
        {"id": "o0", "module": "or2", "version": "1",
         "connections": {"a": "a", "b": "w", "y": "z"}}]}
    r = client.put("/api/hierarchy/compositions/haz", json=doc)
    assert r.status_code == 200
    f = client.get("/api/hierarchy/compositions/haz/flatten").json()
    net = f["netlist"]
    gates = {g["id"]: g for g in net["gates"]}
    # 实例路径 + 局部门 id；延迟保留
    assert gates["i0/y"]["delay"] == [1, 3]
    assert gates["o0/y"]["delay"] == [1, 1]
    # 内部 wire w 是 i0 输出的别名，直接归并到驱动门 i0/y（无需额外导线门）
    assert f["alias_index"]["w"] == "i0/y"
    assert f["alias_index"]["i0/y"] == "i0/y"
    assert gates["o0/y"]["inputs"] == ["a", "i0/y"]
    # 顶层输出 z 与 o0/y（门驱动同名端口）合并
    assert net["outputs"] == ["o0/y"]
    assert f["output_mapping"] == {"z": "o0/y"}
    # 实例记录与版本
    paths = {x["path"] for x in f["instances"]}
    assert paths == {"i0", "o0"}
    assert f["module_versions"] == {"inv": "1", "or2": "1", "haz": ""} or \
        f["module_versions"].get("inv") == "1"
    # crossings 给出端口映射（w 归并到 i0/y，crossings 挂在代表节点 i0/y 上）
    w_info = f["nodes"]["i0/y"]
    ports = {(c["instance_id"], c["port"], c["direction"])
             for c in w_info["crossings"]}
    assert ("i0", "y", "output") in ports
    assert ("o0", "b", "input") in ports


def test_07_flatten_deterministic_and_multi_instance():
    # 同一 inv 实例化两次：门 id 按实例路径区分；两次展开字节序一致
    doc = {"name": "twoinv", "inputs": ["a", "b"], "outputs": ["z"],
           "wires": ["wa", "wb"], "instances": [
        {"id": "x1", "module": "inv", "version": "1",
         "connections": {"a": "a", "y": "wa"}},
        {"id": "x2", "module": "inv", "version": "2",
         "connections": {"a": "b", "y": "wb"}},
        {"id": "o0", "module": "or2", "version": "1",
         "connections": {"a": "wa", "b": "wb", "y": "z"}}]}
    client.put("/api/hierarchy/compositions/twoinv", json=doc)
    import json
    f1 = client.get("/api/hierarchy/compositions/twoinv/flatten").json()
    f2 = client.get("/api/hierarchy/compositions/twoinv/flatten").json()
    assert json.dumps(f1["netlist"], sort_keys=True) == \
        json.dumps(f2["netlist"], sort_keys=True)
    ids = {g["id"] for g in f1["netlist"]["gates"]}
    assert {"x1/y", "x2/y", "o0/y"} <= ids
    # 两个实例绑定不同版本
    vers = {x["path"]: x["version"] for x in f1["instances"]}
    assert vers["x1"] == "1" and vers["x2"] == "2"


def test_08_nested_flatten():
    and2 = {"name": "and2", "version": "1", "inputs": ["a", "b"],
            "outputs": ["y"],
            "gates": [{"id": "y", "type": "and", "inputs": ["a", "b"],
                       "delay": [0, 0]}]}
    inv2 = {"name": "inv2", "version": "1", "inputs": ["a", "en"],
            "outputs": ["y"], "wires": ["n"], "instances": [
                {"id": "u1", "module": "inv", "version": "1",
                 "connections": [["a", "a"], ["y", "n"]]},
                {"id": "g1", "module": "and2", "version": "1",
                 "connections": [["a", "n"], ["b", "en"], ["y", "y"]]}]}
    assert _register(and2).status_code == 200
    assert _register(inv2).status_code == 200
    doc = {"name": "nest", "inputs": ["a", "en"], "outputs": ["z"],
           "wires": ["q"], "instances": [
        {"id": "u", "module": "inv2", "version": "1",
         "connections": {"a": "a", "en": "en", "y": "q"}},
        {"id": "v", "module": "and2", "version": "1",
         "connections": {"a": "q", "b": "en", "y": "z"}}]}
    client.put("/api/hierarchy/compositions/nest", json=doc)
    f = client.get("/api/hierarchy/compositions/nest").json()
    flat = client.get("/api/hierarchy/compositions/nest/flatten").json()
    ids = {g["id"] for g in flat["netlist"]["gates"]}
    # 三层实例路径
    assert "u/u1/y" in ids
    assert {"u", "u/u1", "u/g1", "v"} <= {x["path"] for x in flat["instances"]}


def test_08b_gateless_wrapper_deep_alias():
    # 无门 wrapper：输出端口直接由内部实例输出驱动（作用域同名）
    wrap = {"name": "wrap", "version": "1", "inputs": ["a"], "outputs": ["y"],
            "instances": [{"id": "i", "module": "inv", "version": "1",
                           "connections": {"a": "a", "y": "y"}}]}
    assert _register(wrap).status_code == 200
    doc = {"name": "gateless", "inputs": ["a"], "outputs": ["z"],
           "wires": ["w"], "instances": [
        {"id": "u", "module": "wrap", "version": "1",
         "connections": {"a": "a", "y": "w"}},
        {"id": "o", "module": "or2", "version": "1",
         "connections": {"a": "w", "b": "a", "y": "z"}}]}
    client.put("/api/hierarchy/compositions/gateless", json=doc)
    f = client.get("/api/hierarchy/compositions/gateless/flatten").json()
    net = f["netlist"]
    gates = {g["id"]: g for g in net["gates"]}
    # 深层别名 u/i/y 穿透 wrapper 与内部 wire w，直达 OR 门输入
    assert gates["o/y"]["inputs"] == ["u/i/y", "a"]
    assert net["outputs"] == ["o/y"]
    assert f["output_mapping"] == {"z": "o/y"}


def test_08c_equivalent_to_flat_netlist():
    """层次展开+验证结果必须与等价手写扁平网表一致（同 bad_time/状态）。"""
    # 自包含：幂等确保 inv@1/or2@1 与组合 haz 存在
    if client.get("/api/hierarchy/modules/inv/1").status_code == 404:
        _register(INV)
        _register(OR2)
    comp = {"name": "haz", "inputs": ["a"], "outputs": ["z"], "instances": [
        {"id": "i0", "module": "inv", "version": "1",
         "connections": {"a": "a", "y": "w"}},
        {"id": "o0", "module": "or2", "version": "1",
         "connections": {"a": "a", "b": "w", "y": "z"}}]}
    client.put("/api/hierarchy/compositions/haz", json=comp)
    flat_nl = {
        "inputs": ["a"],
        "gates": [
            {"id": "w1", "type": "not", "inputs": ["a"], "delay": [1, 3]},
            {"id": "z", "type": "or", "inputs": ["a", "w1"], "delay": [1, 1]},
        ],
        "outputs": ["z"],
    }
    params = {"initial": {"a": 1}, "final": {"a": 0},
              "windows": {"a": [2, 2]},
              "outputs": [{"name": "z", "min_pulse_width": 3}]}
    flat_res = client.post("/api/analyze",
                           json={"netlist": flat_nl, "params": params}).json()
    hier_res = client.post(
        "/api/hierarchy/compositions/haz/analyze",
        json={"params": params}).json()
    assert flat_res["result"]["status"] == hier_res["result"]["status"]
    assert flat_res["result"]["bad_time"] == hier_res["result"]["bad_time"]
    # 波形取值序列一致（按拓扑等价节点对齐）
    def waves_map(res):
        return {w["node"]: [e["t"] for e in w["edges"]]
                for w in res["result"]["waves"]}
    fm, hm = waves_map(flat_res), waves_map(hier_res)
    assert fm["w1"] == hm["i0/y"]
    assert fm["z"] == hm["o0/y"]


def test_09_analyze_annotated_counterexample():
    params = {"initial": {"a": 1}, "final": {"a": 0},
              "windows": {"a": [2, 2]},
              "outputs": [{"name": "z", "min_pulse_width": 3}]}
    r = client.post("/api/hierarchy/compositions/haz/analyze",
                    json={"params": params})
    assert r.status_code == 200, r.text
    d = r.json()
    res = d["result"]
    assert res["status"] == "unsafe"
    assert res["bad_output_port"] == "z"
    assert res["bad_output"] == "o0/y"
    ce = res["counterexample"]
    # race_paths 的门 hop 带实例路径/模块版本，叶子带端口映射 route
    hop0 = ce["race_paths"][0]["hops"][0]
    assert hop0["node"] == "o0/y"
    assert hop0["instance_id"] == "o0"
    assert hop0["module"] == "or2" and hop0["module_version"] == "1"
    assert hop0["route"][0] == "o0/y" and "o0.y" in hop0["route"]
    leaf = ce["race_paths"][0]["hops"][-1]
    assert leaf["gate"] is None and leaf["node"] == "a"
    assert "o0.a" in leaf["route"]  # 主输入经实例输入端口的映射
    # 波形节点同样带实例路径与端口映射
    w = next(x for x in res["waves"] if x["node"] == "i0/y")
    assert w["origin"]["module"] == "inv"
    assert any(c["port"] == "y" and c["direction"] == "output"
               for c in w["crossings"])
    # chosen_delays 有路径明细
    assert "o0/y" in ce["chosen_delays_paths"]
    # 去重：同参数再分析复用同一记录
    r2 = client.post("/api/hierarchy/compositions/haz/analyze",
                     json={"params": params})
    assert r2.json()["record"]["id"] == d["record"]["id"]
    assert r2.json()["record"]["reused"] is True


def test_10_record_persistence_and_replay():
    rid = client.get("/api/hierarchy/analyses").json()["records"][0]["id"]
    rec = client.get(f"/api/hierarchy/analyses/{rid}").json()["record"]
    assert rec["versions_json"].get("or2") == "1"
    assert rec["flat_json"]["gates"], "快照保存展开网表"
    rp = client.post(f"/api/hierarchy/analyses/{rid}/replay").json()
    assert rp["ok"] and rp["result"]["status"] == "unsafe"
    assert rp["module_versions"]["inv"] == "1"


def test_11_composition_revision_keeps_old_analysis():
    # 自包含：确保 haz@rev1 存在并产生一条分析记录
    if client.get("/api/hierarchy/modules/inv/1").status_code == 404:
        _register(INV)
    if client.get("/api/hierarchy/modules/inv/2").status_code == 404:
        inv2 = dict(INV, version="2")
        inv2["gates"][0]["delay"] = [0, 5]
        _register(inv2)
    if client.get("/api/hierarchy/modules/or2/1").status_code == 404:
        _register(OR2)
    doc1 = {"name": "haz", "inputs": ["a"], "outputs": ["z"], "instances": [
        {"id": "i0", "module": "inv", "version": "1",
         "connections": {"a": "a", "y": "w"}},
        {"id": "o0", "module": "or2", "version": "1",
         "connections": {"a": "a", "b": "w", "y": "z"}}]}
    v1 = client.put("/api/hierarchy/compositions/haz", json=doc1).json()
    rid_old = client.post(
        "/api/hierarchy/compositions/haz/analyze",
        json={"params": {"initial": {"a": 1}, "final": {"a": 0},
                         "windows": {"a": [2, 2]},
                         "outputs": [{"name": "z",
                                      "min_pulse_width": 3}]}}).json()["record"]["id"]
    # 保存 rev2（结构变化：输出直接取 inv，去掉 or2），旧记录仍可重放
    doc2 = {"name": "haz", "inputs": ["a"], "outputs": ["z"], "instances": [
        {"id": "i0", "module": "inv", "version": "2",
         "connections": {"a": "a", "y": "z"}}]}
    r = client.put("/api/hierarchy/compositions/haz", json=doc2).json()
    assert r["composition"]["version"] == v1["composition"]["version"] + 1
    # 内容不变再次保存：修订号不增长
    r_same = client.put("/api/hierarchy/compositions/haz",
                        json=doc2).json()
    assert r_same["composition"]["version"] == r["composition"]["version"]
    assert r_same["composition"]["unchanged"] is True
    rp = client.post(f"/api/hierarchy/analyses/{rid_old}/replay").json()
    assert rp["result"]["status"] == "unsafe"  # 旧版本快照重放不受影响


def test_12_bundle_import_export_roundtrip():
    # 导出整库包含已登记模块
    bundle = client.get("/api/hierarchy/export").json()
    names = {(m["name"], m["version"]) for m in bundle["modules"]}
    assert ("inv", "1") in names and ("or2", "1") in names
    # 幂等再导入：全部跳过
    r = _post("/api/hierarchy/import",
              {"modules": [m for m in bundle["modules"]
                           if (m["name"], m["version"])
                           in {("inv", "1"), ("or2", "1")}]})
    assert r.status_code == 200
    assert set(r.json()["skipped_existing"]) == {"inv@1", "or2@1"}
    # 内容冲突：整包 409
    bad = dict(INV)
    bad["gates"] = [{"id": "y", "type": "buf", "inputs": ["a"],
                     "delay": [1, 1]}]
    r = _post("/api/hierarchy/import", {"modules": [bad]})
    assert r.status_code == 409


def test_13_dryrun_flatten_without_save():
    doc = {"name": "dry_one", "inputs": ["a"], "outputs": ["z"],
           "instances": [{"id": "i0", "module": "inv", "version": "1",
                          "connections": {"a": "a", "y": "z"}}]}
    r = client.post("/api/hierarchy/compositions/validate-flatten", json=doc)
    assert r.status_code == 200
    assert r.json()["flatten"]["netlist"]["gates"][0]["id"] == "i0/y"
    assert client.get("/api/hierarchy/compositions/dry_one").status_code == 404


def test_14_validate_module_endpoint():
    doc = {"name": "precheck", "version": "1", "inputs": ["a"],
           "outputs": ["y"],
           "gates": [{"id": "y", "type": "buf", "inputs": ["a"]}]}
    r = client.post("/api/hierarchy/modules/validate", json=doc)
    assert r.status_code == 200 and r.json()["summary"]["order"] == ["y"]
    bad = dict(doc, outputs=["nope"])
    r = client.post("/api/hierarchy/modules/validate", json=bad)
    assert r.status_code == 400 and "没有驱动源" in r.json()["error"]


def test_15_samples_available():
    samples = client.get("/api/hierarchy/samples").json()["samples"]
    assert len(samples) >= 2
    s = next(x for x in samples if x["name"].startswith("层次化静态1冒险"))
    # 示例包可干净导入新库语义（这里库已有同名版本 → 跳过）
    r = _post("/api/hierarchy/import",
              {"modules": s["modules"], "compositions": s["compositions"]})
    assert r.status_code == 200
    cname = s["analyze"]["composition"]
    f = client.get(f"/api/hierarchy/compositions/{cname}/flatten").json()
    assert f["netlist"]["gates"]
    ra = client.post(f"/api/hierarchy/compositions/{cname}/analyze",
                     json={"params": s["analyze"]["params"]})
    assert ra.json()["result"]["status"] == "unsafe"

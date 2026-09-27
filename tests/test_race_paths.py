"""竞争因果链回归：同刻多扇入共同触发、重汇聚窄脉冲、零延迟门。

背景：旧版回溯只认“单独撤回该扇入即可恢复门旧值”的敏化扇入。当同一离散
时刻两个（或更多）扇入共同触发 OR/AND/NAND/NOR 的输出沿时，单独撤回任一
扇入都不能恢复旧值，于是旧回溯在该门处断链——race_paths 只剩门节点本身，
窄脉冲的起止两条沿也无法分别递归到主输入。

本文件固定覆盖：
  * OR 同刻双扇入共同 0->1；
  * AND 同刻双扇入共同 1->0（含全零延迟门）；
  * 失配窗口内错时变化、点火时两扇入都为“共同必需”的情形；
  * 单输入重汇聚产生的窄脉冲：起始沿两条同刻链共同触发、结束沿单链触发，
    两条沿都必须给出完整且稳定去重的路径；
  * 零延迟门同格级联的共同触发；
  * 针对重汇聚/多扇入/零延迟结构的 z3 ↔ 枚举 DFS 模拟器交叉测试。
"""

import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models import parse_netlist  # noqa: E402
from app.params import parse_params  # noqa: E402
from app.verifier.sim import enumerate_verify  # noqa: E402
from app.verifier.z3verifier import verify  # noqa: E402

os.environ.setdefault("HAZARD_TIMEOUT_MS", "10000")

INPUTS = {"a", "b", "c"}


def _run(nl_obj, p_obj):
    nl = parse_netlist(nl_obj)
    params = parse_params(nl, p_obj)
    return nl, params, verify(nl, params)


def _assert_chains_complete(r, bad_node, edge_ts):
    """反例因果链不变量：非空、每条链落到主输入、覆盖全部相关输出沿、
    不存在只含门节点的断链，且路径去重。"""
    assert r["status"] == "unsafe"
    ce = r["counterexample"]
    paths = ce["race_paths"]
    assert paths, "race_paths 不应为空"
    assert ce["race_path_count"] == len(paths) or ce["race_paths_truncated"]
    covered = {p["edge_t"] for p in paths}
    for t in edge_ts:
        assert t in covered, f"缺少输出沿 t={t} 的因果链"
    sigs = set()
    for p in paths:
        hops = p["hops"]
        assert len(hops) >= 2 or hops[0]["node"] in INPUTS, "链路停在门节点（断链）"
        assert hops[0]["node"] == bad_node, "链首必须是违规输出门"
        leaf = hops[-1]
        assert leaf["gate"] is None and leaf["node"] in INPUTS, \
            "链尾必须是主输入"
        sig = (p["edge_t"], tuple((h["node"], h["t"],
               tuple(h.get("joint", []))) for h in hops))
        assert sig not in sigs, "存在重复因果链"
        sigs.add(sig)


# ---------------------------------------------------------------------------
# 固定电路
# ---------------------------------------------------------------------------

NL_OR_JOINT = {
    "inputs": ["a", "b"],
    "gates": [{"id": "z", "type": "or", "inputs": ["a", "b"], "delay": [1, 1]}],
    "outputs": ["z"],
}

NL_AND_JOINT_ZERO = {
    "inputs": ["a", "b"],
    "gates": [{"id": "z", "type": "and", "inputs": ["a", "b"], "delay": [0, 0]}],
    "outputs": ["z"],
}

# a 0->1：两条 AND(a, NOT(a)) 分支各产生正脉冲 [2,4) / [2,3)，OR 汇聚后
# z 在 t=2 由 s1、s2 同刻共同 0->1（撤回任一条 z 仍为 1），t=4 由 s1 单独
# 拉回；形成宽度 2 的窄脉冲。
NL_RECONV_JOINT_PULSE = {
    "inputs": ["a"],
    "gates": [
        {"id": "n1", "type": "not", "inputs": ["a"], "delay": [2, 2]},
        {"id": "n2", "type": "not", "inputs": ["a"], "delay": [1, 1]},
        {"id": "s1", "type": "and", "inputs": ["a", "n1"], "delay": [0, 0]},
        {"id": "s2", "type": "and", "inputs": ["a", "n2"], "delay": [0, 0]},
        {"id": "z", "type": "or", "inputs": ["s1", "s2"], "delay": [0, 0]}],
    "outputs": ["z"],
}

# 零延迟同格级联：x=AND(a,b) 同刻共同 1->0，z=NOT(x) 同格沿。
NL_ZERO_CASCADE = {
    "inputs": ["a", "b"],
    "gates": [
        {"id": "x", "type": "and", "inputs": ["a", "b"], "delay": [0, 0]},
        {"id": "z", "type": "not", "inputs": ["x"], "delay": [0, 0]}],
    "outputs": ["z"],
}


def test_or_same_time_joint_fanins_both_kept():
    p = {"initial": {"a": 0, "b": 0}, "final": {"a": 1, "b": 1},
         "windows": {"a": [1, 1], "b": [1, 1]},
         "horizon": 5, "outputs": [{"name": "z", "max_transitions": 0}]}
    nl, params, r = _run(NL_OR_JOINT, p)
    st, bt, _, _ = enumerate_verify(nl, params)
    assert (st, bt) == ("bad", 2)
    assert r["bad_time"] == 2
    _assert_chains_complete(r, "z", [2])
    leaves = sorted(h["node"] for path in r["counterexample"]["race_paths"]
                    for h in path["hops"] if h["gate"] is None)
    assert leaves == ["a", "b"], "两个同刻扇入都必须保留并分别递归到主输入"
    head = r["counterexample"]["race_paths"][0]["hops"][0]
    assert set(head["joint"]) == {"a", "b"}


def test_and_zero_delay_joint_fall():
    p = {"initial": {"a": 1, "b": 1}, "final": {"a": 0, "b": 0},
         "windows": {"a": [2, 2], "b": [2, 2]},
         "horizon": 5, "outputs": [{"name": "z", "max_transitions": 0}]}
    nl, params, r = _run(NL_AND_JOINT_ZERO, p)
    assert r["bad_time"] == 2
    st, bt, _, _ = enumerate_verify(nl, params)
    assert (st, bt) == ("bad", 2)
    _assert_chains_complete(r, "z", [2])
    leaves = sorted(h["node"] for path in r["counterexample"]["race_paths"]
                    for h in path["hops"] if h["gate"] is None)
    assert leaves == ["a", "b"]


def test_staggered_changes_within_mismatch_window_are_joint():
    nl_obj = {"inputs": ["a", "b"], "gates": [
        {"id": "z", "type": "or", "inputs": ["a", "b"], "delay": [2, 2]}],
        "outputs": ["z"]}
    p = {"initial": {"a": 0, "b": 0}, "final": {"a": 1, "b": 1},
         "windows": {"a": [1, 1], "b": [2, 2]},
         "horizon": 6, "outputs": [{"name": "z", "max_transitions": 0}]}
    nl, params, r = _run(nl_obj, p)
    assert r["bad_time"] == 3
    st, bt, _, _ = enumerate_verify(nl, params)
    assert (st, bt) == ("bad", 3)
    _assert_chains_complete(r, "z", [3])
    # 叶子必须落到各自实际的输入沿时刻（a@1、b@2），而不是门点火时刻
    by_leaf = {h["node"]: h["t"]
               for path in r["counterexample"]["race_paths"]
               for h in path["hops"] if h["gate"] is None}
    assert by_leaf == {"a": 1, "b": 2}


def test_reconvergence_narrow_pulse_both_edges_full_chains():
    p = {"initial": {"a": 0}, "final": {"a": 1}, "windows": {"a": [2, 2]},
         "horizon": 8, "outputs": [{"name": "z", "min_pulse_width": 3}]}
    nl, params, r = _run(NL_RECONV_JOINT_PULSE, p)
    st, bt, _, _ = enumerate_verify(nl, params)
    assert (st, bt) == ("bad", 4)
    assert r["bad_time"] == 4
    # 窄脉冲起止两条沿 t=2（共同起始）/ t=4（单独结束）都要有完整链
    _assert_chains_complete(r, "z", [2, 4])
    start = [q for q in r["counterexample"]["race_paths"] if q["edge_t"] == 2]
    end = [q for q in r["counterexample"]["race_paths"] if q["edge_t"] == 4]
    assert len(start) == 2 and len(end) == 1
    assert all(set(q["hops"][0].get("joint", [])) == {"s1", "s2"} for q in start)
    assert all(q["hops"][-1]["node"] == "a" for q in start + end)


def test_zero_delay_cascade_joint_marks():
    p = {"initial": {"a": 1, "b": 1}, "final": {"a": 0, "b": 0},
         "windows": {"a": [3, 3], "b": [3, 3]},
         "horizon": 6, "outputs": [{"name": "z", "max_transitions": 0}]}
    nl, params, r = _run(NL_ZERO_CASCADE, p)
    assert r["bad_time"] == 3
    st, bt, _, _ = enumerate_verify(nl, params)
    assert (st, bt) == ("bad", 3)
    _assert_chains_complete(r, "z", [3])
    leaves = sorted(h["node"] for path in r["counterexample"]["race_paths"]
                    for h in path["hops"] if h["gate"] is None)
    assert leaves == ["a", "b"]
    for q in r["counterexample"]["race_paths"]:
        x_hop = next(h for h in q["hops"] if h["node"] == "x")
        assert set(x_hop["joint"]) == {"a", "b"}


def test_paths_are_stable_across_runs():
    import json
    p = {"initial": {"a": 0}, "final": {"a": 1}, "windows": {"a": [2, 2]},
         "horizon": 8, "outputs": [{"name": "z", "min_pulse_width": 3}]}

    def paths():
        nl = parse_netlist(NL_RECONV_JOINT_PULSE)
        r = verify(nl, parse_params(nl, p))
        return json.dumps(r["counterexample"]["race_paths"],
                          sort_keys=True, ensure_ascii=False)

    assert paths() == paths()


# ---------------------------------------------------------------------------
# 面向重汇聚 / 多扇入 / 零延迟的随机结构：z3 ↔ 枚举模拟交叉测试，
# 且所有 unsafe 结论的 race_paths 必须完整到主输入。
# ---------------------------------------------------------------------------

def _reconv_dag(rng):
    """强制包含重汇聚与 [0,*] 门的小 DAG。"""
    nodes = ["a", "b"]
    gates = []
    bin_types = ["and", "or", "nand", "nor"]
    for k in range(5):
        t = rng.choice(bin_types + ["not", "buf"])
        arity = 1 if t in ("not", "buf") else 2
        pool = nodes[: 2 + k]
        ins = rng.sample(pool, arity)
        lo = rng.choice([0, 0, 1, 2])
        hi = lo + rng.choice([0, 0, 1, 2])
        gates.append({"id": f"g{k}", "type": t, "inputs": ins,
                      "delay": [lo, hi]})
        nodes.append(f"g{k}")
    return {"inputs": ["a", "b"], "gates": gates,
            "outputs": [gates[-1]["id"]]}


def _reconv_params(rng, nl):
    init = {n: rng.randrange(2) for n in nl["inputs"]}
    final, windows = dict(init), {}
    for n in nl["inputs"]:
        if rng.random() < 0.8:
            final[n] = 1 - init[n]
            t0 = rng.randrange(0, 3)
            windows[n] = [t0, t0 + rng.randrange(0, 2)]
    if init == final:  # 至少一个跃迁
        final["a"] = 1 - init["a"]
        windows["a"] = [1, 2]
    out = ({"name": nl["outputs"][0],
            "max_transitions": rng.choice([0, 1, 2])}
           if rng.random() < 0.5 else
           {"name": nl["outputs"][0],
            "min_pulse_width": rng.choice([1, 2, 3])})
    return {"initial": init, "final": final, "windows": windows,
            "outputs": [out], "horizon": rng.choice([6, 8])}


def test_reconvergence_zero_delay_crossvalidate():
    rng = random.Random(7)
    n_unsafe = 0
    for k in range(80):
        nl_obj = _reconv_dag(rng)
        p_obj = _reconv_params(rng, nl_obj)
        nl = parse_netlist(nl_obj)
        params = parse_params(nl, p_obj)
        r = verify(nl, params)
        st, bt, _, _ = enumerate_verify(nl, params, cap=80_000)
        if st == "truncated":
            continue
        if st == "safe":
            assert r["status"] == "safe", f"#{k}: z3={r['status']} sim=safe"
            continue
        assert r["status"] == "unsafe", f"#{k}: z3={r['status']} sim=bad@{bt}"
        assert r["bad_time"] == bt, f"#{k}: z3 t={r['bad_time']} sim t={bt}"
        _assert_chains_complete(r, params.outputs[0].name,
                                list({q["edge_t"]
                                      for q in r["counterexample"]["race_paths"]}))
        n_unsafe += 1
    assert n_unsafe >= 5, "随机用例应覆盖到若干 unsafe 场景"

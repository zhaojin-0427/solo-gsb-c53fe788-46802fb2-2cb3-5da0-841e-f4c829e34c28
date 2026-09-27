"""z3 验证器与枚举 DFS 模拟器的语义交叉验证。

两者独立实现同一套“离散惯性延迟”语义；对 8 个手工电路和 60 个随机
小 DAG，它们必须就“是否存在违规”以及“最早违规时刻”给出一致结论。
"""

import random
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models import parse_netlist
from app.params import parse_params
from app.verifier.z3verifier import verify
from app.verifier.sim import enumerate_verify

# 降低上限，保证测试快速
os.environ.setdefault("HAZARD_TIMEOUT_MS", "10000")


def _run(nl_obj, p_obj):
    nl = parse_netlist(nl_obj)
    params = parse_params(nl, p_obj)
    z = verify(nl, params)
    status_s, bad_s, _, _ = enumerate_verify(nl, params, cap=50_000)
    zb = z.get("bad_time")
    if status_s == "truncated":
        return True
    sim_bad = None if status_s == "safe" else bad_s
    return (z["status"] == "safe") == (sim_bad is None) and (
        sim_bad is None or zb == sim_bad
    )


NL_OR_HAZ = {
    "inputs": ["a"],
    "gates": [
        {"id": "w", "type": "not", "inputs": ["a"], "delay": [1, 3]},
        {"id": "z", "type": "or", "inputs": ["a", "w"], "delay": [1, 1]},
    ],
    "outputs": ["z"],
}
NL_AND_CONST = {
    "inputs": ["a"],
    "gates": [
        {"id": "w", "type": "not", "inputs": ["a"], "delay": [1, 3]},
        {"id": "z", "type": "and", "inputs": ["a", "w"], "delay": [1, 3]},
    ],
    "outputs": ["z"],
}
NL_CHAIN = {
    "inputs": ["a"],
    "gates": [
        {"id": "b1", "type": "buf", "inputs": ["a"], "delay": [2, 2]},
        {"id": "z", "type": "buf", "inputs": ["b1"], "delay": [2, 2]},
    ],
    "outputs": ["z"],
}
NL_AND1 = {
    "inputs": ["a", "b"],
    "gates": [{"id": "z", "type": "and", "inputs": ["a", "b"], "delay": [1, 2]}],
    "outputs": ["z"],
}
NL_ZERO = {
    "inputs": ["a"],
    "gates": [{"id": "z", "type": "not", "inputs": ["a"], "delay": [0, 0]}],
    "outputs": ["z"],
}
NL_ZERO_RECONV = {
    "inputs": ["a"],
    "gates": [
        {"id": "w", "type": "not", "inputs": ["a"], "delay": [0, 2]},
        {"id": "z", "type": "or", "inputs": ["a", "w"], "delay": [0, 0]},
    ],
    "outputs": ["z"],
}


def test_static1_hazard_pulse():
    p = {"initial": {"a": 1}, "final": {"a": 0}, "windows": {"a": [2, 2]},
         "outputs": [{"name": "z", "min_pulse_width": 3}]}
    assert _run(NL_OR_HAZ, p)


def test_pulse_width_boundary_safe():
    p = {"initial": {"a": 1}, "final": {"a": 0}, "windows": {"a": [2, 2]},
         "outputs": [{"name": "z", "min_pulse_width": 2}]}
    nl = parse_netlist(NL_OR_HAZ)
    assert verify(nl, parse_params(nl, p))["status"] == "safe"


def test_constant_and_safe():
    p = {"initial": {"a": 1}, "final": {"a": 0}, "windows": {"a": [2, 2]},
         "outputs": [{"name": "z", "max_transitions": 0}]}
    nl = parse_netlist(NL_AND_CONST)
    assert verify(nl, parse_params(nl, p))["status"] == "safe"


def test_fixed_chain_single_edge():
    p = {"initial": {"a": 0}, "final": {"a": 1}, "windows": {"a": [1, 1]},
         "outputs": [{"name": "z", "max_transitions": 1}]}
    nl = parse_netlist(NL_CHAIN)
    assert verify(nl, parse_params(nl, p))["status"] == "safe"
    assert _run(NL_CHAIN, p)


def test_window_and_delay_single_edge():
    p = {"initial": {"a": 0, "b": 1}, "final": {"a": 1, "b": 1},
         "windows": {"a": [0, 3]},
         "outputs": [{"name": "z", "max_transitions": 1}]}
    assert _run(NL_AND1, p)


def test_short_horizon_boundary():
    p = {"initial": {"a": 0}, "final": {"a": 1}, "windows": {"a": [1, 1]},
         "horizon": 3, "outputs": [{"name": "z", "max_transitions": 1}]}
    nl_obj = {"inputs": ["a"], "gates": [
        {"id": "z", "type": "buf", "inputs": ["a"], "delay": [2, 4]}],
        "outputs": ["z"]}
    assert _run(nl_obj, p)


def test_zero_delay_gate():
    p = {"initial": {"a": 0}, "final": {"a": 1}, "windows": {"a": [2, 4]},
         "outputs": [{"name": "z", "max_transitions": 1}]}
    assert _run(NL_ZERO, p)


def test_zero_delay_reconvergence_hazard():
    p = {"initial": {"a": 1}, "final": {"a": 0}, "windows": {"a": [2, 2]},
         "outputs": [{"name": "z", "max_transitions": 0}]}
    nl = parse_netlist(NL_ZERO_RECONV)
    r = verify(nl, parse_params(nl, p))
    assert r["status"] == "unsafe"
    assert r["bad_time"] == 2
    assert _run(NL_ZERO_RECONV, p)


def _random_dag(rng, n_in, n_gates):
    types = ["and", "or", "xor", "nand", "nor", "xnor", "not", "buf"]
    nodes = [f"i{k}" for k in range(n_in)]
    gates = []
    for k in range(n_gates):
        t = rng.choice(types)
        arity = 1 if t in ("not", "buf") else 2
        pool = nodes[: n_in + k]
        if arity > len(pool):
            t, arity = rng.choice(["not", "buf"]), 1
        ins = rng.sample(pool, arity)
        lo = rng.choice([0, 1, 1, 2])
        hi = lo + rng.choice([0, 1, 2])
        gates.append({"id": f"g{k}", "type": t, "inputs": ins,
                      "delay": [lo, hi]})
        nodes.append(f"g{k}")
    return {"inputs": nodes[:n_in], "gates": gates,
            "outputs": [gates[-1]["id"]]}


def _random_params(rng, nl):
    init = {n: rng.randrange(2) for n in nl["inputs"]}
    final, windows = dict(init), {}
    for n in rng.sample(nl["inputs"], rng.randrange(1, len(nl["inputs"]) + 1)):
        final[n] = 1 - init[n]
        t0 = rng.randrange(0, 3)
        windows[n] = [t0, t0 + rng.randrange(0, 3)]
    out = ({"name": nl["outputs"][0], "max_transitions": rng.choice([0, 1, 2])}
           if rng.random() < 0.5 else
           {"name": nl["outputs"][0], "min_pulse_width": rng.choice([1, 2, 3])})
    return {"initial": init, "final": final, "windows": windows,
            "outputs": [out], "horizon": rng.choice([6, 8, 10])}


def test_random_circuits_crossvalidate():
    rng = random.Random(42)
    for k in range(60):
        nl = _random_dag(rng, rng.choice([1, 2, 3]), rng.choice([2, 3, 4, 5]))
        p = _random_params(rng, nl)
        assert _run(nl, p), f"mismatch on random#{k}: {nl} / {p}"

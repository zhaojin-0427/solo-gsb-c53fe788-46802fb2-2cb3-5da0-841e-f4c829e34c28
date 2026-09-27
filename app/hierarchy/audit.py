"""组合修订行为等价审计：同步枚举输入跃迁时刻与门惯性延迟。

两份展开后的网表共用同一组主输入跃迁时刻；每个版本内部的门延迟独立取值。
模拟器逐项枚举所有可行轨迹，只把被审计组合输出端口的完整 0/1 波形放入集合。
两个版本的输出波形集合完全相同即等价；否则返回最早分歧时刻以及一对轨迹的
触发输入、各门延迟选择和两版输出波形。

这里的输出约束用于声明要审计/回放的输出端口与记录原有约束，不会作为轨迹
“过滤器”：如果只比较满足 max_transitions/min_pulse_width 的安全轨迹，可能
把修订新引入毛刺的轨迹错误排除，从而得到虚假等价。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from ..models import Netlist, eval_gate
from ..params import AnalysisParams

AUDIT_MAX_GATES = int(os.getenv("HAZARD_AUDIT_MAX_GATES", "40"))
AUDIT_MAX_HORIZON = int(os.getenv("HAZARD_AUDIT_MAX_HORIZON", "120"))
AUDIT_MAX_INPUT_ASSIGNMENTS = int(
    os.getenv("HAZARD_AUDIT_MAX_INPUT_ASSIGNMENTS", "10000"))
AUDIT_MAX_TRAJECTORIES = int(
    os.getenv("HAZARD_AUDIT_MAX_TRAJECTORIES", "200000"))
AUDIT_TIMEOUT_S = float(os.getenv("HAZARD_AUDIT_TIMEOUT_S", "5"))


class AuditScaleLimit(Exception):
    """行为审计枚举规模超过本地枚举器上限。"""


@dataclass
class TraceSet:
    traces: dict[tuple[int, ...], dict]
    input_assignments: int
    trajectory_count: int
    truncated: bool = False
    timed_out: bool = False


def check_audit_scale(nl_a: Netlist, nl_b: Netlist,
                      params: AnalysisParams) -> None:
    problems = []
    for side, nl in (("left", nl_a), ("right", nl_b)):
        if len(nl.gates) > AUDIT_MAX_GATES:
            problems.append(f"{side} 门数 {len(nl.gates)} > {AUDIT_MAX_GATES}")
    if params.horizon > AUDIT_MAX_HORIZON:
        problems.append(f"审计 H={params.horizon} > {AUDIT_MAX_HORIZON}")

    assignments = 1
    switched = [w for w in params.windows if w.is_switched]
    for w in switched:
        span = w.tmax - w.tmin + 1
        assignments *= span
        if assignments > AUDIT_MAX_INPUT_ASSIGNMENTS:
            problems.append(
                "输入跃迁时刻组合 "
                f"{'×'.join(str(x.tmax - x.tmin + 1) for x in switched)} "
                f"> {AUDIT_MAX_INPUT_ASSIGNMENTS}")
            break
    if problems:
        raise AuditScaleLimit("; ".join(problems))


def enumerate_output_traces(nl: Netlist, params: AnalysisParams,
                            output_names: list[str],
                            deadline: float | None = None) -> TraceSet:
    """枚举一份网表在所有输入 τ 和门延迟选择下的输出波形集合。

    键是按 output_names、再按 t=0..H 排列的比特元组。同一输出波形的多个
    内部延迟选择只保留确定性 DFS 遇到的第一份见证。
    """
    H = params.horizon
    gmap = nl.gate_map
    switched = [w for w in params.windows if w.is_switched]
    choices = [list(range(w.tmin, w.tmax + 1)) for w in switched]
    input_assignments = 1
    for vals in choices:
        input_assignments *= len(vals)

    c_init = {n: params.initial[n] for n in nl.inputs}
    for gid in nl.order:
        g = gmap[gid]
        c_init[gid] = eval_gate(g.type, [c_init[i] for i in g.inputs])

    C = dict(c_init)
    prev_C = dict(c_init)
    age = {g: 0 for g in nl.order}
    prev_age = {g: 0 for g in nl.order}
    fires: dict[str, list[dict]] = {g: [] for g in nl.order}
    taus = {n: None for n in nl.inputs}
    output_vec: list[int] = []
    traces: dict[tuple[int, ...], dict] = {}
    state = {"runs": 0, "truncated": False, "timed_out": False}

    def budget_exceeded() -> bool:
        if state["runs"] > AUDIT_MAX_TRAJECTORIES:
            state["truncated"] = True
            return True
        if deadline is not None and time.monotonic() >= deadline:
            state["timed_out"] = True
            return True
        return False

    def snapshot_dynamic():
        return dict(C), dict(age), {g: len(fires[g]) for g in nl.order}

    def restore_dynamic(snap):
        saved_c, saved_age, lengths = snap
        C.clear(); C.update(saved_c)
        age.clear(); age.update(saved_age)
        for g, n in lengths.items():
            del fires[g][n:]

    def process_gates(t: int, gi: int) -> None:
        if state["truncated"] or state["timed_out"]:
            return
        if gi == len(nl.order):
            finish_grid(t)
            return

        gid = nl.order[gi]
        g = gmap[gid]
        lo, hi = g.delay_min, g.delay_max
        v = eval_gate(g.type, [C[s] for s in g.inputs])
        c_prev = prev_C[gid]
        ap = prev_age[gid]
        mp = ap > 0
        mc = v != c_prev
        can = ((mp and lo <= ap <= hi) if lo >= 1
               else ((mp and ap <= hi) or mc))
        last_chance = (mp and ap >= hi) or (lo == 0 and hi == 0 and mc)

        def commit(fire: bool) -> None:
            vec_len = len(output_vec)
            snap = snapshot_dynamic()
            if fire:
                C[gid] = v
                age[gid] = 0
                fires[gid].append({"t": t, "delay": ap})
            else:
                C[gid] = c_prev
                age[gid] = ap + 1 if (mp or mc) else 0
            process_gates(t, gi + 1)
            restore_dynamic(snap)
            del output_vec[vec_len:]

        if not can:
            commit(False)
        elif last_chance:
            commit(True)
        else:
            commit(True)
            commit(False)

    def finish_grid(t: int) -> None:
        if state["truncated"] or state["timed_out"]:
            return
        vec_len = len(output_vec)
        output_vec.extend(C[n] for n in output_names)
        if t == H:
            state["runs"] += 1
            key = tuple(output_vec)
            if key not in traces:
                traces[key] = {
                    "input_transition_times": dict(taus),
                    "chosen_delays": {
                        gid: [dict(e) for e in evs]
                        for gid, evs in fires.items() if evs
                    },
                }
            if budget_exceeded():
                del output_vec[vec_len:]
                return
        else:
            saved_prev_c = dict(prev_C)
            saved_prev_a = dict(prev_age)
            prev_C.clear(); prev_C.update(C)
            prev_age.clear(); prev_age.update(age)
            tick(t + 1)
            prev_C.clear(); prev_C.update(saved_prev_c)
            prev_age.clear(); prev_age.update(saved_prev_a)
        del output_vec[vec_len:]

    def tick(t: int) -> None:
        if state["truncated"] or state["timed_out"]:
            return
        for n in nl.inputs:
            C[n] = params.initial[n]
        for w in switched:
            if t >= taus[w.name]:
                C[w.name] = w.value_after
        for gid in nl.order:
            C[gid] = prev_C[gid]
            age[gid] = prev_age[gid]
        process_gates(t, 0)

    def choose_tau(i: int) -> None:
        if state["truncated"] or state["timed_out"]:
            return
        if i == len(switched):
            C.clear(); C.update(c_init)
            prev_C.clear(); prev_C.update(c_init)
            age.clear(); age.update({g: 0 for g in nl.order})
            prev_age.clear(); prev_age.update({g: 0 for g in nl.order})
            for g in nl.order:
                fires[g] = []
            output_vec.clear()
            tick(0)
            return
        w = switched[i]
        for tau in choices[i]:
            taus[w.name] = tau
            choose_tau(i + 1)
            if state["truncated"] or state["timed_out"]:
                return
        taus[w.name] = None

    choose_tau(0)
    return TraceSet(
        traces=traces,
        input_assignments=input_assignments,
        trajectory_count=state["runs"],
        truncated=state["truncated"],
        timed_out=state["timed_out"],
    )


def _waveform_from_key(key: tuple[int, ...], outputs: list[str],
                       initial: dict[str, int], width: int):
    waves = []
    for i, name in enumerate(outputs):
        vals = key[i::len(outputs)]
        edges = []
        prev = initial[name]
        for t, bit in enumerate(vals):
            if bit != prev:
                edges.append({"t": t, "v": bit})
                prev = bit
        waves.append({"output": name, "initial": initial[name], "edges": edges})
    return waves


def _full_waveforms(nl: Netlist, params: AnalysisParams, witness: dict):
    """根据已选定的输入 τ 与各门点火事件确定性重放完整节点波形。"""
    H = params.horizon
    switched = {w.name: w for w in params.windows if w.is_switched}
    fires = {
        gid: {e["t"] for e in evs}
        for gid, evs in witness.get("chosen_delays", {}).items()
    }
    C = {n: params.initial[n] for n in nl.inputs}
    for gid in nl.order:
        g = nl.gate_map[gid]
        C[gid] = eval_gate(g.type, [C[i] for i in g.inputs])
    initial = dict(C)
    result = {n: {"node": n,
                  "kind": "input" if n in nl.inputs else "gate",
                  "initial": initial[n], "edges": [], "fires": []}
              for n in nl.all_nodes}
    taus = witness.get("input_transition_times", {})
    for t in range(H + 1):
        for n in nl.inputs:
            w = switched.get(n)
            tau = taus.get(n)
            nv = w.value_after if w is not None and tau is not None and t >= tau \
                else params.initial[n]
            if nv != C[n]:
                C[n] = nv
                result[n]["edges"].append({"t": t, "v": nv})
        for gid in nl.order:
            g = nl.gate_map[gid]
            v = eval_gate(g.type, [C[i] for i in g.inputs])
            if t in fires.get(gid, set()):
                if v != C[gid]:
                    result[gid]["edges"].append({"t": t, "v": v})
                result[gid]["fires"].append({"t": t, "v": v})
                C[gid] = v
    for n in nl.all_nodes:
        if n in nl.inputs:
            result[n].pop("fires")
    return [result[n] for n in nl.all_nodes]


class _Trie:
    __slots__ = ("children", "witness", "key")

    def __init__(self):
        self.children: dict[int, "_Trie"] | None = None
        self.witness = None
        self.key: tuple[int, ...] | None = None

    def insert(self, key: tuple[int, ...], witness: dict) -> None:
        node = self
        for bit in key:
            if node.children is None:
                node.children = {}
            node = node.children.setdefault(bit, _Trie())
        node.witness = witness
        node.key = key

    def first(self):
        node = self
        bits = []
        while node.children:
            bit = min(node.children)
            bits.append(bit)
            node = node.children[bit]
        return node.key, node.witness

    def divergence(self, key: tuple[int, ...]):
        """返回 (首个分歧下标, 对方位, 对方完整轨迹, 见证)；完全相同返回 None。"""
        node = self
        for idx, bit in enumerate(key):
            if node.children is None or bit not in node.children:
                other_bit = min(node.children) if node.children else None
                if other_bit is None:
                    return idx, None, None, None
                other_key, other_witness = node.children[other_bit].first()
                return idx, other_bit, other_key, other_witness
            node = node.children[bit]
        if not node.children:
            return None
        idx = len(key)
        other_bit = min(node.children)
        other_key, other_witness = node.children[other_bit].first()
        return idx, other_bit, other_key, other_witness


def _find_divergence(nl_a: Netlist, nl_b: Netlist,
                     params_a: AnalysisParams, params_b: AnalysisParams,
                     a: TraceSet, b: TraceSet, a_rev: int, b_rev: int,
                     a_outs: list[str], b_outs: list[str],
                     a_init: dict[str, int], b_init: dict[str, int],
                     width: int):
    trie_b = _Trie()
    for key, wit in b.traces.items():
        trie_b.insert(key, wit)
    trie_a = _Trie()
    for key, wit in a.traces.items():
        trie_a.insert(key, wit)

    candidates = []
    # dict 按枚举顺序插入；再排序确保重放完全确定。
    for key in sorted(a.traces):
        found = trie_b.divergence(key)
        if found is not None:
            idx, b_bit, b_key, b_wit = found
            candidates.append((idx, "left_extra", key, a.traces[key],
                                b_key, b_wit))
    for key in sorted(b.traces):
        found = trie_a.divergence(key)
        if found is not None:
            idx, a_bit, a_key, a_wit = found
            # 方向换算为 left key / right key
            candidates.append((idx, "right_extra", a_key, a_wit,
                                key, b.traces[key]))
    if not candidates:
        return None
    idx, direction, left_key, left_wit, right_key, right_wit = min(
        candidates, key=lambda x: (x[0], x[1], x[2] or (), x[4] or ()))[:6]
    out_count = len(a_outs)
    t = idx // out_count
    out_i = idx % out_count
    return {
        "direction": direction,
        "time": t,
        "output_index": out_i,
        "left_output": a_outs[out_i],
        "right_output": b_outs[out_i],
        "left_value": left_key[idx] if left_key else None,
        "right_value": right_key[idx] if right_key else None,
        "trigger": {
            "left_revision": a_rev,
            "right_revision": b_rev,
            "input_transition_times": left_wit["input_transition_times"],
        },
        "delay_choices": {
            f"revision_{a_rev}": left_wit["chosen_delays"],
            f"revision_{b_rev}": right_wit["chosen_delays"],
        },
        "waveforms": {
            f"revision_{a_rev}": _waveform_from_key(
                left_key, a_outs, a_init, width),
            f"revision_{b_rev}": _waveform_from_key(
                right_key, b_outs, b_init, width),
        },
        "full_waveforms": {
            f"revision_{a_rev}": _full_waveforms(nl_a, params_a, left_wit),
            f"revision_{b_rev}": _full_waveforms(nl_b, params_b, right_wit),
        },
    }


def audit_equivalence(nl_a: Netlist, params_a: AnalysisParams,
                      nl_b: Netlist, params_b: AnalysisParams,
                      output_pairs: list[tuple[str, str, str]],
                      revisions: tuple[int, int]) -> dict:
    """对两个已展开版本做完整有界枚举并返回结构化审计结果。"""
    check_audit_scale(nl_a, nl_b, params_a)
    H = params_a.horizon
    top_outs = [x[0] for x in output_pairs]
    outs_a = [x[1] for x in output_pairs]
    outs_b = [x[2] for x in output_pairs]
    started = time.monotonic()
    deadline = started + AUDIT_TIMEOUT_S

    set_a = enumerate_output_traces(nl_a, params_a, outs_a, deadline)
    set_b = enumerate_output_traces(nl_b, params_b, outs_b, deadline)

    limits = {
        "max_gates_per_revision": AUDIT_MAX_GATES,
        "max_horizon": AUDIT_MAX_HORIZON,
        "max_input_assignments": AUDIT_MAX_INPUT_ASSIGNMENTS,
        "max_trajectories_per_revision": AUDIT_MAX_TRAJECTORIES,
        "timeout_seconds": AUDIT_TIMEOUT_S,
    }
    if set_a.timed_out or set_b.timed_out:
        return _unknown("timeout", H, set_a, set_b, limits, revisions)
    if set_a.truncated or set_b.truncated:
        return _unknown("trajectory_limit", H, set_a, set_b, limits, revisions)

    stats = _stats(H, nl_a, nl_b, params_a, set_a, set_b)
    init_a = _initial_for(nl_a, params_a, nl_a.all_nodes)
    init_b = _initial_for(nl_b, params_b, nl_b.all_nodes)
    divergence = _find_divergence(
        nl_a, nl_b, params_a, params_b,
        set_a, set_b, revisions[0], revisions[1],
        top_outs, top_outs,
        {top: init_a[flat_a] for top, flat_a, _ in output_pairs},
        {top: init_b[flat_b] for top, _, flat_b in output_pairs}, H)
    if divergence is not None:
        return {
            "status": "inequivalent",
            "equivalent": False,
            "horizon": H,
            "message": (
                f"修订 {revisions[0]} 与 {revisions[1]} 存在可行轨迹在 "
                f"t={divergence['time']} 的输出 "
                f"{divergence['left_output']} 上首次分歧"),
            "earliest_divergence": divergence,
            "statistics": stats,
        }

    return {
        "status": "equivalent",
        "equivalent": True,
        "horizon": H,
        "message": f"在 t=0..{H} 内，两修订所有可行轨迹的审计输出波形均相同",
        "coverage": stats["coverage"],
        "statistics": stats,
    }


def _initial_for(nl: Netlist, params: AnalysisParams, outputs: list[str]):
    vals = {n: params.initial[n] for n in nl.inputs}
    for gid in nl.order:
        g = nl.gate_map[gid]
        vals[gid] = eval_gate(g.type, [vals[i] for i in g.inputs])
    return {name: vals[name] for name in outputs}


def _stats(H, nl_a, nl_b, params, set_a, set_b):
    window_end = max((w.tmax for w in params.windows), default=0)
    longest = max(nl_a.longest_path_delay(), nl_b.longest_path_delay())
    settling = window_end + longest
    return {
        "input_assignments": set_a.input_assignments,
        "left_trajectories": set_a.trajectory_count,
        "right_trajectories": set_b.trajectory_count,
        "left_distinct_output_waveforms": len(set_a.traces),
        "right_distinct_output_waveforms": len(set_b.traces),
        "output_count": len(params.outputs),
        "coverage": {
            "covered_time": H,
            "window_end_max": window_end,
            "longest_static_path": longest,
            "settling_bound": settling,
            "transient_fully_covered": H >= settling,
            "pending_note": (
                None if H >= settling
                else f"H={H} 小于完全稳定界 {settling}，t>{H} 的行为未覆盖"),
        },
    }


def _unknown(reason: str, H: int, set_a: TraceSet, set_b: TraceSet,
             limits: dict, revisions: tuple[int, int]) -> dict:
    reason_text = {
        "timeout": f"枚举超过 {AUDIT_TIMEOUT_S} 秒预算",
        "trajectory_limit": "可行轨迹数超过枚举上限",
    }[reason]
    return {
        "status": "unknown",
        "equivalent": None,
        "reason": reason,
        "message": f"无法判定修订 {revisions[0]} 与 {revisions[1]} 是否等价：{reason_text}",
        "horizon": H,
        "limits": limits,
        "statistics": {
            "input_assignments": set_a.input_assignments,
            "left_trajectories": set_a.trajectory_count,
            "right_trajectories": set_b.trajectory_count,
            "left_distinct_output_waveforms": len(set_a.traces),
            "right_distinct_output_waveforms": len(set_b.traces),
            "left_timed_out": set_a.timed_out,
            "right_timed_out": set_b.timed_out,
            "left_truncated": set_a.truncated,
            "right_truncated": set_b.truncated,
        },
    }

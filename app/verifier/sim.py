"""枚举式 DFS 参考模拟器（仅用于小规模交叉验证）。

直接复刻 z3verifier 的逐格递归方程。对门 g 在格 t：
  c_prev = 上格输出 C_g(t-1)（t=0 为初态）
  ap     = 上格末失配年龄 age_g(t-1)；t=0 为 0
  mp     = 上格失配未点火（ap>0）；t=0 为假
  mc     = 本格失配 V_g(t)≠c_prev（V 沿拓扑序含同格零延迟变化）
  can_fire:
    lo>=1 : mp 且 lo<=ap<=hi
    lo==0 : (mp 且 ap<=hi) 或 mc
  最晚时机 last_chance：(mp 且 ap>=hi) 或 ([0,0] 失配) → 必须点火；
  其余 can_fire 情形在“点火 / 等待”间分支枚举。
  age(t) = 点火?0 : (mp 或 mc)? ap+1 : 0
  C(t)   = 点火? V(t) : c_prev

门按拓扑序处理，d=0 点火同格对后续门可见。选择点只有输入 τ 与点火/等待。
"""

from __future__ import annotations

from ..models import Netlist, eval_gate
from ..params import AnalysisParams


def enumerate_verify(nl: Netlist, params: AnalysisParams, cap: int = 200_000):
    """穷举搜索最早违规。

    返回 (status, bad_t, witness, n_runs)：status ∈ {"bad","safe","truncated"}。
    """
    gmap = nl.gate_map
    H = params.horizon

    c_init = {n: params.initial[n] for n in nl.inputs}
    for gid in nl.order:
        c_init[gid] = eval_gate(gmap[gid].type,
                                [c_init[i] for i in gmap[gid].inputs])

    sw = [w for w in params.windows if w.is_switched]
    tau_ranges = [list(range(w.tmin, w.tmax + 1)) for w in sw]

    st = {"best_t": None, "n": 0, "trunc": False}

    C = dict(c_init)        # 本格门输出（随处理推进被更新）
    prev_C = dict(c_init)   # 上格末输出
    age = {g: 0 for g in nl.order}
    prev_age = {g: 0 for g in nl.order}
    trace = {n: [None] * (H + 1) for n in nl.all_nodes}
    last_edge = {s.name: -1 for s in params.outputs}
    count = {s.name: 0 for s in params.outputs}
    taus: dict[str, int] = {}

    def snap_gates():
        return dict(C), dict(prev_C), dict(age), dict(prev_age)

    def restore_gates(s):
        c, pc, a, pa = s
        C.clear(); C.update(c)
        prev_C.clear(); prev_C.update(pc)
        age.clear(); age.update(a)
        prev_age.clear(); prev_age.update(pa)

    def snap_all():
        return (dict(C), dict(prev_C), dict(age), dict(prev_age),
                dict(last_edge), dict(count),
                {k: list(v) for k, v in trace.items()})

    def restore_all(s):
        c, pc, a, pa, le, ct, tr = s
        C.clear(); C.update(c)
        prev_C.clear(); prev_C.update(pc)
        age.clear(); age.update(a)
        prev_age.clear(); prev_age.update(pa)
        last_edge.clear(); last_edge.update(le)
        count.clear(); count.update(ct)
        trace.clear(); trace.update(tr)

    def process_gates(t, gi):
        """拓扑序处理第 gi 个门；点火/等待处做分支 DFS。"""
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

        def commit(fire: bool):
            if fire:
                C[gid] = v
                age[gid] = 0
            else:
                C[gid] = c_prev
                age[gid] = ap + 1 if (mp or mc) else 0
            process_gates(t, gi + 1)

        if not can:
            commit(False)
        elif last_chance:
            commit(True)
        else:
            s = snap_gates()
            commit(True)
            restore_gates(s)
            commit(False)
            restore_gates(s)

    def finish_grid(t):
        # trace 写入属于本格副作用，必须在分支快照内，回溯时一并还原。
        s_all = snap_all()
        for n in nl.all_nodes:
            trace[n][t] = C[n]
        bad = False
        for spec in params.outputs:
            node = spec.name
            prev = c_init[node] if t == 0 else trace[node][t - 1]
            if C[node] == prev:
                continue
            if spec.min_pulse_width is not None and last_edge[node] >= 0 \
                    and t - last_edge[node] < spec.min_pulse_width:
                bad = True
            count[node] += 1
            if spec.max_transitions is not None \
                    and count[node] > spec.max_transitions:
                bad = True
            last_edge[node] = t
        if bad and (st["best_t"] is None or t < st["best_t"]):
            st["best_t"] = t
            restore_all(s_all)
            return
        if t < H and (st["best_t"] is None or t + 1 < st["best_t"]):
            prev_C.clear(); prev_C.update(C)
            prev_age.clear(); prev_age.update(age)
            tick(t + 1)
        restore_all(s_all)

    def tick(t):
        if st["best_t"] is not None and t >= st["best_t"]:
            return
        for n in nl.inputs:
            C[n] = params.initial[n]
        for w in sw:
            if t >= taus[w.name]:
                C[w.name] = w.value_after
        for gid in nl.order:
            C[gid] = prev_C[gid]
        process_gates(t, 0)

    def choose_tau(i):
        if st["trunc"] or st["best_t"] == 0:
            return
        if i == len(sw):
            st["n"] += 1
            if st["n"] > cap:
                st["trunc"] = True
                return
            C.clear(); C.update(c_init)
            prev_C.clear(); prev_C.update(c_init)
            age.clear(); age.update({g: 0 for g in nl.order})
            prev_age.clear(); prev_age.update({g: 0 for g in nl.order})
            for n in nl.all_nodes:
                trace[n] = [None] * (H + 1)
            for s in params.outputs:
                last_edge[s.name] = -1
                count[s.name] = 0
            tick(0)
            return
        w = sw[i]
        for d in tau_ranges[i]:
            taus[w.name] = d
            choose_tau(i + 1)

    choose_tau(0)

    if st["trunc"]:
        return "truncated", None, None, st["n"]
    if st["best_t"] is None:
        return "safe", None, None, st["n"]
    return "bad", st["best_t"], {"input_times": dict(taus)}, st["n"]

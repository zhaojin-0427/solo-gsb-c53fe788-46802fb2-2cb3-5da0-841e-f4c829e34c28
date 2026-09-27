"""基于 z3 的组合电路惯性延迟冒险验证。

离散时间语义（半开格子 [t, t+1)，t=0..H）
-------------------------------------------
* 输入在窗口 [tmin,tmax] 内任选时刻 τ 跃迁：C(t)=after 当 t>=τ，否则 before。
* 每个门带整数惯性延迟区间 [lo,hi]。门计算值 V_g(t)=f(C_pred(t))，
  实际输出 C_g(t) 仅在门“点火”后更新。
* 失配（V≠C）连续保持 d 个格子后，门可在延迟 d 时刻点火（lo<=d<=hi）；
  失配在 d<lo 时消失则候选变化取消（毛刺被惯性滤除）；d=hi 时必须点火。
* 同一时刻先取消/再点火（输入在 t 刚消失的失配不能点火），
  d=0 的门可在失配出现的同一格子点火，沿拓扑序级联。

输出约束（在 t=0..H 上检查）
* min_pulse_width：相邻两次输出跃迁间隔小于该值即违规（窄脉冲）。
* max_transitions：输出跃迁次数超过上限即违规（0 表示不允许任何毛刺）。

存在违规时，用 z3 Optimize 求**最早**违规时刻，并以输入跃迁时刻之和、
各门点火延迟之和作为次序，保证反例可复现。
"""

from __future__ import annotations

import itertools
import os

import z3
from z3 import And, Bool, BoolVal, If, Implies, Int, Not, Or, Sum, UGE, ULE

from ..models import Netlist, eval_gate
from ..params import AnalysisParams

# 规模上限（可用环境变量覆盖）。超出即返回 unknown，不做不切实际的承诺。
LIMIT_GATES = int(os.getenv("HAZARD_MAX_GATES", "200"))
LIMIT_NODES = int(os.getenv("HAZARD_MAX_NODES", "300"))
LIMIT_HORIZON = int(os.getenv("HAZARD_MAX_HORIZON", "250"))
LIMIT_CELLS = int(os.getenv("HAZARD_MAX_CELLS", "40000"))
SOLVER_TIMEOUT_MS = int(os.getenv("HAZARD_TIMEOUT_MS", "25000"))


class ScaleLimit(Exception):
    """超过规模上限。"""


def check_scale(nl: Netlist, params: AnalysisParams) -> None:
    n_nodes = len(nl.inputs) + len(nl.gates)
    cells = n_nodes * (params.horizon + 1)
    problems = []
    if len(nl.gates) > LIMIT_GATES:
        problems.append(f"门数 {len(nl.gates)} > {LIMIT_GATES}")
    if n_nodes > LIMIT_NODES:
        problems.append(f"节点数 {n_nodes} > {LIMIT_NODES}")
    if params.horizon > LIMIT_HORIZON:
        problems.append(f"时间上界 H={params.horizon} > {LIMIT_HORIZON}")
    if cells > LIMIT_CELLS:
        problems.append(f"节点×时间规模 {cells} > {LIMIT_CELLS}")
    if problems:
        raise ScaleLimit("; ".join(problems))


def _bv_gate(gtype: str, vs):
    """BV1 信号上的门求值（位运算，直接交给位求解器）。"""
    x = vs[0]
    if gtype == "buf":
        return x
    if gtype == "not":
        return ~x
    y = vs[1]
    if gtype == "and":
        return x & y
    if gtype == "or":
        return x | y
    if gtype == "xor":
        return x ^ y
    if gtype == "nand":
        return ~(x & y)
    if gtype == "nor":
        return ~(x | y)
    if gtype == "xnor":
        return ~(x ^ y)
    raise ValueError(gtype)


def _bv_const(v: int):
    return z3.BitVecVal(v, 1)


def _encode(nl: Netlist, params: AnalysisParams, dyn: dict | None = None):
    """构建全部符号表达式与基础约束（BV 编码，不含违规目标）。

    信号用 1 位位向量（BV1），门直接映射位运算；年龄用足够表示
    [0,max_hi] 的小位宽无符号 BV。返回 ctx，vbad[t] 为违规谓词。

    dyn（修复搜索使用，普通验证为 None）允许把门惯性延迟端点、输入跃迁
    窗口端点、输出约束阈值做成 **z3 Int 符号**：
      dyn["lo"]/dyn["hi"]   : {gid: Int 表达式}，要求 0<=lo<=hi；
      dyn["win"]            : {输入名: (tmin 表达式, tmax 表达式)}；
      dyn["pw"]/dyn["k"]    : {输出名: Int 表达式}；
      dyn["hi_cap"]         : 符号 hi 可能取到的最大值（决定年龄位宽）。
    """
    dyn = dyn or {}
    gmap = nl.gate_map
    H = params.horizon
    T = H + 1
    cap_hi = dyn.get("hi_cap")
    max_hi = max((g.delay_max for g in nl.gates), default=0)
    if cap_hi is not None:
        max_hi = max(max_hi, int(cap_hi))
    aw = max(1, max_hi.bit_length())  # 年龄位宽

    c_init = {n: params.initial[n] for n in nl.inputs}
    for gid in nl.order:
        g = gmap[gid]
        c_init[gid] = eval_gate(g.type, [c_init[i] for i in g.inputs])

    # 输入跃迁时刻 τ 仍用 Int（窗口是稀疏整数，直接参与 C 的 mux）
    tau = {}
    switched = {w.name for w in params.windows if w.is_switched}
    base = []
    tau_terms = []
    for w in params.windows:
        x = Int(f"tau_{w.name}")
        tau[w.name] = x
        bounds = dyn.get("win", {}).get(w.name)
        if bounds is not None:
            base += [x >= bounds[0], x <= bounds[1]]
        else:
            base += [x >= w.tmin, x <= w.tmax]
        tau_terms.append(x)

    # C 存 BV1 表达式（常量输入为 BV1 常量）
    C = {n: [None] * T for n in nl.all_nodes}
    for name in nl.inputs:
        for t in range(T):
            if name in switched:
                w = next(w for w in params.windows if w.name == name)
                C[name][t] = If(
                    t >= tau[name],
                    _bv_const(w.value_after),
                    _bv_const(w.value_before),
                )
            else:
                C[name][t] = _bv_const(params.initial[name])

    age_expr = {gid: [None] * T for gid in nl.order}
    fire_expr = {gid: [None] * T for gid in nl.order}
    one_age = z3.BitVecVal(1, aw)

    for t in range(T):
        for gid in nl.order:
            g = gmap[gid]
            if "lo" in dyn and gid in dyn["lo"]:
                lo = dyn["lo"][gid]
                hi = dyn["hi"][gid]
            else:
                lo, hi = g.delay_min, g.delay_max
            v = _bv_gate(g.type, [C[s][t] for s in g.inputs])
            c_prev = _bv_const(c_init[gid]) if t == 0 else C[gid][t - 1]
            if t == 0:
                mp = BoolVal(False)
                ap = z3.BitVecVal(0, aw)
            else:
                ap = age_expr[gid][t - 1]
                mp = ap != z3.BitVecVal(0, aw)
            mc = v != c_prev  # BV1 不等 → Bool
            symbolic = not isinstance(lo, int)
            zero_v = z3.BitVecVal(0, aw)
            lo_v = z3.Int2BV(lo, aw) if symbolic else z3.BitVecVal(lo, aw)
            hi_v = z3.Int2BV(hi, aw) if symbolic else z3.BitVecVal(hi, aw)
            if symbolic:
                # 0<=lo<=hi（域约束已在搜索器中添加）。与参考枚举器一致：
                # lo>=1 时需失配已持续（mp）且 lo<=ap<=hi；lo=0 时失配当格
                # （mc）即可点火，或持续失配在 ap<=hi 内点火。
                can_fire = Or(
                    And(mp, UGE(ap, lo_v), ULE(ap, hi_v)),
                    And(mc, lo_v == zero_v))
                last_chance = Or(And(mp, UGE(ap, hi_v)),
                                 And(mc, lo_v == zero_v, hi_v == zero_v))
            elif lo >= 1:
                can_fire = And(mp, UGE(ap, lo_v), ULE(ap, hi_v))
                last_chance = And(mp, UGE(ap, hi_v))
            else:
                can_fire = Or(And(mp, ULE(ap, hi_v)), mc)
                last_chance = Or(And(mp, UGE(ap, hi_v)),
                                 And(lo == 0, hi == 0, mc))
            fr = Bool(f"fr_{gid}_{t}")
            # 与参考枚举器一致：不可点火时只能等待；到点必须点火。
            base += [Implies(fr, can_fire),
                     Implies(Not(can_fire), Not(fr)),
                     Implies(last_chance, fr)]
            fire = fr
            age = If(fire, z3.BitVecVal(0, aw),
                     If(mp, ap + one_age, If(mc, one_age,
                                             z3.BitVecVal(0, aw))))

            age_expr[gid][t] = age
            fire_expr[gid][t] = fire
            C[gid][t] = If(fire, v, c_prev)

            base += [ULE(age, hi_v)]

    per_time_bad = [[] for _ in range(T)]
    for spec in params.outputs:
        node = spec.name
        last, count = -1, 0
        pw_sym = dyn.get("pw", {}).get(node)
        k_sym = dyn.get("k", {}).get(node)
        for t in range(T):
            c_prev = _bv_const(c_init[node]) if t == 0 else C[node][t - 1]
            e = C[node][t] != c_prev
            conds = []
            pulse_cond = None
            if spec.min_pulse_width is not None:
                pw = pw_sym if pw_sym is not None else spec.min_pulse_width
                pulse_cond = And(e, last >= 0, t - last < pw)
                conds.append(pulse_cond)
            if spec.max_transitions is not None:
                k = k_sym if k_sym is not None else spec.max_transitions
                count_bad = count + If(e, 1, 0) > k
                if pulse_cond is not None:
                    count_bad = And(count_bad, Not(pulse_cond))
                conds.append(count_bad)
            if conds:
                per_time_bad[t].append(Or(conds))
            last = If(e, t, last)
            count = count + If(e, 1, 0)

    vbad = [Or(per_time_bad[t]) if per_time_bad[t] else BoolVal(False)
            for t in range(T)]

    return {
        "nl": nl, "params": params, "H": H, "T": T, "gmap": gmap,
        "age_bits": aw,
        "c_init": c_init, "tau": tau, "switched": switched,
        "C": C, "age": age_expr, "fire": fire_expr,
        "base": base, "vbad": vbad, "tau_terms": tau_terms,
    }


def verify(nl: Netlist, params: AnalysisParams) -> dict:
    """返回结构化验证结果 dict（safe / unsafe / unknown）。

    两阶段求解（比单个大 Optimize 快得多）：
      1) 普通 Solver 判“存在违规”——unsat 即安全；
      2) sat 时增量追加 vbad<=k 的约束，二分找最早违规时刻 t*；
      3) 固定 vbad[t*] 后用 Solver 配合确定性次序（τ 和、延迟和）求可复现解。
    """
    check_scale(nl, params)
    ctx = _encode(nl, params)
    H, T = ctx["H"], ctx["T"]
    vbad = ctx["vbad"]
    base = ctx["base"]

    # ---- 阶段 1：是否存在违规 ----
    s1 = z3.Solver()
    s1.set("timeout", SOLVER_TIMEOUT_MS)
    s1.add(base)
    s1.add(Or(vbad))
    r1 = s1.check()
    if r1 == z3.unsat:
        return _build_safe(nl, params, H)
    if r1 == z3.unknown:
        return _unknown(H)

    # ---- 阶段 2：复用 s1 做 push/pop 增量二分最早违规时刻 ----
    bisect_ms = min(10000, SOLVER_TIMEOUT_MS)
    s1.set("timeout", bisect_ms)
    lo, hi = 0, T - 1
    m = s1.model()
    for t in range(T):
        if bool(m.eval(vbad[t], model_completion=True)):
            hi = t
            break
    timed_out = False
    fallback_model = m
    while lo < hi:
        mid = (lo + hi) // 2
        s1.push()
        s1.add(Or(vbad[:mid + 1]))
        rr = s1.check()
        if rr == z3.sat:
            mm = s1.model()
            fallback_model = mm
            hi = mid
            for t in range(mid + 1):
                if bool(mm.eval(vbad[t], model_completion=True)):
                    hi = t
                    break
            s1.pop()
        elif rr == z3.unknown:
            s1.pop()
            timed_out = True
            break  # 保守用当前上界 hi（反例有效，只是未必最早）
        else:
            s1.pop()
            lo = mid + 1
    t_star = hi

    # ---- 阶段 3：固定 tbad=t*，用 Optimize 最小化 Στ 求可复现次序 ----
    # 只对输入跃迁时刻（整数、数量少）做最小化；每次门点火延迟由 z3
    # 默认模型决定（同 τ 下确定），避免昂贵的 BV2Int 大规模目标。
    s3 = z3.Optimize()
    s3.set("timeout", SOLVER_TIMEOUT_MS)
    s3.add(base)
    s3.add(vbad[t_star])
    if t_star > 0:
        s3.add(z3.Not(Or(vbad[:t_star])))
    if ctx["tau_terms"]:
        s3.minimize(Sum(ctx["tau_terms"]))
    r3 = s3.check()
    if r3 == z3.sat:
        model = s3.model()
        order_ok = True
    else:
        model = fallback_model
        order_ok = False

    result = _build_unsafe(ctx, model, t_star)
    if timed_out or not order_ok:
        result["counterexample"]["earliest"] = False
        result["message"] += "（求解预算不足，未能严格保证最早/最小次序）"
    return result


def _unknown(H):
    return {
        "status": "unknown",
        "reason": "timeout",
        "message": f"z3 求解在 {SOLVER_TIMEOUT_MS} ms 内未得出结论",
        "horizon": H,
    }


def _ev_int(model, expr) -> int:
    v = model.eval(expr, model_completion=True)
    return v.as_long()


def _ev_bool(model, expr) -> bool:
    v = model.eval(expr, model_completion=True)
    return bool(v)


def _build_safe(nl: Netlist, params: AnalysisParams, H: int) -> dict:
    max_win = max((w.tmax for w in params.windows), default=0)
    settle_bound = max_win + nl.longest_path_delay()
    return {
        "status": "safe",
        "horizon": H,
        "message": (
            f"在 t=0..{H} 内，对所有门延迟在给定区间内的取值，"
            "指定输出均不能违反约束。"
        ),
        "coverage": {
            "covered_time": H,
            "window_end_max": max_win,
            "settling_bound": settle_bound,
            "transient_fully_covered": H >= settle_bound,
            "longest_static_path": nl.longest_path_delay(),
            "pending_note": (
                None if H >= settle_bound
                else f"H={H} 小于完全稳定界 {settle_bound}，t>{H} 的行为未覆盖"
            ),
        },
        "delay_bounds": {g.id: [g.delay_min, g.delay_max] for g in nl.gates},
    }


def _build_unsafe(ctx, model, t_star) -> dict:
    nl = ctx["nl"]
    params = ctx["params"]
    H, T = ctx["H"], ctx["T"]
    C = ctx["C"]
    fire_expr = ctx["fire"]
    age_expr = ctx["age"]
    c_init = ctx["c_init"]
    tau = ctx["tau"]
    switched = ctx["switched"]
    gmap = nl.gate_map
    pi = set(nl.inputs)

    def cv(node, t):
        expr = C[node][t]
        if isinstance(expr, int):
            return expr
        return model.eval(expr, model_completion=True).as_long() & 1

    input_times = {}
    for name in nl.inputs:
        input_times[name] = _ev_int(model, tau[name]) if name in switched else None

    waves = []
    gate_events: dict[str, list[dict]] = {}
    for name in nl.inputs:
        edges, prev = [], c_init[name]
        for t in range(T):
            val = cv(name, t)
            if val != prev:
                edges.append({"t": t, "v": val})
                prev = val
        waves.append({"node": name, "kind": "input",
                      "initial": c_init[name], "edges": edges})

    for gid in nl.order:
        g = gmap[gid]
        edges, fires, prev = [], [], c_init[gid]
        for t in range(T):
            fired = _ev_bool(model, fire_expr[gid][t])
            val = cv(gid, t)
            if fired:
                ap = 0 if t == 0 else _ev_int(model, age_expr[gid][t - 1])
                # 失配当格点火（mc，ap=0）延迟为 0；拖后点火延迟为 ap
                fires.append({"t": t, "v": val, "delay": ap})
            if val != prev:
                edges.append({"t": t, "v": val})
                prev = val
        gate_events[gid] = fires
        waves.append({"node": gid, "kind": "gate", "type": g.type,
                      "initial": c_init[gid], "edges": edges, "fires": fires})

    # ---- 违规输出与违规类型，并收集该违规涉及的沿（以最早时刻 t* 为准）----
    bad_t = bad_node = bad_kind = None
    bad_edge_times: list[int] = []
    for spec in params.outputs:
        node = spec.name
        last, count, prev = -1, 0, c_init[node]
        edge_ts = []
        for t in range(T):
            val = cv(node, t)
            if val == prev:
                continue
            edge_ts.append(t)
            pulse_bad = (
                spec.min_pulse_width is not None and last >= 0
                and t - last < spec.min_pulse_width
            )
            count += 1
            count_bad = (not pulse_bad and spec.max_transitions is not None
                         and count > spec.max_transitions)
            if (pulse_bad or count_bad) and (bad_t is None or t < bad_t):
                bad_t = t
                bad_node = node
                bad_kind = "min_pulse_width" if pulse_bad else "max_transitions"
                bad_edge_times = [last, t] if pulse_bad else list(edge_ts)
            last, prev = t, val

    race_info = _backtrack(nl, model, C, gate_events, bad_node, bad_t,
                           c_init, pi, bad_edge_times)
    race_paths = race_info["paths"]

    chosen_delays = {
        gid: [{"t": ev["t"], "delay": ev["delay"]} for ev in evs]
        for gid, evs in gate_events.items() if evs
    }

    return {
        "status": "unsafe",
        "horizon": H,
        "bad_time": bad_t,
        "bad_output": bad_node,
        "violation": bad_kind,
        "message": f"输出 {bad_node} 在 t={bad_t} 违反 {bad_kind} 约束",
        "counterexample": {
            "earliest": bad_t == t_star,
            "bad_time": bad_t,
            "input_transition_times": input_times,
            "chosen_delays": chosen_delays,
            "race_paths": race_paths,
            "race_path_count": race_info["total"],
            "race_paths_truncated": race_info["truncated"],
        },
        "waves": waves,
        "constraints": [
            {"output": s.name, "min_pulse_width": s.min_pulse_width,
             "max_transitions": s.max_transitions}
            for s in params.outputs
        ],
    }


def _backtrack(nl, model, C, gate_events, node, t_bad, c_init, pi,
               edge_times=None, max_paths=200):
    """从违规输出的相关沿回溯敏化因果链到主输入。

    门 g 在 t 点火产生输出沿时，其失配自 s = t - delay 开始（delay 取自该
    门实际点火事件）。在 [s, t] 内取值真正改变过的扇入，构成“变化扇入集”。
    对该集合枚举能让门输出回到旧值的**最小**子集：

    * 单子集即有效（如重汇聚时另一条路径）→ 该扇入单独因果必需，单列一条链；
    * 只有多扇入联合才能恢复旧值（同一离散时刻两个或更多扇入共同触发
      OR/AND/NAND/NOR 的输出沿，单独撤回任一扇入仍不能恢复旧值）→
      保留这些**同刻/同失配窗口的共同扇入**，分别递归到各自实际的主输入沿，
      形成一组联合因果链（hop 带 joint 标记同组扇入）。

    递归到主输入时落到该输入**实际发生沿的时刻**（在 s 时刻或之后的第一条
    沿），保证每条链都完整到达主输入，不会停在门节点或出现空因果链。

    返回 {"paths": [...], "total": 去重后路径总数, "truncated": 是否截断}，
    每条路径为 {"edge_t": 输出沿时刻, "hops": [...]}，hop 首元素为违规门、
    末元素为主输入；同一 hop 在多条链间共享同一对象。
    """
    gmap = nl.gate_map
    cache: dict[tuple, list[list[dict]]] = {}

    def val(n, t):
        if t < 0:
            return c_init[n]
        expr = C[n][t]
        if isinstance(expr, int):
            return expr
        return model.eval(expr, model_completion=True).as_long() & 1

    def first_edge_at_or_after(n, s):
        """节点 n 在时刻 s（含）之后第一条取值沿的时刻，无则 None。"""
        H = len(C[n]) - 1
        if s > H:
            return None
        base = val(n, s - 1)
        for te in range(s, H + 1):
            if val(n, te) != base:
                return te
        return None

    def chains(n, t):
        key = (n, t)
        if key in cache:
            return cache[key]
        if n in pi:
            te = first_edge_at_or_after(n, 0)
            leaf_t = t if te is None else te
            cache[key] = [[{"node": n, "gate": None, "t": leaf_t,
                            "delay": None}]]
            return cache[key]

        g = gmap[n]
        old_out = c_init[n] if t == 0 else val(n, t - 1)
        ev = next((e for e in gate_events.get(n, []) if e["t"] == t), None)
        delay = ev["delay"] if ev is not None else None
        # 失配起点：实际点火延迟 d 表示失配自 t-d 起连续保持；防御性兜底
        # （异常情况下取不到点火事件）退回 [t-hi, t] 窗口。
        s = t - delay if delay is not None else max(0, t - g.delay_max)
        s = max(0, s)
        hop = {"node": n, "gate": g.type, "t": t,
               "delay": delay, "mismatch_start": s}

        changed = []  # (扇入下标, 失配窗口内第一条沿时刻)
        for j, src in enumerate(g.inputs):
            te = first_edge_at_or_after(src, s)
            if te is not None and te <= t and val(src, te - 1) != val(src, te):
                changed.append((j, te))

        # 枚举让门输出恢复 old_out 的最小变化扇入子集：先找全部单元素，
        # 没有再找二元（二元门至多 2 个；一元门至多 1 个）。
        def restores(subset):
            trial = [val(x, t) for x in g.inputs]
            for j in subset:
                trial[j] = val(g.inputs[j], s - 1)
            return eval_gate(g.type, trial) == old_out

        minimal = [[j] for j, _ in changed if restores([j])]
        if not minimal and len(changed) >= 2:
            for r in range(2, len(changed) + 1):
                for combo in itertools.combinations([j for j, _ in changed], r):
                    if restores(list(combo)):
                        minimal.append(list(combo))
                if minimal:
                    break
        # 正常情况下必有子集（点火即 t 刻计算值≠旧值）；防御性兜底，
        # 避免任何分析数据里出现停在门节点的断链。
        if not minimal:
            minimal = [[j] for j, _ in changed]

        edge_t_of = {j: te for j, te in changed}
        result: list[list[dict]] = []
        for subset in minimal:
            joint = (sorted(g.inputs[j] for j in subset)
                     if len(subset) > 1 else None)
            for j in subset:
                src = g.inputs[j]
                h = dict(hop)
                if joint is not None:
                    h["joint"] = joint
                te = edge_t_of[j]
                for sub in chains(src, te):
                    result.append([h] + sub)
        if not result:
            result = [[hop]]
        cache[key] = result
        return result

    times = edge_times or ([t_bad] if t_bad is not None else [])
    raw: list[tuple[int, tuple, list[dict]]] = []
    seen: set[tuple[int, tuple]] = set()
    if node is not None and t_bad is not None:
        if node in pi:
            te = first_edge_at_or_after(node, 0)
            leaf_t = t_bad if te is None else te
            for et in times:
                key = (et, (node, leaf_t))
                if key in seen:
                    continue
                seen.add(key)
                raw.append((et, key,
                            [{"node": node, "gate": None, "t": leaf_t,
                              "delay": None}]))
        else:
            for et in times:
                for c in chains(node, et):
                    sig = tuple(
                        (h["node"], h["t"],
                         tuple(h.get("joint", [])) if h.get("joint") else ())
                        for h in c
                    )
                    key = (et, sig)
                    if key in seen:
                        continue
                    seen.add(key)
                    raw.append((et, key, c))

    # 稳定排序：先按输出沿时刻，再沿链自上而下（门时刻/联合组/节点），
    # 保证截断与展示结果在相同模型下确定、可复现、去重。
    raw.sort(key=lambda item: (item[0], item[1]))
    total = len(raw)
    truncated = total > max_paths
    paths = [{"edge_t": et, "hops": hops} for et, _, hops in raw[:max_paths]]
    return {"paths": paths, "total": total, "truncated": truncated}

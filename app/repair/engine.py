"""反例驱动修复搜索的 z3 约束优化核心。

搜索空间（全部为整数）
----------------------
* 每个被用户列入可调的门 g：lo_g、hi_g ∈ 用户给定范围，0<=lo<=hi；
* 每个被列入可调的输入 i：窗口端点 wmin_i、wmax_i ∈ 给定范围，wmin<=wmax；
* 每个被列入可调的输出约束：min_pulse_width / max_transitions ∈ 给定范围。

一个“改动（change）”是**一个被修改的实体端点组**：门延迟对算 1 项、
一个输入窗口算 1 项、输出的一个约束字段算 1 项。总偏差 deviation 是各端点
与原值绝对差之和。

搜索过程
--------
1. 用 z3verifier._encode 的 dyn 模式把上述量做成符号，叠加
   “所有时间都不违规”安全谓词 ¬∨vbad；
2. 令改动数 k 从 0 递增，依次求可行安全赋值（SAT），并阻塞全赋值枚举同 k
   的全部解；每枚举出一个候选就用既有验证器 verify() 独立复核；
3. 复核为 safe 的候选进入 Pareto 前沿（按改动数、总偏差、最早风险消除时间
   三维互不支配），按 (改动数, 总偏差, 最早风险消除时间) 字典序取前 10 条。

“最早风险消除时间”取复核 safe 结论中的完全稳定界 settling_bound：
最大跃迁窗口右端 + 最长静态路径，越小代表越早必然无风险。
"""

from __future__ import annotations

import os
import time

import z3

from ..models import Netlist, NetlistError, parse_netlist
from ..params import AnalysisParams, parse_params
from ..verifier import z3verifier

# 搜索规模/时间预算（环境变量可覆盖）
REPAIR_MAX_GATES = int(os.getenv("HAZARD_REPAIR_MAX_GATES", "60"))
REPAIR_MAX_HORIZON = int(os.getenv("HAZARD_REPAIR_MAX_HORIZON", "250"))
REPAIR_SOLVER_TIMEOUT_MS = int(os.getenv("HAZARD_REPAIR_SOLVER_TIMEOUT_MS", "20000"))
REPAIR_MAX_MODELS = int(os.getenv("HAZARD_REPAIR_MAX_MODELS", "60"))
REPAIR_MAX_BAD_KEYS = int(os.getenv("HAZARD_REPAIR_MAX_BAD_KEYS", "200"))
REPAIR_MAX_SAFE_PER_LEVEL = int(
    os.getenv("HAZARD_REPAIR_MAX_SAFE_PER_LEVEL", "30"))
REPAIR_MAX_VERIFICATIONS = int(os.getenv("HAZARD_REPAIR_MAX_VERIFICATIONS", "30"))
MAX_AUDIT_ENTRIES = 200

MAX_DELAY_VALUE = 10_000
MAX_WINDOW_T = 100_000
MAX_PW = 100_000
MAX_K = 100_000


class RepairError(ValueError):
    """修复搜索请求非法。"""


class RepairScaleLimit(Exception):
    """搜索规模超过专门上限。"""


# ---------------- 请求解析 ----------------

def _as_int(v, what: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise RepairError(f"{what} 必须是整数")
    return v


def _pair(raw, what: str) -> tuple[int, int]:
    if (not isinstance(raw, list) or len(raw) != 2
            or any(isinstance(x, bool) or not isinstance(x, int) for x in raw)):
        raise RepairError(f"{what} 必须是 [min,max] 两个整数")
    return raw[0], raw[1]


def _as_gate_specs(raw, what: str) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise RepairError(f"{what} 必须是数组")
    out = {}
    for item in raw:
        if not isinstance(item, dict) or "id" not in item:
            raise RepairError(f"{what} 条目需含门 id 与 range")
        gid = str(item["id"])
        lo0, hi0 = _pair(item.get("range"), f"门 {gid} 可调范围")
        out[gid] = {"lo": lo0, "hi": hi0}
    return out


def _as_window_specs(raw, what: str) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise RepairError(f"{what} 必须是数组")
    out = {}
    for item in raw:
        if not isinstance(item, dict) or "name" not in item:
            raise RepairError(f"{what} 条目需含输入 name 与 range")
        name = str(item["name"])
        t0, t1 = _pair(item.get("range"), f"输入 {name} 窗口可调范围")
        out[name] = {"tmin": t0, "tmax": t1}
    return out


def _as_output_specs(raw, what: str) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise RepairError(f"{what} 必须是数组")
    out = {}
    for item in raw:
        if not isinstance(item, dict) or "name" not in item:
            raise RepairError(f"{what} 条目需含输出 name")
        name = str(item["name"])
        spec = {}
        if item.get("min_pulse_width_range") is not None:
            a, b = _pair(item["min_pulse_width_range"],
                         f"输出 {name} min_pulse_width 可调范围")
            spec["pw"] = [a, b]
        if item.get("max_transitions_range") is not None:
            a, b = _pair(item["max_transitions_range"],
                         f"输出 {name} max_transitions 可调范围")
            spec["k"] = [a, b]
        if spec:
            out[name] = spec
    return out


def parse_search_request(req: dict) -> dict:
    if not isinstance(req, dict):
        raise RepairError("请求体必须是 JSON 对象")
    gates = _as_gate_specs(req.get("gates"), "gates")
    windows = _as_window_specs(req.get("windows"), "windows")
    outputs = _as_output_specs(req.get("outputs"), "outputs")

    budget = req.get("budget", {})
    if not isinstance(budget, dict):
        raise RepairError("budget 必须是对象")
    max_changes = _as_int(budget.get("max_changes", 0), "budget.max_changes")
    max_deviation = _as_int(
        budget.get("max_total_deviation", 0), "budget.max_total_deviation")
    if max_changes < 0 or max_deviation < 0:
        raise RepairError("预算必须是非负整数")

    if not (gates or windows or outputs):
        raise RepairError(
            "至少需要在 gates/windows/outputs 中指定一个可调范围")
    return {"gates": gates, "windows": windows, "outputs": outputs,
            "max_changes": max_changes, "max_deviation": max_deviation}


# ---------------- 可调项与原值核对 ----------------

def _baseline(nl: Netlist, params: AnalysisParams):
    """汇总各可调实体的原始端点值（存在则给 [lo,hi] / [tmin,tmax] / 字段值）。"""
    base_gates = {g.id: [g.delay_min, g.delay_max] for g in nl.gates}
    win_by_name = {w.name: w for w in params.windows}
    base_windows = {n: [w.tmin, w.tmax] for n, w in win_by_name.items()}
    base_pw, base_k = {}, {}
    for s in params.outputs:
        if s.min_pulse_width is not None:
            base_pw[s.name] = s.min_pulse_width
        if s.max_transitions is not None:
            base_k[s.name] = s.max_transitions
    return base_gates, base_windows, base_pw, base_k, win_by_name


def build_space(nl: Netlist, params: AnalysisParams, spec: dict) -> dict:
    """把用户可调范围与网表/参数核对，生成符号变量定义与统计信息。"""
    base_gates, base_windows, base_pw, base_k, win_by_name = _baseline(
        nl, params)
    H = params.horizon

    gates, windows, outputs = [], [], []
    for gid, rng in spec["gates"].items():
        if gid not in base_gates:
            raise RepairError(f"可调门 {gid} 不在网表中")
        lo0, hi0 = base_gates[gid]
        a, b = rng["lo"], rng["hi"]
        if not (0 <= a <= b) or b > MAX_DELAY_VALUE:
            raise RepairError(
                f"门 {gid} 延迟范围需满足 0<=min<=max<={MAX_DELAY_VALUE}")
        if a > hi0 or b < lo0:
            raise RepairError(
                f"门 {gid} 可调范围 [{a},{b}] 不包含原值 [{lo0},{hi0}]")
        gates.append({"id": gid, "range": [a, b], "base": [lo0, hi0]})

    switched = {w.name for w in params.windows if w.is_switched}
    for name, rng in spec["windows"].items():
        if name not in base_windows:
            raise RepairError(
                f"可调窗口输入 {name} 在原记录中没有跃迁窗口，不能新增窗口"
                "（只能调整已发生跃迁输入的窗口）")
        t0, t1 = base_windows[name]
        a, b = rng["tmin"], rng["tmax"]
        if not (0 <= a <= b) or a > H or b > H:
            raise RepairError(
                f"输入 {name} 窗口范围需满足 0<=tmin<=tmax<=H({H})")
        if a > t1 or b < t0:
            raise RepairError(
                f"输入 {name} 窗口可调范围 [{a},{b}] 不包含原窗口 [{t0},{t1}]")
        if name not in switched:
            raise RepairError(
                f"输入 {name} 初态=终态，原记录中未发生跃迁，不能调整其窗口")
        windows.append({"name": name, "range": [a, b], "base": [t0, t1]})

    out_by_name = {s.name: s for s in params.outputs}
    for name, fields in spec["outputs"].items():
        if name not in out_by_name:
            raise RepairError(f"可调输出 {name} 不在原约束输出中")
        entry = {"name": name, "pw": None, "k": None}
        if "pw" in fields:
            if name not in base_pw:
                raise RepairError(f"输出 {name} 原本没有 min_pulse_width 约束")
            a, b = fields["pw"]
            if not (1 <= a <= b) or b > MAX_PW:
                raise RepairError(
                    f"输出 {name} min_pulse_width 范围需满足 1<=min<=max")
            if a > base_pw[name] or b < base_pw[name]:
                raise RepairError(
                    f"输出 {name} 脉宽范围 [{a},{b}] 不包含原值 {base_pw[name]}")
            entry["pw"] = {"range": [a, b], "base": base_pw[name]}
        if "k" in fields:
            if name not in base_k:
                raise RepairError(f"输出 {name} 原本没有 max_transitions 约束")
            a, b = fields["k"]
            if not (0 <= a <= b) or b > MAX_K:
                raise RepairError(
                    f"输出 {name} max_transitions 范围需满足 0<=min<=max")
            if a > base_k[name] or b < base_k[name]:
                raise RepairError(
                    f"输出 {name} 跃迁次数范围 [{a},{b}] 不包含原值 {base_k[name]}")
            entry["k"] = {"range": [a, b], "base": base_k[name]}
        outputs.append(entry)

    n_units = (len(gates) + len(windows)
               + sum(len([f for f in ("pw", "k") if e[f] is not None])
                     for e in outputs))
    return {"gates": gates, "windows": windows, "outputs": outputs,
            "n_units": n_units}


# ---------------- 搜索 ----------------

def search(nl: Netlist, params: AnalysisParams, spec: dict,
           source_race_paths: list[dict] | None = None,
           bad_output: str | None = None, audit: list | None = None) -> dict:
    """执行修复搜索，返回结构化结果（safe_plans / no_solution / unknown）。"""
    space = build_space(nl, params, spec)
    _check_scale(nl, params, space)
    audit = audit if audit is not None else []
    source_race_paths = source_race_paths or []

    def log(entry):
        if len(audit) < MAX_AUDIT_ENTRIES:
            audit.append(entry)

    hi_cap = max((g["range"][1] for g in space["gates"]), default=0)

    lo_v, hi_v = {}, {}
    win_v = {}
    pw_v, k_v = {}, {}

    dyn = {"lo": lo_v, "hi": hi_v, "win": win_v, "pw": pw_v, "k": k_v,
           "hi_cap": hi_cap}
    domain = []

    # ---- 符号与域约束 ----
    for g in space["gates"]:
        gid = g["id"]
        a, b = g["range"]
        lo = z3.Int(f"fix_lo_{gid}")
        hi = z3.Int(f"fix_hi_{gid}")
        lo_v[gid], hi_v[gid] = lo, hi
        domain += [lo >= a, lo <= b, hi >= a, hi <= b, lo <= hi]

    for w in space["windows"]:
        name = w["name"]
        a, b = w["range"]
        t0 = z3.Int(f"fix_wmin_{name}")
        t1 = z3.Int(f"fix_wmax_{name}")
        win_v[name] = (t0, t1)
        domain += [t0 >= a, t0 <= b, t1 >= a, t1 <= b, t0 <= t1]

    for e in space["outputs"]:
        name = e["name"]
        if e["pw"] is not None:
            x = z3.Int(f"fix_pw_{name}")
            a, b = e["pw"]["range"]
            pw_v[name] = x
            domain += [x >= a, x <= b]
        if e["k"] is not None:
            x = z3.Int(f"fix_k_{name}")
            a, b = e["k"]["range"]
            k_v[name] = x
            domain += [x >= a, x <= b]

    ctx = z3verifier._encode(nl, params, dyn)

    # ---- 改动指示量 / 偏差项（按端点组）。必须在组装 base 之前全部加入
    # domain，否则 n_changes 对求解器无约束、分层失效。 ----
    change_terms, dev_terms, unit_vars = [], [], []

    def changed(expr_pair, base_pair):
        (x, y) = expr_pair
        (x0, y0) = base_pair
        chg = z3.Or(x != x0, y != y0)
        b = z3.Int(f"chg_{len(unit_vars)}")
        unit_vars.append(b)
        domain.append(b == z3.If(chg, 1, 0))
        # 偏差：有改动时计两端点绝对差（z3 Abs）
        dev_terms.append(z3.If(chg, z3.Abs(x - x0) + z3.Abs(y - y0), 0))
        change_terms.append(b)

    for g in space["gates"]:
        gid = g["id"]
        changed((lo_v[gid], hi_v[gid]),
                (g["base"][0], g["base"][1]))
    for w in space["windows"]:
        name = w["name"]
        changed(win_v[name], (w["base"][0], w["base"][1]))
    for e in space["outputs"]:
        name = e["name"]
        if e["pw"] is not None:
            x = pw_v[name]
            x0 = e["pw"]["base"]
            b = z3.Int(f"chg_{len(unit_vars)}")
            unit_vars.append(b)
            domain.append(b == z3.If(x != x0, 1, 0))
            dev_terms.append(z3.Abs(x - x0))
            change_terms.append(b)
        if e["k"] is not None:
            x = k_v[name]
            x0 = e["k"]["base"]
            b = z3.Int(f"chg_{len(unit_vars)}")
            unit_vars.append(b)
            domain.append(b == z3.If(x != x0, 1, 0))
            dev_terms.append(z3.Abs(x - x0))
            change_terms.append(b)

    n_changes = z3.Sum(change_terms) if change_terms else z3.IntVal(0)
    deviation = z3.Sum(dev_terms) if dev_terms else z3.IntVal(0)
    base = list(ctx["base"]) + domain

    # 所有“赋值变量”：用于阻塞枚举。只包含真正可调的变量——固定端点的门/窗口
    # 不进入元组，否则阻塞条件可被无关固定变量无限次重新满足。
    # 顺序：每门 (lo,hi) → 每窗口 (tmin,tmax) → 输出 pw/k，
    # 与 _assignment_from_key 严格对应。
    assign_vars = []
    for g in space["gates"]:
        gid = g["id"]
        assign_vars.append((lo_v[gid], f"lo_{gid}"))
        assign_vars.append((hi_v[gid], f"hi_{gid}"))
    for w in space["windows"]:
        assign_vars.append((win_v[w["name"]][0], f"w0_{w['name']}"))
        assign_vars.append((win_v[w["name"]][1], f"w1_{w['name']}"))
    for e in space["outputs"]:
        if e["pw"] is not None:
            assign_vars.append((pw_v[e["name"]], f"pw_{e['name']}"))
        if e["k"] is not None:
            assign_vars.append((k_v[e["name"]], f"k_{e['name']}"))

    max_k = min(spec["max_changes"], space["n_units"])
    max_dev = spec["max_deviation"]

    # 安全性是**对所有输入 τ 与门点火选择 fr 都不违规**：与 verify 阶段一相同，
    # 用“违规存在性不可满足（unsat）”判定。枚举阶段先由 z3 批量证明
    # 安全候选（不逐个调用验证器），最后仅对 Pareto 前沿与最小改动见证
    # 用既有 verify() 独立复核。
    violation = z3.Or(ctx["vbad"])

    # ---- 逐层枚举 ----
    candidates: list[dict] = []
    unknown_candidates = 0
    unknown_reasons: dict[str, int] = {}
    unsafe_recheck = 0
    verifications = 0
    timed_out = False
    levels_exhausted: list[int] = []
    level_status: dict[int, str] = {}
    first_feasible_k = None
    min_deviation_at_first = None
    assignment_keys: set[tuple] = set()

    def metrics_of(assignment: dict) -> tuple[int, int]:
        """直接从具体赋值计算改动数与总偏差（不依赖模型对指示量的补全）。"""
        changes, dev = 0, 0
        for g in space["gates"]:
            v = assignment["gates"][g["id"]]
            if v != g["base"]:
                changes += 1
                dev += abs(v[0] - g["base"][0]) + abs(v[1] - g["base"][1])
        for w in space["windows"]:
            v = assignment["windows"][w["name"]]
            if v != w["base"]:
                changes += 1
                dev += abs(v[0] - w["base"][0]) + abs(v[1] - w["base"][1])
        for e in space["outputs"]:
            name = e["name"]
            vals = assignment["outputs"].get(name, {})
            if e["pw"] is not None and vals["pw"] != e["pw"]["base"]:
                changes += 1
                dev += abs(vals["pw"] - e["pw"]["base"])
            if e["k"] is not None and vals["k"] != e["k"]["base"]:
                changes += 1
                dev += abs(vals["k"] - e["k"]["base"])
        return changes, dev

    def key_from_model(model) -> tuple:
        # 决策变量有有限域，sat 模型中必取具体整数（model_completion 对未决
        # 变量也给域内值；并由调用方再做 k/预算一致性校验）。
        vals = []
        for v, _ in assign_vars:
            vals.append(model.eval(v, model_completion=True).as_long())
        return tuple(vals)

    def block_key(key):
        return z3.Or([v != val for (v, _), val in zip(assign_vars, key)])

    def pin_assignment(key):
        return [v == val for (v, _), val in zip(assign_vars, key)]

    t_start = time.monotonic()
    global_stop = False
    # k=0 是唯一的“零改动”赋值（即基线本身），源记录已确认为 unsafe，
    # 直接记为不可行，避免枚举大量等价空改动候选浪费复核预算。
    level_status[0] = "unsat"
    levels_exhausted.append(0)
    log({"step": "level_feasibility", "k": 0, "result": "unsat",
         "note": "baseline_record_unsafe"})

    for k in range(1, max_k + 1):
        if verifications >= REPAIR_MAX_VERIFICATIONS:
            log({"step": "search_stop", "reason": "verification_budget",
                 "at_k": k})
            global_stop = True
            break
        level_constraints = [n_changes == k, deviation <= max_dev]
        # 主求解器：断言“存在违规轨迹”。返回的每个键都是仍不安全的
        # 赋值，逐条剔除；unsat 时本层已无不安全键（剩余全安全）。
        # 达到坏键预算则截断，剩余键改用钉死判定。
        main = z3.Solver()
        main.set("timeout", REPAIR_SOLVER_TIMEOUT_MS)
        main.add(base)
        main.add(level_constraints)
        main.add(violation)
        # 钉死键用的求解器（复用同一份编码 AST，仅 push/pop 附加约束）
        pin = z3.Solver()
        pin.set("timeout", REPAIR_SOLVER_TIMEOUT_MS)
        pin.add(base)
        pin.add(level_constraints)
        # 剩余候选键枚举器（不预置 violation）
        good = z3.Solver()
        good.set("timeout", REPAIR_SOLVER_TIMEOUT_MS)
        good.add(base)
        good.add(level_constraints)

        def is_unsafe(key):
            pin.push()
            pin.add(pin_assignment(key))
            pin.add(violation)
            rr = pin.check()
            pin.pop()
            return rr

        level_cut = False
        bad_keys: set[tuple] = set()
        bad_capped = False

        # 阶段 A：批量剔除不安全键（轻量求解，不调用验证器）
        while len(bad_keys) < REPAIR_MAX_BAD_KEYS:
            r = main.check()
            if r == z3.unknown:
                level_cut, timed_out = True, True
                log({"step": "bad_enumerate", "k": k, "result": "unknown"})
                break
            if r == z3.unsat:
                break
            key = key_from_model(main.model())
            bad_keys.add(key)
            main.add(block_key(key))
        else:
            bad_capped = True
            log({"step": "bad_enumerate", "k": k, "result": "capped",
                 "bad_keys": len(bad_keys)})
        unsafe_recheck += len(bad_keys)
        if level_cut:
            level_status[k] = "unknown"
            global_stop = True
            break

        for bk in bad_keys:
            good.add(block_key(bk))

        # 阶段 B：枚举剩余键，钉死后区分安全 / 不安全
        safe_collected_this_level = 0
        while True:
            if safe_collected_this_level >= REPAIR_MAX_SAFE_PER_LEVEL:
                level_cut = True
                log({"step": "search_stop", "reason": "per_level_model_budget",
                     "at_k": k, "safe_candidates": len(candidates)})
                break
            r = good.check()
            if r == z3.unknown:
                level_cut, timed_out = True, True
                log({"step": "good_enumerate", "k": k, "result": "unknown"})
                break
            if r == z3.unsat:
                break
            key = key_from_model(good.model())
            good.add(block_key(key))
            if key in assignment_keys or key in bad_keys:
                continue
            # 阶段 A 已穷尽“存在违规”的键（main unsat）时，剩余键必然
            # 对所有 τ/点火都安全，无需再钉死；只有截断时才需逐个确认。
            if bad_capped:
                pr = is_unsafe(key)
                if pr == z3.unknown:
                    level_cut, timed_out = True, True
                    log({"step": "candidate_feasibility", "k": k,
                         "result": "unknown"})
                    break
                if pr == z3.sat:
                    bad_keys.add(key)
                    unsafe_recheck += 1
                    continue
            assignment = _assignment_from_key(space, key)
            k_val, d_val = metrics_of(assignment)
            if k_val != k or d_val > max_dev:
                continue
            assignment_keys.add(key)
            if first_feasible_k is None:
                first_feasible_k = k
            if min_deviation_at_first is None or \
                    d_val < min_deviation_at_first:
                min_deviation_at_first = d_val
            cand = _build_candidate(nl, params, space, assignment, k_val, d_val)
            cand["risk_eliminated_at"] = _settling_bound(cand)
            candidates.append(cand)
            safe_collected_this_level += 1
            log({"step": "z3_safe_candidate", "k": k_val,
                 "deviation": d_val,
                 "assignment": _compact_assignment(space, assignment)})

        if level_cut:
            level_status[k] = "unknown" if timed_out else "cut"
            global_stop = True
            break

        if first_feasible_k == k:
            level_status[k] = "exhausted"
        else:
            level_status[k] = "unsat"
        levels_exhausted.append(k)

    elapsed_ms = round((time.monotonic() - t_start) * 1000, 1)

    # ---- Pareto 前沿（z3 已证安全），再用既有验证器独立复核 ----
    z3_safe_count = len(candidates)
    for cand in candidates:
        cand["_source_paths"] = [
            dict(p, _bad_output=bad_output) for p in source_race_paths]
    front = _pareto(candidates)
    front.sort(key=lambda p: (p["changes"], p["total_deviation"], p["key"]))
    # 最小改动见证（min_changes 下偏差最小）必须复核，作为“预算内最小改动”
    # 的直接证据；Pareto 候选按最终排序至多复核 REPAIR_MAX_VERIFICATIONS 个。
    witness_key = None
    if first_feasible_k is not None:
        same = [c for c in candidates if c["changes"] == first_feasible_k]
        if same:
            witness_key = min(same, key=lambda c: c["total_deviation"])["key"]
    to_verify: list[dict] = []
    for c in front:
        if c["key"] == witness_key and c not in to_verify:
            to_verify.append(c)
    for c in front:
        if c not in to_verify and len(to_verify) < REPAIR_MAX_VERIFICATIONS:
            to_verify.append(c)
    verified_plans = []
    unknown_candidates = 0
    for cand in to_verify:
        verifications += 1
        t0 = time.monotonic()
        info = _recheck(cand)
        cand["recheck_ms"] = round((time.monotonic() - t0) * 1000, 1)
        log({"step": "verifier_recheck", "k": cand["changes"],
             "deviation": cand["total_deviation"],
             "verified": info["status"], "elapsed_ms": cand["recheck_ms"]})
        if info["status"] == "safe":
            cand["coverage"] = info["coverage"]
            cand["risk_eliminated_at"] = info["coverage"]["settling_bound"]
            verified_plans.append(cand)
        elif info["status"] == "unknown":
            unknown_candidates += 1
            reason = info.get("reason", "unknown")
            unknown_reasons[reason] = unknown_reasons.get(reason, 0) + 1
        else:
            unsafe_recheck += 1
    verified_plans.sort(key=lambda p: (p["changes"], p["total_deviation"],
                                     p["risk_eliminated_at"], p["key"]))
    plans = verified_plans[:10]
    _annotate_race_paths(nl, params, space, plans)

    proof_complete = (
        first_feasible_k is not None
        and first_feasible_k > 0
        and all(level_status.get(j) == "unsat"
                for j in range(0, first_feasible_k))
    )
    if first_feasible_k is not None and first_feasible_k > 0:
        evidence = {
            "min_changes": first_feasible_k,
            "lower_change_levels_unsat":
                [j for j in range(0, first_feasible_k)
                 if level_status.get(j) == "unsat"],
            "min_total_deviation_at_min_changes": min_deviation_at_first,
            "budget_max_changes": spec["max_changes"],
            "budget_max_total_deviation": spec["max_deviation"],
            "proof": "complete" if proof_complete else "partial",
            "note": (
                "已用 z3 严格证明：改动数小于该值的所有域内赋值都无法消除违规"
                if proof_complete else
                "部分低层受求解预算影响未能严格穷尽，最小改动证据不完整"),
        }
    else:
        evidence = None

    if plans:
        status = "safe_plans"
        reason = None
    elif timed_out:
        status, reason = "unknown", "solver_timeout"
    elif unknown_candidates:
        status, reason = "unknown", "verifier_unknown"
    elif global_stop:
        status, reason = "unknown", "search_budget"
    elif first_feasible_k is not None:
        # z3 证明安全的候选复核均被验证器判为 unsafe（两者分歧）
        status, reason = "unknown", "recheck_mismatch"
    else:
        status, reason = "no_solution", "budget_exceeded"

    return {
        "status": status,
        "reason": reason,
        "plans": plans,
        "min_change_evidence": evidence,
        "unknown_reasons": unknown_reasons,
        "uncovered": _uncovered_summary(plans, params.horizon),
        "statistics": {
            "adjustable_units": space["n_units"],
            "z3_safe_candidates": z3_safe_count,
            "verified_plans": len(verified_plans),
            "verifier_calls": verifications,
            "unsafe_assignments": unsafe_recheck,
            "unknown_candidates": unknown_candidates,
            "levels_exhausted": levels_exhausted,
            "level_status": {str(kk): vv for kk, vv in level_status.items()},
            "timed_out": timed_out,
            "elapsed_ms": elapsed_ms,
            "z3_version": z3.get_version_string(),
        },
        "limits": {
            "max_gates": REPAIR_MAX_GATES,
            "max_horizon": REPAIR_MAX_HORIZON,
            "solver_timeout_ms": REPAIR_SOLVER_TIMEOUT_MS,
            "max_models": REPAIR_MAX_MODELS,
            "max_verifications": REPAIR_MAX_VERIFICATIONS,
        },
        "horizon": params.horizon,
    }


def _check_scale(nl: Netlist, params: AnalysisParams, space: dict) -> None:
    problems = []
    if len(nl.gates) > REPAIR_MAX_GATES:
        problems.append(f"门数 {len(nl.gates)} > {REPAIR_MAX_GATES}")
    if params.horizon > REPAIR_MAX_HORIZON:
        problems.append(f"H={params.horizon} > {REPAIR_MAX_HORIZON}")
    cells = (len(nl.inputs) + len(nl.gates)) * (params.horizon + 1)
    if cells > 40000:
        problems.append(f"节点×时间规模 {cells} > 40000")
    if problems:
        raise RepairScaleLimit("; ".join(problems))


def _assignment_from_key(space: dict, key: tuple) -> dict:
    """从阻塞元组（与 assign_vars 同序：所有 lo、所有 hi、窗口、输出）
    还原候选端点赋值。"""
    idx = 0
    gvals = {}
    for g in space["gates"]:
        gvals[g["id"]] = [key[idx], key[idx + 1]]
        idx += 2
    wvals = {}
    for w in space["windows"]:
        wvals[w["name"]] = [key[idx], key[idx + 1]]
        idx += 2
    ovals = {}
    for e in space["outputs"]:
        if e["pw"] is not None:
            ovals.setdefault(e["name"], {})["pw"] = key[idx]
            idx += 1
        if e["k"] is not None:
            ovals.setdefault(e["name"], {})["k"] = key[idx]
            idx += 1
    return {"gates": gvals, "windows": wvals, "outputs": ovals}


def _compact_assignment(space: dict, assignment: dict) -> dict:
    """审计日志里只记录非默认端点，控制日志体积。"""
    out = {"gates": {}, "windows": {}, "outputs": {}}
    for g in space["gates"]:
        v = assignment["gates"][g["id"]]
        if v != g["base"]:
            out["gates"][g["id"]] = v
    for w in space["windows"]:
        v = assignment["windows"][w["name"]]
        if v != w["base"]:
            out["windows"][w["name"]] = v
    for e in space["outputs"]:
        vals = assignment["outputs"].get(e["name"], {})
        changed_fields = {}
        if e["pw"] is not None and vals.get("pw") != e["pw"]["base"]:
            changed_fields["pw"] = vals["pw"]
        if e["k"] is not None and vals.get("k") != e["k"]["base"]:
            changed_fields["k"] = vals["k"]
        if changed_fields:
            out["outputs"][e["name"]] = changed_fields
    return out


def _build_candidate(nl: Netlist, params: AnalysisParams, space: dict,
                     assignment: dict, k_val: int, d_val: int) -> dict:
    """由符号赋值构造候选网表/参数对象（用于复核与预览）。"""
    cand_nl = {
        "inputs": list(nl.inputs),
        "gates": [],
        "outputs": list(nl.outputs),
    }
    if getattr(nl, "name", None):
        cand_nl["name"] = nl.name
    delays = assignment["gates"]
    for g in nl.gates:
        item = {"id": g.id, "type": g.type, "inputs": list(g.inputs),
                "delay": delays.get(g.id, [g.delay_min, g.delay_max])}
        cand_nl["gates"].append(item)

    wins = assignment["windows"]
    windows_obj = {}
    for w in params.windows:
        if w.name in wins:
            windows_obj[w.name] = list(wins[w.name])
        else:
            windows_obj[w.name] = [w.tmin, w.tmax]

    out_vals = assignment["outputs"]
    outputs_obj = []
    for s in params.outputs:
        spec = {"name": s.name}
        vals = out_vals.get(s.name, {})
        if s.min_pulse_width is not None:
            spec["min_pulse_width"] = vals.get("pw", s.min_pulse_width)
        if s.max_transitions is not None:
            spec["max_transitions"] = vals.get("k", s.max_transitions)
        outputs_obj.append(spec)

    cand_params = {
        "initial": dict(params.initial),
        "final": dict(params.final),
        "windows": windows_obj,
        "outputs": outputs_obj,
        "horizon": params.horizon,
    }

    changes = {"gates": [], "windows": [], "outputs": []}
    for g in space["gates"]:
        v = delays[g["id"]]
        if v != g["base"]:
            changes["gates"].append(
                {"id": g["id"], "from": g["base"], "to": v})
    for w in space["windows"]:
        v = wins[w["name"]]
        if v != w["base"]:
            changes["windows"].append(
                {"name": w["name"], "from": w["base"], "to": v})
    for e in space["outputs"]:
        name = e["name"]
        vals = out_vals.get(name, {})
        if e["pw"] is not None and vals.get("pw") != e["pw"]["base"]:
            changes["outputs"].append(
                {"name": name, "field": "min_pulse_width",
                 "from": e["pw"]["base"], "to": vals["pw"]})
        if e["k"] is not None and vals.get("k") != e["k"]["base"]:
            changes["outputs"].append(
                {"name": name, "field": "max_transitions",
                 "from": e["k"]["base"], "to": vals["k"]})

    key = (tuple((gid, tuple(delays[gid])) for gid in sorted(delays)),
           tuple((n, tuple(wins[n])) for n in sorted(wins)),
           tuple((n, out_vals.get(n, {}).get("pw"),
                  out_vals.get(n, {}).get("k")) for n in sorted(out_vals)))

    return {
        "key": repr(key),
        "netlist": cand_nl,
        "params": cand_params,
        "changes": k_val,
        "total_deviation": d_val,
        "change_detail": changes,
    }


def _recheck(cand: dict) -> dict:
    """用既有验证器（独立 parse + z3 流水线）复核候选。"""
    try:
        nl2 = parse_netlist(cand["netlist"])
        p2 = parse_params(nl2, cand["params"])
    except NetlistError as e:
        return {"status": "unknown", "reason": "invalid_candidate",
                "message": str(e)}
    try:
        result = z3verifier.verify(nl2, p2)
    except z3verifier.ScaleLimit as e:
        return {"status": "unknown", "reason": "scale_limit",
                "message": str(e)}
    if result["status"] == "safe":
        return {"status": "safe", "coverage": result.get("coverage", {})}
    if result["status"] == "unknown":
        return {"status": "unknown", "reason": result.get("reason", "timeout"),
                "message": result.get("message", "")}
    return {"status": "unsafe"}


def _settling_bound(cand: dict) -> int:
    """候选的完全稳定界 = 最大窗口右端 + 最长静态路径（取 hi 求和）。

    无需 z3：纯静态公式，与 verify() safe coverage 中 settling_bound 一致。
    """
    net, pobj = cand["netlist"], cand["params"]
    depth = {n: 0 for n in net["inputs"]}
    gmap = {g["id"]: g for g in net["gates"]}
    for g in net["gates"]:
        depth[g["id"]] = max(depth[s] for s in g["inputs"]) + g["delay"][1]
    win_end = max((w[1] for w in pobj.get("windows", {}).values()),
                   default=0)
    return win_end + max(depth.values(), default=0)


def _pareto(cands: list[dict]) -> list[dict]:
    front = []
    for c in cands:
        point = (c["changes"], c["total_deviation"],
                 c["risk_eliminated_at"])
        dominated = any(
            (o["changes"], o["total_deviation"], o["risk_eliminated_at"])
            != point
            and o["changes"] <= point[0]
            and o["total_deviation"] <= point[1]
            and o["risk_eliminated_at"] <= point[2]
            for o in cands)
        if not dominated:
            front.append(c)
    return front


def _annotate_race_paths(nl: Netlist, params: AnalysisParams, space: dict,
                         plans: list[dict]) -> None:
    """把原反例竞争路径逐条标注被方案的哪类改动消除。

    判定为保守的“覆盖归因”：路径链上出现被改门 → 门延迟改动；
    否则叶输入窗口被改 → 窗口改动；否则违规输出约束被放宽 → 输出约束。
    """
    for p in plans:
        detail = p["change_detail"]
        changed_gates = {x["id"] for x in detail["gates"]}
        changed_windows = {x["name"] for x in detail["windows"]}
        relaxed_outputs = {x["name"] for x in detail["outputs"]}
        paths = p.get("_source_paths", [])
        eliminated = []
        for idx, path in enumerate(paths):
            hops = path.get("hops", [])
            gate_hit = next((h["node"] for h in hops
                             if h.get("gate") and h["node"] in changed_gates),
                            None)
            if gate_hit is not None:
                cause = {"kind": "gate_delay", "id": gate_hit}
            else:
                leaves = [h["node"] for h in hops if not h.get("gate")]
                win_hit = next((n for n in leaves if n in changed_windows),
                               None)
                if win_hit is not None:
                    cause = {"kind": "input_window", "id": win_hit}
                elif path.get("_bad_output") in relaxed_outputs:
                    cause = {"kind": "output_constraint",
                             "id": path.get("_bad_output")}
                else:
                    cause = {"kind": "global"}
            eliminated.append({"index": idx, "edge_t": path.get("edge_t"),
                               "cause": cause})
        p["eliminated_race_paths"] = eliminated
        p.pop("_source_paths", None)


def _uncovered_summary(plans: list[dict], horizon: int) -> dict:
    items = []
    for p in plans:
        cov = p.get("coverage") or {}
        if not cov.get("transient_fully_covered", True):
            items.append({
                "plan_key": p["key"],
                "changes": p["changes"],
                "settling_bound": cov.get("settling_bound"),
                "horizon": horizon,
                "note": cov.get("pending_note")
                or f"H={horizon} 小于完全稳定界 {cov.get('settling_bound')}，"
                   "t>H 的行为未覆盖",
            })
    return {"plans_with_pending_after_horizon": len(items), "items": items}

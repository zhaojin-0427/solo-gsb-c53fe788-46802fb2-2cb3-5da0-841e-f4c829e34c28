"""分析参数：初态、终态、输入跃迁窗口与输出约束。

输入跃迁窗口 [tmin, tmax] 表示该输入在离散时刻 t ∈ [tmin, tmax] 中**任选一个时刻**
发生一次 0→1 / 1→0 跃迁；tmin == tmax 表示时刻固定。未列出的输入保持初态不变
（此时初态应与终态相同）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Netlist, NetlistError


@dataclass
class InputWindow:
    name: str
    tmin: int
    tmax: int
    value_before: int
    value_after: int

    @property
    def is_switched(self) -> bool:
        return self.value_before != self.value_after


@dataclass
class OutputSpec:
    name: str
    min_pulse_width: int | None  # 最小可接受脉冲宽度（格子数），None=不检查
    max_transitions: int | None  # 窗口内最多跃迁次数，None=不检查


@dataclass
class AnalysisParams:
    initial: dict[str, int]
    final: dict[str, int]
    windows: list[InputWindow]
    outputs: list[OutputSpec]
    horizon: int  # 检查时间上界（含），H 个半开格子 [t,t+1), t=0..H


def _as_bit(v, what: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v not in (0, 1):
        raise NetlistError(f"{what} 必须是 0 或 1")
    return v


def parse_params(nl: Netlist, obj) -> AnalysisParams:
    if not isinstance(obj, dict):
        raise NetlistError("分析参数必须是 JSON 对象")

    raw_init = obj.get("initial", {})
    raw_final = obj.get("final", {})
    if not isinstance(raw_init, dict) or not isinstance(raw_final, dict):
        raise NetlistError("initial / final 必须是 {输入名: 0|1} 对象")

    initial, final = {}, {}
    for name in nl.inputs:
        initial[name] = _as_bit(raw_init.get(name, 0), f"initial[{name}]")
        final[name] = _as_bit(raw_final.get(name, initial[name]), f"final[{name}]")

    raw_windows = obj.get("windows", {})
    if not isinstance(raw_windows, dict):
        raise NetlistError("windows 必须是 {输入名: [tmin,tmax]} 对象")

    windows: list[InputWindow] = []
    for name in nl.inputs:
        before = initial[name]
        after = final[name]
        if name in raw_windows:
            win = raw_windows[name]
            if (
                not isinstance(win, list)
                or len(win) != 2
                or not all(isinstance(x, int) and not isinstance(x, bool) for x in win)
            ):
                raise NetlistError(f"windows[{name}] 必须是 [tmin,tmax] 整数数组")
            tmin, tmax = win
            if tmin < 0 or tmax < tmin:
                raise NetlistError(f"windows[{name}] 需满足 0 <= tmin <= tmax")
        else:
            if before != after:
                raise NetlistError(
                    f"输入 {name} 的初态与终态不同，必须在 windows 中给出跃迁窗口"
                )
            continue
        windows.append(InputWindow(name, tmin, tmax, before, after))

    # 输出约束：可整体给出对象 / 对象数组；省略约束字段表示该项不检查。
    raw_outs = obj.get("outputs", nl.outputs)
    if isinstance(raw_outs, str):
        raw_outs = [raw_outs]
    if not isinstance(raw_outs, list):
        raise NetlistError("outputs 必须是信号名数组或约束对象数组")
    out_specs: list[OutputSpec] = []
    for item in raw_outs:
        if isinstance(item, str):
            name, pw, k = item, None, None
        elif isinstance(item, dict) and "name" in item:
            name = str(item["name"])
            pw = item.get("min_pulse_width")
            k = item.get("max_transitions")
            if pw is not None:
                if not isinstance(pw, int) or isinstance(pw, bool) or pw < 1:
                    raise NetlistError(f"输出 {name} 的 min_pulse_width 必须是 >=1 整数")
            if k is not None:
                if not isinstance(k, int) or isinstance(k, bool) or k < 0:
                    raise NetlistError(f"输出 {name} 的 max_transitions 必须是 >=0 整数")
        else:
            raise NetlistError("outputs 条目必须是信号名字符串或 {name,...} 约束对象")
        if name not in nl.all_nodes:
            raise NetlistError(f"约束输出 {name} 不在网表中")
        if pw is None and k is None:
            raise NetlistError(
                f"输出 {name} 至少需要一个约束 (min_pulse_width 或 max_transitions)"
            )
        out_specs.append(OutputSpec(name, pw, k))
    if not out_specs:
        raise NetlistError("至少指定一个输出约束")
    if len({s.name for s in out_specs}) != len(out_specs):
        raise NetlistError("输出约束存在重复")

    horizon = obj.get("horizon")
    auto_h = max(
        (w.tmax for w in windows),
        default=0,
    ) + nl.longest_path_delay()
    if horizon is None:
        horizon = auto_h
    elif not isinstance(horizon, int) or isinstance(horizon, bool) or horizon < 1:
        raise NetlistError("horizon 必须是 >=1 的整数")

    return AnalysisParams(
        initial=initial, final=final, windows=windows,
        outputs=out_specs, horizon=horizon,
    )

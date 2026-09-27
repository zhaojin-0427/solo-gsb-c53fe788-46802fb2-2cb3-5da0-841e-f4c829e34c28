"""网表数据模型与校验。

网表是**无环二值门级**网络：节点取值只有 0/1，门之间不允许出现组合环路。

JSON 格式见 README，核心结构：

    {
      "inputs": ["a", "b", ...],
      "gates": [
        {"id": "w1", "type": "and", "inputs": ["a", "b"], "delay": [1, 3]},
        ...
      ],
      "outputs": ["z"]
    }
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 支持的门类型 → (元数, 求值函数)。NOT/BUF 为一元门，其余为二元门。
UNARY_TYPES = {"not", "buf"}
BINARY_TYPES = {"and", "or", "xor", "nand", "nor", "xnor"}
ALL_TYPES = UNARY_TYPES | BINARY_TYPES

DEFAULT_DELAY = (1, 3)
MAX_DELAY_VALUE = 10_000


def eval_gate(gate_type: str, values: list[int]) -> int:
    """二值门求值，values 为按网表顺序排列的输入值（一元门长度 1，二元门长度 2）。"""
    if gate_type == "buf":
        return values[0]
    if gate_type == "not":
        return 1 - values[0]
    a, b = values
    if gate_type == "and":
        return a & b
    if gate_type == "or":
        return a | b
    if gate_type == "xor":
        return a ^ b
    if gate_type == "nand":
        return 1 - (a & b)
    if gate_type == "nor":
        return 1 - (a | b)
    if gate_type == "xnor":
        return 1 - (a ^ b)
    raise ValueError(f"未知门类型: {gate_type}")


class NetlistError(ValueError):
    """网表格式或拓扑错误。"""


@dataclass
class Gate:
    id: str
    type: str
    inputs: list[str]
    delay_min: int = DEFAULT_DELAY[0]
    delay_max: int = DEFAULT_DELAY[1]


@dataclass
class Netlist:
    inputs: list[str]
    gates: list[Gate]
    outputs: list[str]
    # 拓扑排序后的门 id（Kahn 算法在 parse 时生成）。
    order: list[str] = field(default_factory=list)
    _gate_map: dict[str, Gate] = field(default_factory=dict, repr=False)

    @property
    def gate_map(self) -> dict[str, Gate]:
        return self._gate_map

    @property
    def all_nodes(self) -> list[str]:
        return list(self.inputs) + [g.id for g in self.gates]

    def longest_path_delay(self) -> int:
        """任意输入到任意门的最长静态路径（各门取 delay_max 求和）。

        跃迁窗口结束之后，再经过该时间，门输出在最坏情况下必然稳定。
        """
        depth = {name: 0 for name in self.inputs}
        for gid in self.order:
            g = self._gate_map[gid]
            depth[gid] = max(depth[i] for i in g.inputs) + g.delay_max
        return max((depth[n] for n in self.inputs + self.order), default=0)

    def longest_path_to(self, output: str) -> int:
        """到指定输出的最长延迟路径。"""
        depth = {name: 0 for name in self.inputs}
        for gid in self.order:
            g = self._gate_map[gid]
            depth[gid] = max(depth[i] for i in g.inputs) + g.delay_max
        return depth[output]


def _parse_delay(raw) -> tuple[int, int]:
    if raw is None:
        return DEFAULT_DELAY
    if isinstance(raw, bool):
        raise NetlistError("delay 必须是 1~2 个非负整数")
    if isinstance(raw, int):
        lo = hi = raw
    elif isinstance(raw, list) and len(raw) in (1, 2) and all(
        isinstance(x, int) and not isinstance(x, bool) for x in raw
    ):
        lo = raw[0]
        hi = raw[-1]
    else:
        raise NetlistError("delay 必须是整数或 [min,max] 整数数组")
    if lo < 0 or hi < lo:
        raise NetlistError("delay 需满足 0 <= min <= max")
    if hi > MAX_DELAY_VALUE:
        raise NetlistError(f"delay 超过上限 {MAX_DELAY_VALUE}")
    return lo, hi


def parse_netlist(obj) -> Netlist:
    """解析并校验用户提交的 JSON 网表。"""
    if not isinstance(obj, dict):
        raise NetlistError("网表必须是 JSON 对象")

    raw_inputs = obj.get("inputs")
    if not isinstance(raw_inputs, list) or not raw_inputs:
        raise NetlistError("inputs 必须是非空字符串数组")
    inputs = [str(x) for x in raw_inputs]
    if len(set(inputs)) != len(inputs):
        raise NetlistError("inputs 中存在重复信号")

    raw_gates = obj.get("gates")
    if not isinstance(raw_gates, list) or not raw_gates:
        raise NetlistError("gates 必须是非空数组")

    gates: list[Gate] = []
    known: set[str] = set(inputs)
    for i, g in enumerate(raw_gates):
        if not isinstance(g, dict):
            raise NetlistError(f"第 {i + 1} 个门不是对象")
        gid = g.get("id")
        if gid is None:
            raise NetlistError(f"第 {i + 1} 个门缺少 id")
        gid = str(gid)
        if gid in known:
            raise NetlistError(f"信号 id 重复: {gid}")
        gtype = str(g.get("type", "")).lower()
        if gtype not in ALL_TYPES:
            raise NetlistError(
                f"门 {gid} 的类型 {gtype!r} 不支持，允许: {sorted(ALL_TYPES)}"
            )
        ginputs = g.get("inputs")
        arity = 1 if gtype in UNARY_TYPES else 2
        if not isinstance(ginputs, list) or len(ginputs) != arity:
            raise NetlistError(f"门 {gid}({gtype}) 需要恰好 {arity} 个输入")
        ginputs = [str(x) for x in ginputs]
        lo, hi = _parse_delay(g.get("delay"))
        gates.append(Gate(gid, gtype, ginputs, lo, hi))
        known.add(gid)

    # 所有扇入引用必须已声明；Kahn 拓扑排序同时检测环路。
    gate_map = {g.id: g for g in gates}
    indegree: dict[str, int] = {}
    fanout: dict[str, list[str]] = {n: [] for n in inputs}
    for g in gates:
        for src in g.inputs:
            if src not in known:
                raise NetlistError(f"门 {g.id} 引用了未声明的信号 {src}")
            if src in gate_map:
                indegree[g.id] = indegree.get(g.id, 0) + 1
                fanout.setdefault(src, []).append(g.id)
        indegree.setdefault(g.id, indegree.get(g.id, 0))
        fanout.setdefault(g.id, [])

    queue = [g.id for g in gates if indegree[g.id] == 0]
    order: list[str] = []
    while queue:
        n = queue.pop()
        order.append(n)
        for m in fanout.get(n, []):
            indegree[m] -= 1
            if indegree[m] == 0:
                queue.append(m)
    if len(order) != len(gates):
        cyc = [g.id for g in gates if indegree[g.id] > 0]
        raise NetlistError(f"网表存在组合环路，涉及门: {cyc[:10]}")

    raw_outputs = obj.get("outputs", [])
    if not isinstance(raw_outputs, list) or not raw_outputs:
        raise NetlistError("outputs 必须是非空数组")
    outputs = [str(x) for x in raw_outputs]
    if len(set(outputs)) != len(outputs):
        raise NetlistError("outputs 中存在重复信号")
    for o in outputs:
        if o not in known:
            raise NetlistError(f"输出 {o} 未在 inputs/gates 中声明")

    nl = Netlist(inputs=inputs, gates=gates, outputs=outputs, order=order)
    nl._gate_map = gate_map
    return nl

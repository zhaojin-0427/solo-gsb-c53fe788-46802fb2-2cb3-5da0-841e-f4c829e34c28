"""层次化模块/组合网表的数据模型与校验。

模块（module）是有名字与版本、无环的层次网表：声明**有序**的输入/输出
端口，网表由门（复用 models.parse_netlist 的门语义与延迟区间）和**模块
实例**组成；组合（composition）是仅由模块实例构成的顶层网表。

核心校验（不合法即拒绝保存，见 HierarchyError）：

* 标识符合法且在模块/组合命名空间内唯一（输入、门、实例、显式 wire 不重名）；
* 每个实例必须绑定**已存在**的模块版本（"模块名@版本" 或 module+version）；
* 实例的 connections 必须为被绑定模块的每个端口提供**恰好一次**映射
  ——端口缺失、映射未知端口、同一端口重复映射都报错；
* 驱动唯一：实例输出端口必须连到一个显式 wire（或可隐式创建的新 wire），
  多个源驱动同一信号、连到已有门/输入输出端口都报错；
* 所有使用的信号必须存在（显式 wires 或输入/门/被驱动 wire/输出端口名）；
* 信号依赖图（门→其扇入源，实例→其输出 wire 的扇入实例）无组合环；
* 模块引用图无环（保存时 DFS 检测直接/间接自引用的实例递归）。
"""

from __future__ import annotations

import heapq
import re
from dataclasses import dataclass, field

from ..models import (
    ALL_TYPES,
    UNARY_TYPES,
    _parse_delay,
)

# 标识符：不含空白与路径分隔符 '/'，允许数字开头（版本号常见 "1.0"、"2024r1"）。
IDENT_RE = re.compile(r"^[^\s/]{1,64}$", re.UNICODE)
MAX_FLATTEN_GATES = int(__import__("os").getenv("HAZARD_HIER_MAX_GATES", "2000"))
MAX_INSTANCE_DEPTH = int(__import__("os").getenv("HAZARD_HIER_MAX_DEPTH", "100"))


class HierarchyError(ValueError):
    """层次网表格式、绑定或拓扑错误。"""


def _ident(name, what: str):
    s = str(name)
    if not IDENT_RE.match(s):
        raise HierarchyError(
            f"{what} {s!r} 不合法：需以字母/下划线/汉字开头，"
            "仅含字母数字下划线/点/连字符/汉字，长度 1~64，且不能含 '/'"
        )
    return s


@dataclass
class HGate:
    id: str
    type: str
    inputs: list[str]          # 已规范化为局部 canonical 信号
    delay_min: int
    delay_max: int


@dataclass
class HInstance:
    id: str
    module: str
    version: str
    connections: dict[str, str]  # 被引用模块端口名 -> 本作用域信号名


@dataclass
class HBody:
    """一个已解析的模块体或顶层组合。"""
    kind: str                  # "module" | "composition"
    inputs: list[str]          # 有序输入端口
    outputs: list[str]         # 有序输出端口（局部信号名）
    gates: list[HGate]
    instances: list[HInstance]
    wires: list[str]           # 显式声明的内部 wire
    # 校验产物
    gate_order: list[str] = field(default_factory=list)   # 门的确定性拓扑序
    inst_order: list[str] = field(default_factory=list)   # 实例的确定性拓扑序
    refs: set[tuple[str, str]] = field(default_factory=set)
    gate_map: dict[str, HGate] = field(default_factory=dict)
    inst_map: dict[str, HInstance] = field(default_factory=dict)
    # 拓扑分析后的稳定全序（门与实例混合，展开时按此顺序处理）
    body_order: list[str] = field(default_factory=list)


def _coerce_connections(raw, inst_id: str) -> dict[str, str]:
    """连接支持 {端口: 信号} 对象或 [[端口, 信号], ...] 数组（后者能检测重复）。"""
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    if isinstance(raw, list):
        out: dict[str, str] = {}
        for i, item in enumerate(raw):
            if (not isinstance(item, (list, tuple)) or len(item) != 2
                    or not all(isinstance(x, str) for x in item)):
                raise HierarchyError(
                    f"实例 {inst_id} 的 connections[{i}] 必须是 [端口, 信号] 数组")
            port, wire = item
            if port in out:
                raise HierarchyError(
                    f"实例 {inst_id} 的端口 {port!r} 被重复映射到 "
                    f"{out[port]!r} 与 {wire!r}")
            out[port] = wire
        return out
    raise HierarchyError(f"实例 {inst_id} 的 connections 必须是对象或 [端口,信号] 数组")


class _SelfRef:
    """parse_body 期间“正在登记的自身模块”的占位引用（用于递归检测）。"""

    def __init__(self, module: str, version: str):
        self.module = module
        self.version = version
        self.inputs: list[str] = []
        self.outputs: list[str] = []


def _ports(doc) -> tuple[list[str], list[str]]:
    """统一“已登记模块文档 dict”与“已解析 HBody/_SelfRef/占位”的端口访问。"""
    if doc is None:
        return None
    if hasattr(doc, "inputs") and hasattr(doc, "outputs"):
        return list(doc.inputs), list(doc.outputs)
    return list(doc["inputs"]), list(doc["outputs"])


def parse_body(obj, kind: str, lookup, self_ref=None) -> HBody:
    """解析并校验模块体 / 顶层组合。

    kind="module"      —— 保存单个模块（inputs/outputs 必填，可含门与实例）。
    kind="composition" —— 顶层组合（inputs/outputs 必填，只允许实例）。
    lookup(module, version) -> 模块文档 dict（至少含 inputs/outputs）；
                              找不到返回 None。
    """
    if not isinstance(obj, dict):
        raise HierarchyError("层次网表必须是 JSON 对象")

    inputs = _ordered_names(obj.get("inputs"), "inputs", allow_empty=False)
    outputs = _ordered_names(obj.get("outputs"), "outputs", allow_empty=False)

    raw_wires = obj.get("wires", [])
    if not isinstance(raw_wires, list):
        raise HierarchyError("wires 必须是字符串数组")
    wires = [_ident(w, "wire") for w in raw_wires]

    # ---- 标识符命名空间唯一 ----
    names: dict[str, str] = {}

    def claim(name, owner):
        _ident(name, owner)
        if name in names:
            raise HierarchyError(f"{owner} {name!r} 与 {names[name]} {name!r} 重名")
        names[name] = owner

    for n in inputs:
        claim(n, "输入端口")
    for n in wires:
        claim(n, "wire")
    if len(set(outputs)) != len(outputs):
        dup = {n for n in outputs if outputs.count(n) > 1}
        raise HierarchyError(f"输出端口重复: {sorted(dup)}")
    # 输出端口名：可引用输入（直接导出）；其余名字在 driver 阶段作为 wire 登记。

    # ---- 门 ----
    gates: list[HGate] = []
    raw_gates = obj.get("gates", [])
    if not isinstance(raw_gates, list):
        raise HierarchyError("gates 必须是数组")
    for i, g in enumerate(raw_gates):
        if not isinstance(g, dict):
            raise HierarchyError(f"第 {i + 1} 个门不是对象")
        gid = g.get("id")
        if gid is None:
            raise HierarchyError(f"第 {i + 1} 个门缺少 id")
        claim(str(gid), "门")
        gtype = str(g.get("type", "")).lower()
        if gtype not in ALL_TYPES:
            raise HierarchyError(
                f"门 {gid} 的类型 {gtype!r} 不支持，允许: {sorted(ALL_TYPES)}")
        arity = 1 if gtype in UNARY_TYPES else 2
        ginputs = g.get("inputs")
        if not isinstance(ginputs, list) or len(ginputs) != arity:
            raise HierarchyError(f"门 {gid}({gtype}) 需要恰好 {arity} 个输入")
        ginputs = [str(x) for x in ginputs]
        lo, hi = _parse_delay(g.get("delay"))
        gates.append(HGate(str(gid), gtype, ginputs, lo, hi))

    # ---- 实例 ----
    instances: list[HInstance] = []
    raw_insts = obj.get("instances", [])
    if not isinstance(raw_insts, list):
        raise HierarchyError("instances 必须是数组")
    for i, it in enumerate(raw_insts):
        if not isinstance(it, dict):
            raise HierarchyError(f"第 {i + 1} 个实例不是对象")
        iid = it.get("id")
        if iid is None:
            raise HierarchyError(f"第 {i + 1} 个实例缺少 id")
        claim(str(iid), "实例")
        module, version = _module_ref(it)
        doc = lookup(module, version)
        if doc is None and (module, version) != self_ref:
            raise HierarchyError(
                f"实例 {iid} 绑定的模块版本 {module}@{version} 不存在，请先登记")
        if doc is None:  # 自引用：HBody 尚未构造，仅记录引用，递归环稍后检测
            doc = _SelfRef(module, version)
            doc.inputs = list(inputs)
            doc.outputs = list(outputs)
        m_inputs, m_outputs = _ports(doc)
        m_ports = m_inputs + m_outputs
        conns = _coerce_connections(it.get("connections", {}), str(iid))
        _check_ports(str(iid), conns, m_ports, m_inputs, m_outputs)
        instances.append(HInstance(str(iid), module, version, conns))

    if kind == "composition" and gates:
        raise HierarchyError("顶层组合只能包含模块实例，不能直接放门")
    if not gates and not instances:
        raise HierarchyError("网表至少需要一个门或一个模块实例")

    body = HBody(kind=kind, inputs=list(inputs), outputs=list(outputs),
                 gates=gates, instances=instances, wires=wires)
    body.gate_map = {g.id: g for g in gates}
    body.inst_map = {x.id: x for x in instances}
    body.refs = {(x.module, x.version) for x in instances}

    # ---- 信号声明 / 驱动唯一性 ----
    # drivers[signal] = 唯一驱动描述 ("input"/"gate"/"inst", owner, detail)
    drivers: dict[str, tuple[str, str, str]] = {}

    def add_driver(signal, kind, owner, detail):
        old = drivers.get(signal)
        if old is not None:
            raise HierarchyError(
                f"驱动冲突: 信号 {signal} 同时被 {old[2]} 与 {detail} 驱动")
        drivers[signal] = (kind, owner, detail)

    for n in inputs:
        add_driver(n, "input", n, f"输入端口 {n}")
    for g in gates:
        add_driver(g.id, "gate", g.id, f"门 {g.id}")
    # 显式 wire 与输出端口名：登记为“暂无驱动”，稍后检查必须被驱动
    pending: dict[str, str] = {}
    for w in wires:
        pending[w] = f"wire {w}"
    for o in outputs:
        _ident(o, "输出端口")
        if o not in drivers:  # 输出端口可直接引用已有门/输入信号
            pending.setdefault(o, f"输出端口 {o}")

    inst_docs: dict[str, tuple] = {}
    for x in instances:
        d = lookup(x.module, x.version)
        if (x.module, x.version) == self_ref:
            d = _SelfRef(x.module, x.version)
            d.inputs = list(inputs)
            d.outputs = list(outputs)
        if d is None:
            raise HierarchyError(
                f"实例 {x.id} 绑定的模块版本 {x.module}@{x.version} 不存在，"
                "请先登记")
        inst_docs[x.id] = _ports(d)
    for x in instances:
        in_ports, out_ports = inst_docs[x.id]
        for port, wire in x.connections.items():
            _ident(wire, f"实例 {x.id} 端口 {port} 的连接")
            if port in out_ports:
                detail = f"实例 {x.id} 的输出端口 {port}"
                if wire in pending:
                    add_driver(wire, "inst", x.id, detail)
                    del pending[wire]
                elif wire in drivers:
                    add_driver(wire, "inst", x.id, detail)  # 必抛冲突
                else:
                    add_driver(wire, "inst", x.id, detail)
        body.refs.add((x.module, x.version))

    known = set(drivers) | set(pending)
    for x in instances:
        in_ports, _ = inst_docs[x.id]
        for port, wire in x.connections.items():
            if port in in_ports and wire not in known:
                raise HierarchyError(
                    f"实例 {x.id} 的输入端口 {port} 连到了未声明的信号 {wire}；"
                    "请在 wires 中声明，或由某个实例输出驱动")

    # ---- 扇入引用必须可解析 ----
    def resolve(ref, where):
        if ref not in known:
            raise HierarchyError(f"{where} 引用了未声明的信号 {ref}")
        return ref

    for g in gates:
        g.inputs = [resolve(s, f"门 {g.id}") for s in g.inputs]

    # 显式 wire / 输出端口必须被驱动（输入、门同名时 drivers 已有源，不在 pending）
    for w, what in sorted(pending.items()):
        raise HierarchyError(f"信号 {w}（{what}）没有驱动源"
                             "（需要门或实例输出驱动）")

    # ---- 信号级无环（门 + 实例混合 DAG，Kahn，稳定排序） ----
    _topo_sort(body, inst_docs, drivers)
    return body


def _ordered_names(raw, what: str, allow_empty: bool) -> list[str]:
    if not isinstance(raw, list) or (not allow_empty and not raw):
        raise HierarchyError(f"{what} 必须是非空字符串数组（有序端口）")
    out = [str(x) for x in raw]
    if len(set(out)) != len(out):
        raise HierarchyError(f"{what} 中存在重复端口")
    return out


def _module_ref(it: dict) -> tuple[str, str]:
    ref = it.get("module_ref") or it.get("ref")
    if ref is not None:
        if not isinstance(ref, str) or "@" not in ref:
            raise HierarchyError(
                f"实例 {it.get('id')} 的 module_ref 必须是 '名字@版本' 字符串")
        module, version = ref.split("@", 1)
    else:
        module = it.get("module")
        version = it.get("version")
        if module is None or version is None:
            raise HierarchyError(
                f"实例 {it.get('id')} 需要 module+version 或 module_ref='名字@版本'")
    return _ident(module, "模块名"), _ident(version, "模块版本")


def _check_ports(inst_id, conns, m_ports, m_inputs, m_outputs):
    port_set = set(m_ports)
    unknown = sorted(set(conns) - port_set)
    if unknown:
        raise HierarchyError(
            f"实例 {inst_id} 映射了模块不存在的端口 {unknown}；"
            f"可用端口: {m_ports}")
    missing = [p for p in m_ports if p not in conns]
    if missing:
        raise HierarchyError(
            f"实例 {inst_id} 缺少端口映射 {missing}（模块输入 {m_inputs}，"
            f"输出 {m_outputs}，每个端口必须恰好映射一次）")


def _topo_sort(body: HBody, inst_docs: dict, drivers: dict):
    """门与实例的混合信号依赖图 Kahn 拓扑序，并产出稳定的全序。

    节点：每个门、每个实例。
    边：门 g 扇入来自门 g' / 实例 x 输出 → g'→g、x→g；
        实例 y 的输入连接来自门 g' / 实例 x 输出 → g'→y、x→y。
    实例输出 wire 的驱动者即该实例（drivers[wire] 单源）。
    """
    gate_set = {g.id for g in body.gates}
    inst_set = {x.id for x in body.instances}

    def owner_of(signal):
        """信号的（唯一）驱动节点：(kind, owner)；输入端口/悬空返回 None。"""
        d = drivers.get(signal)
        if d is None or d[0] == "input":
            return None
        return (d[0], d[1])

    node_kind = {g.id: "gate" for g in body.gates}
    node_kind.update({x.id: "inst" for x in body.instances})
    indeg = {n: 0 for n in node_kind}
    succ: dict[str, list[str]] = {n: [] for n in node_kind}

    def add_deps(node, fanin_signals):
        deps = set()
        for s in fanin_signals:
            own = owner_of(s)
            if own is not None and own[1] != node:
                deps.add(own)
        for kind, src in deps:
            succ[src].append(node)
            indeg[node] += 1

    for g in body.gates:
        add_deps(g.id, g.inputs)
    for x in body.instances:
        in_ports, _ = inst_docs[x.id]
        fanin = [x.connections[p] for p in in_ports]
        add_deps(x.id, fanin)

    ready = sorted(n for n, d in indeg.items() if d == 0)
    order: list[str] = []
    heapq.heapify(ready)
    while ready:
        n = heapq.heappop(ready)
        order.append(n)
        for m in sorted(succ[n]):
            indeg[m] -= 1
            if indeg[m] == 0:
                heapq.heappush(ready, m)
    if len(order) != len(node_kind):
        cyc = sorted(n for n, d in indeg.items() if d > 0)
        raise HierarchyError(f"层次网表存在组合环，涉及门/实例: {cyc[:10]}")

    body.body_order = order
    body.gate_order = [n for n in order if n in gate_set]
    body.inst_order = [n for n in order if n in inst_set]


def check_no_recursion(name: str, version: str, body: HBody,
                       get_body) -> None:
    """模块引用图 DFS：一个模块版本不得（传递）实例化自身。

    get_body(module, version) -> HBody | None。版本不可变且版本间无修改，
    历史模块不可能形成环；该检查同时拦截“引用自己这个新版本”等请求。
    """
    stack: list[tuple[str, str]] = [(name, version)]
    on_stack = {stack[0]}
    visited: set[tuple[str, str]] = set()

    def dfs(m, v):
        b = get_body(m, v) if (m, v) != (name, version) else body
        if b is None:
            return  # 引用不存在由 parse_body 拦截；这里只查环
        for ref in sorted(b.refs):
            if ref in on_stack:
                chain = " -> ".join(f"{a}@{b_}" for a, b_ in stack + [ref])
                raise HierarchyError(f"检测到实例递归（模块引用成环）：{chain}")
            if ref in visited:
                continue
            stack.append(ref)
            on_stack.add(ref)
            dfs(*ref)
            on_stack.discard(ref)
            stack.pop()
        visited.add((m, v))

    dfs(name, version)

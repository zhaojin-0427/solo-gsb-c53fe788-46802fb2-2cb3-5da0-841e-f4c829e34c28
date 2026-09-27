"""层次网表的确定性展开。

展开把模块/组合中由门与模块实例构成的层次网表递归“压平”成可直接交给既有
`parse_netlist` + z3 验证流水线的无环门级网表：

* 每个局部门生成**唯一全局限定名** ``实例路径/gateId``（顶层门路径为空，
  保留为 ``gateId``），实例路径形如 ``u1/u2``；
* 模块实例的输入/输出端口与外部相连的 wire 是**同一根物理信号的别名**
  （如 ``u1/in1`` 与顶层 ``a``），用并查集跨层合并，每个等价类选一个
  确定的代表名（优先顶层输入，其次门输出；同优先级路径浅、字典序小者胜）；
  由于保存时已保证每个信号最终由门或主输入驱动，归并后不会出现悬空信号；
* 同一模块被实例化多次时，每次展开的门与内部 wire 都按实例路径分别命名，
  互不干扰；
* 沿 HBody.body_order（门+实例混合 Kahn 稳定序）DFS，门列表最后再按
  全局 id 排序，保证同样输入字节序的展开结果逐字节可复现（版本固定后
  可重放旧分析）。

同时保留反例/波形展示所需的层次信息：每个门的实例来源（module@version、
实例路径、局部门延迟）以及节点穿越的实例边界端口映射 crossings（从外到内
排序：实例路径 + 输入/输出端口名 + 模块版本）。
"""

from __future__ import annotations

from .models import MAX_FLATTEN_GATES, MAX_INSTANCE_DEPTH, HierarchyError


class _UnionFind:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def declare(self, x: str) -> None:
        if x not in self.parent:
            self.parent[x] = x

    def find(self, x: str) -> str:
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != x:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        # 代表名：路径深度浅（“/”少）优先，深度相同取字典序，保证确定性。
        da, db = ra.count("/"), rb.count("/")
        if da < db or (da == db and ra < rb):
            self.parent[rb] = ra
        else:
            self.parent[ra] = rb


def flatten(top_name: str, top_body, get_module_body,
            max_gates: int = MAX_FLATTEN_GATES,
            max_depth: int = MAX_INSTANCE_DEPTH) -> dict:
    """展开已解析的顶层 HBody（组合或模块）。

    top_name 仅用于展示；get_module_body(module, version) -> HBody，
    找不到返回 None。
    """
    uf = _UnionFind()
    gates: list[dict] = []
    node_info: dict[str, dict] = {}
    inst_records: dict[str, dict] = {}
    inst_ports: dict[str, tuple[list[str], list[str]]] = {}
    module_versions: dict[str, str] = {}
    stats = {"n_gates": 0, "n_instances": 0, "max_depth": 0}

    def gpath(path: str, local: str) -> str:
        return local if path == "" else f"{path}/{local}"

    def expand(b, path: str, depth: int) -> None:
        if depth > max_depth:
            raise HierarchyError(f"实例层次超过 {max_depth} 层，拒绝展开")
        for key in b.body_order:
            if key in b.gate_map:
                g = b.gate_map[key]
                gid = gpath(path, g.id)
                uf.declare(gid)
                ginputs = []
                for s in g.inputs:
                    full = gpath(path, s)
                    uf.declare(full)
                    ginputs.append(full)
                gates.append({
                    "id": gid, "type": g.type, "inputs": ginputs,
                    "delay": [g.delay_min, g.delay_max],
                })
                stats["n_gates"] += 1
                if stats["n_gates"] > max_gates:
                    raise HierarchyError(
                        f"展开后门数超过上限 {max_gates}，拒绝展开")
                if path != "":
                    parent_path, _, inst_id = path.rpartition("/")
                    node_info[gid] = {
                        "kind": "gate", "local": g.id,
                        "instance_path": parent_path,
                        "instance_id": inst_id,
                        "module": None, "version": None,
                        "delay": [g.delay_min, g.delay_max],
                    }
            else:
                x = b.inst_map[key]
                child = gpath(path, x.id)
                child_body = get_module_body(x.module, x.version)
                if child_body is None:
                    raise HierarchyError(
                        f"实例 {child} 绑定的模块 {x.module}@{x.version} 不存在")
                module_versions[x.module] = x.version
                inst_ports[child] = (list(child_body.inputs),
                                     list(child_body.outputs))
                inst_records[child] = {
                    "path": child, "id": x.id,
                    "module": x.module, "version": x.version,
                    "connections": dict(x.connections),
                }
                stats["n_instances"] += 1
                stats["max_depth"] = max(stats["max_depth"], depth + 1)

                # 子作用域端口先登记，再与外部 wire 做别名合并；外部 wire
                # 可能是尚未处理的兄弟实例输出，先声明，代表名最终统一决定。
                # 子模块内同名门驱动同名输出端口时，两者的全局限定名完全相同
                # （child/op），天然是同一并查集元素，无需额外合并。
                for sig in child_body.inputs + child_body.outputs:
                    uf.declare(f"{child}/{sig}")
                for port, wire in x.connections.items():
                    external = gpath(path, wire)
                    uf.declare(external)
                    uf.union(external, f"{child}/{port}")

                expand(child_body, child, depth + 1)

    top = top_body
    for sig in top.inputs:
        uf.declare(sig)
    expand(top, "", 0)

    # ---- 别名归并 ----
    # 代表名优先级：顶层输入 > 门输出（路径深度浅优先，其次字典序）> 其他。
    # 这样“实例输出端口→外部 wire”若外部由门/输入驱动，代表名就是带驱动的
    # 节点，内部 wire 只有在真正只被实例输出驱动时才插入 wire-buf。
    gate_id_set_pre = {g["id"] for g in gates}

    def _priority(member: str):
        if member in set(top.inputs):
            return (0, member.count("/"), member)
        if member in gate_id_set_pre:
            return (1, member.count("/"), member)
        return (2, member.count("/"), member)

    # 在并查集上强制每个等价类选优先级最小的成员为代表
    classes_raw: dict[str, list[str]] = {}
    for m in list(uf.parent):
        classes_raw.setdefault(uf.find(m), []).append(m)
    forced_root: dict[str, str] = {}
    for members in classes_raw.values():
        rep = min(members, key=_priority)
        for m in members:
            forced_root[m] = rep

    def canon(x: str) -> str:
        uf.declare(x)  # 外部信号可能是深层实例的内部 wire（未在本层声明）
        root = uf.find(x)
        return forced_root.get(root, root)

    for g in gates:
        g["inputs"] = [canon(s) for s in g["inputs"]]
    gates.sort(key=lambda g: g["id"])

    top_in_canon = [canon(s) for s in top.inputs]
    flat_inputs = list(dict.fromkeys(top_in_canon))  # 保序去重
    flat_outputs = [canon(o) for o in top.outputs]

    gate_ids = [g["id"] for g in gates]
    if len(set(gate_ids)) != len(gate_ids):
        raise HierarchyError("展开内部错误：门全局 id 冲突")

    # 健全性：模型校验已保证每个信号最终由门驱动（无驱动输出端口的模块/组合
    # 在保存时拒绝），端口别名并类后代表名优先选门/输入。任何仍追溯不到
    # 驱动节点的代表名都属于内部不变量破坏。
    driven = set(flat_inputs) | set(gate_ids)
    orphans = sorted({canon(m) for m in uf.parent} - driven)
    if orphans:
        raise HierarchyError(
            f"展开内部错误：信号 {orphans[:5]} 追溯不到门驱动")

    netlist = {
        "name": top_name,
        "inputs": flat_inputs,
        "gates": gates,
        "outputs": flat_outputs,
    }

    # 门的 module@version：由其所属实例记录填充
    for gid, info in node_info.items():
        if info["kind"] == "gate":
            p = info["instance_path"]
            inst_path = info["instance_id"] if p == "" else f"{p}/{info['instance_id']}"
            rec = inst_records[inst_path]
            info["module"] = rec["module"]
            info["version"] = rec["version"]

    for n in flat_inputs:
        node_info.setdefault(n, {
            "kind": "input", "local": n, "instance_path": None,
            "instance_id": None, "module": None, "version": None})

    # ---- crossings：等价类中每个实例边界端口（外→内排序）----
    classes: dict[str, list[str]] = {}
    for member in uf.parent:
        classes.setdefault(canon(member), []).append(member)
    for rep, members in classes.items():
        crossings = []
        for m in members:
            if "/" not in m:
                continue
            inst_path, _, port = m.rpartition("/")
            in_ports, out_ports = inst_ports.get(inst_path, ([], []))
            rec = inst_records.get(inst_path)
            if rec is None or port not in in_ports + out_ports:
                continue
            ext_local = rec["connections"].get(port)
            if ext_local:
                # 外部连接处在**父作用域**：实例路径去掉最后一段
                parent_path = inst_path.rsplit("/", 1)[0] if "/" in inst_path \
                    else ""
                ext_member = ext_local if parent_path == "" \
                    else f"{parent_path}/{ext_local}"
            else:
                ext_member = None
            crossings.append({
                "instance_path": inst_path,
                "instance_id": rec["id"],
                "module": rec["module"], "version": rec["version"],
                "port": port,
                "direction": "input" if port in in_ports else "output",
                "external_member": ext_member,
                "external": canon(ext_member) if ext_member else rep,
            })
        crossings.sort(key=lambda c: (c["instance_path"].count("/"),
                                      c["instance_path"],
                                      0 if c["direction"] == "input" else 1,
                                      c["port"]))
        node_info.setdefault(rep, {"kind": "wire", "local": rep,
                                   "instance_path": None, "instance_id": None,
                                   "module": None, "version": None})
        node_info[rep]["crossings"] = crossings

    members_index = {m: rep for rep, ms in classes.items() for m in ms}

    return {
        "netlist": netlist,
        "inputs": flat_inputs,
        "outputs": flat_outputs,
        "output_mapping": dict(zip(top.outputs, flat_outputs)),
        "instances": [inst_records[k] for k in sorted(inst_records)],
        "nodes": node_info,
        "alias_index": members_index,
        "module_versions": module_versions,
        "statistics": stats,
    }

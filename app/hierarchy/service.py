"""层次模块库的业务服务：登记/解析缓存、组合保存、展开、验证、标注。

对 routes 提供与 HTTP 无关的纯函数，方便测试直接调用。
"""

from __future__ import annotations

import json

from ..models import parse_netlist
from ..params import parse_params
from ..verifier.z3verifier import (
    LIMIT_CELLS,
    LIMIT_GATES,
    LIMIT_HORIZON,
    LIMIT_NODES,
    ScaleLimit,
    verify,
)
from . import audit as audit_engine, store
from .flatten import flatten as flatten_hier
from .models import HierarchyError, check_no_recursion, parse_body

# 请求内 HBody 解析缓存：(kind, name, version) -> HBody
_BODY_CACHE: dict[tuple[str, str, str], object] = {}


class Conflict(HierarchyError):
    """与已存版本冲突（409）。"""


class RevisionNotFound(HierarchyError):
    """请求的组合修订不存在（404）。"""


class _Placeholder:
    """捆绑包解析中“正在处理的同包模块”的端口占位。"""

    def __init__(self, module, version, doc):
        self.module = module
        self.version = version
        self.inputs = list(doc.get("inputs", []))
        self.outputs = list(doc.get("outputs", []))


def _lookup_doc(module: str, version: str):
    return store.get_module_doc(module, version)


def get_body(module: str, version: str):
    key = ("module", module, version)
    b = _BODY_CACHE.get(key)
    if b is None:
        doc = store.get_module_doc(module, version)
        if doc is None:
            return None
        b = parse_body(doc, "module", _lookup_doc)
        _BODY_CACHE[key] = b
    return b


def invalidate_caches() -> None:
    _BODY_CACHE.clear()


# ---------------- 模块登记 ----------------

def register_module(doc: dict, bundle_lookup=None) -> dict:
    name, version = _name_version(doc)
    if store.get_module_doc(name, version) is not None:
        raise Conflict(f"模块 {name}@{version} 已存在；模块版本不可修改，"
                       "请使用新的版本号登记")
    lookup = bundle_lookup or _lookup_doc
    body = parse_body(doc, "module", lookup, self_ref=(name, version))
    check_no_recursion(name, version, body,
                       lambda m, v: body if (m, v) == (name, version)
                       else get_body(m, v))
    try:
        rec = store.create_module(name, version, doc)
    except store.Conflict as e:
        raise Conflict(str(e))
    invalidate_caches()
    return {"module": rec, "summary": _body_summary(body)}


def import_bundle(payload: dict) -> dict:
    """一次性导入若干模块（可互相引用，按拓扑序落库）+ 可选组合。

    {"modules": [doc,...], "compositions": {"名字": doc, ...}}。
    已存在且内容相同的模块跳过；内容不同报冲突，不做任何写入。
    """
    if not isinstance(payload, dict):
        raise HierarchyError("导入包必须是 JSON 对象")
    raw_mods = payload.get("modules", [])
    if not isinstance(raw_mods, list):
        raise HierarchyError("modules 必须是数组")
    pending: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    for doc in raw_mods:
        name, version = _name_version(doc)
        if (name, version) in pending:
            raise HierarchyError(f"导入包内模块重复: {name}@{version}")
        pending[(name, version)] = doc
        order.append((name, version))

    parsed: dict[tuple[str, str], object] = {}
    in_progress: set[tuple[str, str]] = set()

    def lookup(m, v):
        if (m, v) in parsed:
            return parsed[(m, v)]
        if (m, v) in in_progress:
            # 环中的待解析模块：端口信息已知，返回占位（HBody.refs 之后检测）
            ph = _Placeholder(m, v, pending[(m, v)])
            return ph
        if (m, v) in pending:
            in_progress.add((m, v))
            try:
                b = parse_body(pending[(m, v)], "module", lookup,
                               self_ref=(m, v))
            finally:
                in_progress.discard((m, v))
            parsed[(m, v)] = b
            return b
        return get_body(m, v)

    # 全部解析；环中的占位引用足以完成端口/绑定校验，递归环随后统一报错
    for key in order:
        lookup(*key)
    for key in order:
        name, version = key
        check_no_recursion(name, version, parsed[key], lookup)

    # 冲突预检：同 (name,version) 已存在且内容不同 → 整包拒绝
    created, skipped = [], []
    for key in order:
        name, version = key
        existing = store.get_module_doc(name, version)
        if existing is not None:
            if _canon_json(existing) != _canon_json(pending[key]):
                raise Conflict(
                    f"模块 {name}@{version} 已存在且内容不同；版本不可修改，"
                    "导入已取消（未写入任何模块）")
            skipped.append(f"{name}@{version}")
        else:
            store.create_module(name, version, pending[key])
            created.append(f"{name}@{version}")
    invalidate_caches()

    comps = payload.get("compositions", {})
    comp_results = []
    if comps:
        if not isinstance(comps, dict):
            raise HierarchyError("compositions 必须是 {名字: 组合文档}")
        for cname, cdoc in comps.items():
            if not isinstance(cname, str) or not cname.strip():
                raise HierarchyError("组合名必须是非空字符串")
            cdoc = dict(cdoc)
            cdoc.setdefault("name", cname)
            comp_results.append(save_composition(cdoc))

    return {"created_modules": created, "skipped_existing": skipped,
            "compositions": comp_results}


def export_bundle() -> dict:
    mods = []
    for m in store.list_modules():
        mods.append(store.get_module_doc(m["name"], m["version"]))
    comps = {c["name"]: store.get_composition_doc(c["name"])
             for c in store.list_compositions()}
    return {"modules": mods, "compositions": comps}


# ---------------- 组合保存 ----------------

def save_composition(doc: dict) -> dict:
    name = doc.get("name") if isinstance(doc, dict) else None
    if not isinstance(name, str) or not name.strip():
        raise HierarchyError("组合需要唯一 name")
    body = parse_body(doc, "composition", _lookup_doc)
    # 先校验并确定性展开，只有成功后才保存新修订；展开结果同时作为该修订的
    # 不可变快照，后续行为审计无需依赖仍会变化的当前组合文档。
    flat = flatten_hier(name, body, lambda m, v: get_body(m, v))
    try:
        rec = store.save_composition_revision(
            name, doc, flat["netlist"], flat["module_versions"])
    except store.HierarchyStoreError as e:
        raise HierarchyError(str(e))
    invalidate_caches()
    return {"composition": rec, "summary": _body_summary(body)}


def delete_composition(name: str) -> None:
    if not store.delete_composition(name):
        raise HierarchyError(f"组合 {name} 不存在")
    invalidate_caches()


def validate_composition(doc: dict) -> dict:
    body = parse_body(doc, "composition", _lookup_doc)
    return {"ok": True, "summary": _body_summary(body)}


def flatten_dry(doc: dict) -> dict:
    """不写库，直接对请求里的组合文档做校验+展开（保存前预览）。"""
    body = parse_body(doc, "composition", _lookup_doc)
    name = str(doc.get("name", "dryrun"))
    return flatten_hier(name, body, lambda m, v: get_body(m, v))


def validate_module_doc(doc: dict) -> dict:
    name, version = _name_version(doc)
    body = parse_body(doc, "module", _lookup_doc, self_ref=(name, version))
    check_no_recursion(name, version, body,
                       lambda m, v: body if (m, v) == (name, version)
                       else get_body(m, v))
    return {"ok": True, "name": name, "version": version,
            "summary": _body_summary(body)}


def flatten_saved(name: str) -> dict:
    cdoc = store.get_composition_doc(name)
    if cdoc is not None:
        body = parse_body(cdoc, "composition", _lookup_doc)
        return flatten_hier(name, body, lambda m, v: get_body(m, v))
    # 也允许直接展开一个已登记模块（便于页面查看，名字写成 模块@版本）
    if "@" in name:
        m, v = name.split("@", 1)
        body = get_body(m, v)
        if body is not None:
            return flatten_hier(name, body, lambda mm, vv: get_body(mm, vv))
    raise HierarchyError(f"组合 {name} 不存在")


# ---------------- 组合修订快照与行为等价审计 ----------------

def list_revisions(name: str) -> list[dict]:
    if store.get_composition_doc(name) is None:
        raise RevisionNotFound(f"组合 {name} 不存在")
    return store.list_composition_revisions(name)


def get_revision(name: str, revision: int) -> dict:
    rec = store.get_composition_revision(name, revision)
    if rec is None:
        raise RevisionNotFound(f"组合 {name} 的修订 {revision} 不存在")
    if rec["flat_json"] is None:
        _backfill_revision_snapshot(name, revision)
        rec = store.get_composition_revision(name, revision)
    return rec


def _backfill_revision_snapshot(name: str, revision: int) -> None:
    """本功能上线前保存的旧组合只有当前文档，按需补建修订展开快照。"""
    rec = store.get_composition_revision(name, revision)
    if rec is None or rec["flat_json"] is not None:
        return
    current = store.get_composition_doc(name)
    meta = next((x for x in store.list_compositions() if x["name"] == name), None)
    if current is None or meta is None or meta["version"] != revision:
        raise RevisionNotFound(
            f"修订 {name}@{revision} 缺少展开快照，且不是当前修订，无法补齐")
    body = parse_body(current, "composition", _lookup_doc)
    flat = flatten_hier(name, body, lambda m, v: get_body(m, v))
    store.update_revision_snapshot(
        name, revision, flat["netlist"], flat["module_versions"])


def _audit_ports(raw_params: dict, body) -> list[str]:
    raw = raw_params.get("outputs") if isinstance(raw_params, dict) else None
    if raw is None:
        return list(body.outputs)
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise HierarchyError("审计参数 outputs 必须是数组")
    ports = []
    for item in raw:
        if isinstance(item, str):
            ports.append(item)
        elif isinstance(item, dict) and "name" in item:
            ports.append(str(item["name"]))
        else:
            raise HierarchyError("审计输出必须是端口名或 {name,...} 对象")
    if not ports:
        raise HierarchyError("至少选择一个输出端口参与等价审计")
    if any(p not in body.outputs for p in ports):
        raise HierarchyError(
            f"审计输出必须属于组合端口 {list(body.outputs)}")
    return ports


def _normalize_audit_params(params_obj: dict, body, alias_index: dict,
                            output_ports: list[str],
                            forced_horizon: int | None = None) -> dict:
    """把顶层审计参数映射为一份可直接给 parse_params 的规范参数。

    parse_params 目前要求每个输出至少有一个 hazard 约束；等价审计并不检查
    约束，只复用它的输入/窗口解析，因此给一个宽松的占位跃迁上限，最终保存
    参数时移除该占位。
    """
    p = dict(params_obj) if isinstance(params_obj, dict) else {}
    if not isinstance(params_obj, dict):
        raise HierarchyError("审计参数必须是 JSON 对象")

    def resolve(signal: str) -> str:
        return alias_index.get(signal, signal)

    normalized = {"initial": {}, "final": {}, "windows": {}}
    for key in ("initial", "final", "windows"):
        raw = p.get(key)
        if raw is not None and not isinstance(raw, dict):
            raise HierarchyError(f"审计参数 {key} 必须是对象")
        normalized[key] = dict(raw or {})

    mapped = {
        "initial": {resolve(k): v for k, v in normalized["initial"].items()},
        "final": {resolve(k): v for k, v in normalized["final"].items()},
        "windows": {resolve(k): v for k, v in normalized["windows"].items()},
        "outputs": [
            {"name": resolve(port), "max_transitions": AUDIT_PLACEHOLDER_K}
            for port in output_ports
        ],
    }
    horizon = forced_horizon if forced_horizon is not None else p.get("horizon")
    if horizon is not None:
        mapped["horizon"] = horizon
    return mapped


AUDIT_PLACEHOLDER_K = 1_000_000


def _canonical_output_specs(params_obj: dict, output_ports: list[str]) -> list:
    raw = params_obj.get("outputs", output_ports) if isinstance(
        params_obj, dict) else output_ports
    if isinstance(raw, str):
        raw = [raw]
    by_name = {}
    for item in raw or []:
        if isinstance(item, str):
            by_name[item] = item
        elif isinstance(item, dict) and "name" in item:
            spec = {"name": str(item["name"])}
            for key in ("min_pulse_width", "max_transitions"):
                if item.get(key) is not None:
                    spec[key] = item[key]
            by_name[spec["name"]] = spec
    return [by_name[port] for port in output_ports]


def _canonical_audit_params(parsed, output_ports: list[str],
                            output_specs: list | None = None) -> dict:
    return {
        "initial": {n: parsed.initial[n] for n in parsed.initial},
        "final": {n: parsed.final[n] for n in parsed.final},
        "windows": {
            w.name: [w.tmin, w.tmax]
            for w in parsed.windows
            if w.is_switched or w.tmin != w.tmax
        },
        "outputs": output_specs if output_specs is not None else list(output_ports),
        "horizon": parsed.horizon,
    }


def _load_audit_revision(comp_name: str, revision: int):
    rec = get_revision(comp_name, revision)
    body = parse_body(rec["doc_json"], "composition", _lookup_doc)
    flat_net = rec["flat_json"]
    output_mapping = dict(zip(body.outputs, flat_net["outputs"]))
    alias_index = {n: n for n in flat_net.get("inputs", [])}
    alias_index.update({g["id"]: g["id"] for g in flat_net.get("gates", [])})
    alias_index.update(output_mapping)
    flat = {"netlist": flat_net, "outputs": flat_net["outputs"],
            "output_mapping": output_mapping, "alias_index": alias_index}

    return rec, body, flat


def behavior_audit(comp_name: str, left_revision: int, right_revision: int,
                   params_obj: dict) -> dict:
    if left_revision == right_revision:
        raise HierarchyError("请选择两个不同修订进行行为等价审计")
    left_revision, right_revision = sorted((left_revision, right_revision))

    left_rec, left_body, left_flat = _load_audit_revision(
        comp_name, left_revision)
    right_rec, right_body, right_flat = _load_audit_revision(
        comp_name, right_revision)
    if left_body.inputs != right_body.inputs or \
            left_body.outputs != right_body.outputs:
        raise HierarchyError(
            "两个修订的输入/输出端口序列不同，无法按同名端口同步审计")

    output_ports = _audit_ports(params_obj, left_body)
    raw_h = params_obj.get("horizon") if isinstance(params_obj, dict) else None
    forced_h = raw_h if isinstance(raw_h, int) and not isinstance(raw_h, bool) \
        else None
    left_map = _normalize_audit_params(
        params_obj, left_body, left_flat["alias_index"], output_ports, forced_h)
    right_map = _normalize_audit_params(
        params_obj, right_body, right_flat["alias_index"], output_ports,
        forced_h)
    nl_l = parse_netlist(left_flat["netlist"])
    nl_r = parse_netlist(right_flat["netlist"])
    par_l = parse_params(nl_l, left_map)
    par_r = parse_params(nl_r, right_map)
    if forced_h is None:
        common_h = max(par_l.horizon, par_r.horizon)
        left_map["horizon"] = right_map["horizon"] = common_h
        par_l = parse_params(nl_l, left_map)
        par_r = parse_params(nl_r, right_map)
    if par_l.horizon != par_r.horizon:
        raise HierarchyError("内部错误：两版审计 horizon 不一致")

    output_pairs = [(port, par_l.outputs[i].name, par_r.outputs[i].name)
                    for i, port in enumerate(output_ports)]
    try:
        result = audit_engine.audit_equivalence(
            nl_l, par_l, nl_r, par_r, output_pairs,
            (left_revision, right_revision))
    except audit_engine.AuditScaleLimit as e:
        result = {
            "status": "unknown",
            "equivalent": None,
            "reason": "scale_limit",
            "message": f"超过审计枚举规模上限，无法判定：{e}",
            "horizon": par_l.horizon,
            "limits": {
                "max_gates_per_revision": audit_engine.AUDIT_MAX_GATES,
                "max_horizon": audit_engine.AUDIT_MAX_HORIZON,
                "max_input_assignments":
                    audit_engine.AUDIT_MAX_INPUT_ASSIGNMENTS,
                "max_trajectories_per_revision":
                    audit_engine.AUDIT_MAX_TRAJECTORIES,
                "timeout_seconds": audit_engine.AUDIT_TIMEOUT_S,
            },
        }
    result["composition"] = comp_name
    result["revisions"] = {"left": left_revision, "right": right_revision}
    result["output_ports"] = output_ports
    output_specs = _canonical_output_specs(params_obj, output_ports)
    result["output_constraints"] = output_specs
    canonical = _canonical_audit_params(par_l, output_ports, output_specs)

    record_payload = {
        "comp_name": comp_name,
        "left_revision": left_revision,
        "right_revision": right_revision,
        "params": canonical,
        "left": {"doc": left_rec["doc_json"], "flat": left_rec["flat_json"],
                 "flat_hash": left_rec["flat_hash"],
                 "versions": left_rec["versions_json"] or {}},
        "right": {"doc": right_rec["doc_json"], "flat": right_rec["flat_json"],
                  "flat_hash": right_rec["flat_hash"],
                  "versions": right_rec["versions_json"] or {}},
        "result": result,
    }
    record = store.save_behavior_audit(record_payload)
    return {"ok": True, "record": record, "result": result,
            "params": canonical}


def replay_behavior_audit(rid: int) -> dict:
    rec = store.get_behavior_audit(rid)
    if rec is None:
        raise HierarchyError(f"行为审计记录 {rid} 不存在")
    raw_ports = rec["params_json"].get("outputs", [])
    ports = [x if isinstance(x, str) else x["name"] for x in raw_ports]
    nl_l = parse_netlist(rec["left_flat_json"])
    nl_r = parse_netlist(rec["right_flat_json"])
    output_mapping_l = dict(zip(
        store.get_composition_revision(rec["comp_name"], rec["left_revision"])
        ["doc_json"]["outputs"],
        rec["left_flat_json"]["outputs"]))
    output_mapping_r = dict(zip(
        store.get_composition_revision(rec["comp_name"], rec["right_revision"])
        ["doc_json"]["outputs"],
        rec["right_flat_json"]["outputs"]))
    p_l = dict(rec["params_json"], outputs=[
        {"name": output_mapping_l.get(n, n), "max_transitions": AUDIT_PLACEHOLDER_K}
        for n in ports
    ])
    p_r = dict(rec["params_json"], outputs=[
        {"name": output_mapping_r.get(n, n), "max_transitions": AUDIT_PLACEHOLDER_K}
        for n in ports
    ])
    par_l = parse_params(nl_l, p_l)
    par_r = parse_params(nl_r, p_r)
    output_pairs = [
        (port, output_mapping_l.get(port, port),
         output_mapping_r.get(port, port))
        for port in ports
    ]
    try:
        result = audit_engine.audit_equivalence(
            nl_l, par_l, nl_r, par_r, output_pairs,
            (rec["left_revision"], rec["right_revision"]))
    except audit_engine.AuditScaleLimit as e:
        result = {"status": "unknown", "equivalent": None,
                  "reason": "scale_limit",
                  "message": f"超过审计枚举规模上限，无法判定：{e}",
                  "horizon": par_l.horizon}
    result["composition"] = rec["comp_name"]
    result["revisions"] = {"left": rec["left_revision"],
                           "right": rec["right_revision"]}
    result["output_ports"] = ports
    result["output_constraints"] = raw_ports
    return {"ok": True, "id": rid, "result": result,
            "params": rec["params_json"]}


# ---------------- 验证 ----------------

def _map_params(top_body, flat, params_obj: dict):
    """把顶层输入/输出名翻译为展开后的信号名。"""
    p = dict(params_obj) if isinstance(params_obj, dict) else {}
    net = flat["netlist"]
    alias = flat["alias_index"]

    def resolve(signal):
        full = signal if signal in net["inputs"] or any(
            g["id"] == signal for g in net["gates"]) else None
        if full is None:
            # 顶层信号名在展开中可能成为某个等价类成员
            full = alias.get(signal, signal)
        return full

    # initial/final/windows 以顶层输入为键，展开后代表名一般即顶层名
    renamed = {}
    for key in ("initial", "final", "windows"):
        val = p.get(key)
        if isinstance(val, dict):
            renamed[key] = {resolve(k): v for k, v in val.items()}
    p.update(renamed)

    # outputs：顶层输出名 -> 展开代表名
    raw_outs = p.get("outputs")
    out_pairs = []
    if raw_outs is None:
        # 缺省：对每个组合输出端口检查约束，但没有任何约束字段时由下面统一
        # 报“至少需要一个约束”，错误信息使用组合端口名而非展开后的门 id。
        if not top_body.outputs:
            raise HierarchyError("组合没有输出端口，无法设置输出约束")
        raise HierarchyError(
            f"请在 params.outputs 中为组合输出端口 {top_body.outputs} "
            "至少指定一个约束 (min_pulse_width 或 max_transitions)")
    elif isinstance(raw_outs, str):
        out_pairs.append((raw_outs, {"name": raw_outs}))
    elif isinstance(raw_outs, list):
        for item in raw_outs:
            if isinstance(item, str):
                out_pairs.append((item, {"name": item}))
            elif isinstance(item, dict) and "name" in item:
                out_pairs.append((str(item["name"]), dict(item)))
            else:
                raise HierarchyError("params.outputs 条目格式非法")
    else:
        raise HierarchyError("params.outputs 必须是数组")

    top_out_set = set(top_body.outputs)
    mapped = []
    for top_name, spec in out_pairs:
        if top_name not in top_out_set:
            raise HierarchyError(
                f"参数输出 {top_name} 不是组合的输出端口 {sorted(top_out_set)}")
        pw = spec.get("min_pulse_width")
        k = spec.get("max_transitions")
        if pw is None and k is None:
            raise HierarchyError(
                f"组合输出 {top_name} 至少需要一个约束 "
                "(min_pulse_width 或 max_transitions)")
        flat_name = alias.get(top_name, top_name)
        spec = dict(spec)
        spec["name"] = flat_name
        mapped.append(spec)
    p["outputs"] = mapped
    return p


def analyze(name: str, params_obj: dict, comp_version: int | None = None) -> dict:
    cdoc = store.get_composition_doc(name)
    if cdoc is None:
        raise HierarchyError(f"组合 {name} 不存在，请先创建并保存组合网表")
    top_body = parse_body(cdoc, "composition", _lookup_doc)
    flat = flatten_hier(name, top_body, lambda m, v: get_body(m, v))
    net = flat["netlist"]
    # 顶层门补登 node_info（顶层为模块时可能直接含门；组合无门）
    for g in net["gates"]:
        flat["nodes"].setdefault(g["id"], {
            "kind": "gate", "local": g["id"], "instance_path": None,
            "instance_id": None, "module": None, "version": None,
            "delay": g["delay"]})

    mapped_params = _map_params(top_body, flat, params_obj)
    nl = parse_netlist(net)
    params = parse_params(nl, mapped_params)

    try:
        result = verify(nl, params)
    except ScaleLimit as e:
        result = {
            "status": "unknown", "reason": "scale_limit",
            "message": f"超过规模上限，无法判定：{e}",
            "limits": {"max_gates": LIMIT_GATES, "max_nodes": LIMIT_NODES,
                       "max_horizon": LIMIT_HORIZON, "max_cells": LIMIT_CELLS},
            "horizon": params.horizon,
        }
    annotate_result(result, flat)
    versions = flat["module_versions"]
    cver = comp_version or _composition_version(name)
    record = store.save_hier_analysis(name, cver, net, mapped_params,
                                      versions, result)
    return {"record": record, "result": result,
            "composition": {"name": name, "version": cver},
            "module_versions": versions,
            "flatten": _flat_brief(flat)}


def replay(rid: int) -> dict:
    rec = store.get_hier_analysis(rid)
    if rec is None:
        raise HierarchyError(f"层次分析记录 {rid} 不存在")
    nl = parse_netlist(rec["flat_json"])
    params = parse_params(nl, rec["params_json"])
    try:
        result = verify(nl, params)
    except ScaleLimit as e:
        result = {"status": "unknown", "reason": "scale_limit",
                  "message": f"超过规模上限，无法判定：{e}",
                  "horizon": params.horizon}
    # 用快照的模块版本重建展开上下文，重新挂层次标注（实例路径/端口映射/
    # race_paths 与波形节点）；若库中模块版本仍在，重放与首次分析完全同源。
    flat_ctx = _rebuild_flat_context(rec)
    if flat_ctx is not None:
        annotate_result(result, flat_ctx)
    else:
        old = rec["result_json"] or {}
        if old.get("hierarchy") is not None:
            result["hierarchy"] = old["hierarchy"]
        if old.get("bad_output_port") is not None:
            result["bad_output_port"] = old["bad_output_port"]
    return {"ok": True, "id": rid, "result": result,
            "module_versions": rec["versions_json"],
            "composition": {"name": rec["comp_name"],
                            "version": rec["comp_version"]}}


def _rebuild_flat_context(rec: dict):
    """从层次分析快照（展开网表 + 组合文档 + 版本映射）重建 flatten 结果。"""
    cdoc = store.get_composition_doc(rec["comp_name"])
    if cdoc is None:
        return None
    try:
        top = parse_body(cdoc, "composition", _lookup_doc)
        flat = flatten_hier(rec["comp_name"], top,
                            lambda m, v: get_body(m, v))
    except HierarchyError:
        return None
    # 组合可能已修订到新版本；若重展开与快照不一致则不重挂，退回存储的标注
    if _canon_json(flat["netlist"]) != _canon_json(rec["flat_json"]):
        return None
    return flat


# ---------------- 结果标注：实例路径 + 端口映射 ----------------

def annotate_result(result: dict, flat: dict) -> dict:
    """就地给 race_paths / waves / 记录节点补充层次信息。"""
    nodes = flat["nodes"]
    alias = flat["alias_index"]

    def info(node):
        n = alias.get(node, node)
        return nodes.get(n, {"kind": "anon", "crossings": []})

    def route_str(node):
        """人类可读的完整来源：顶层名 ← 实例路径(端口映射)..."""
        n = alias.get(node, node)
        inf = nodes.get(n)
        chain = [node]
        if inf and inf.get("crossings"):
            for c in inf["crossings"]:
                who = c["instance_path"] or c["instance_id"]
                chain.append(f"{who}.{c['port']}")
        return chain

    result["hierarchy"] = {
        "instances": flat["instances"],
        "module_versions": flat["module_versions"],
        "statistics": flat["statistics"],
        "node_catalog": _node_catalog(flat),
        "output_ports": dict(flat.get("output_mapping", {})),
    }
    # 扁平 bad_output（门全局 id）→ 组合输出端口名（若有）
    bad = result.get("bad_output")
    if bad:
        for top_port, rep in result["hierarchy"]["output_ports"].items():
            if rep == bad:
                result["bad_output_port"] = top_port
                break

    for wv in result.get("waves", []):
        inf = info(wv["node"])
        wv["path"] = wv["node"]
        wv["route"] = route_str(wv["node"])
        wv["origin"] = _origin(inf)
        wv["crossings"] = inf.get("crossings", [])

    ce = result.get("counterexample")
    if ce:
        ce["node_paths"] = {
            n: {"route": route_str(n), "origin": _origin(info(n))}
            for n in list(ce.get("input_transition_times", {}).keys())
        }
        for path in ce.get("race_paths", []):
            for hop in path.get("hops", []):
                n = hop["node"]
                inf = info(n)
                hop["path"] = n
                hop["route"] = route_str(n)
                hop["origin"] = _origin(inf)
                if hop.get("gate") and inf.get("module"):
                    hop["instance_path"] = inf.get("instance_path")
                    hop["instance_id"] = inf.get("instance_id")
                    hop["module"] = inf.get("module")
                    hop["module_version"] = inf.get("version")
        # joint 扇入同样补充路径（在 hop 上单独给出映射表）
        for path in ce.get("race_paths", []):
            for hop in path.get("hops", []):
                joints = hop.get("joint") or []
                hop["joint_paths"] = {j: {"path": alias.get(j, j),
                                          "route": route_str(j)}
                                      for j in joints}
        # chosen_delays 的键保持扁平 id，另给一份带路径的明细
        cd = ce.get("chosen_delays", {})
        ce["chosen_delays_paths"] = {
            gid: {"route": route_str(gid), "origin": _origin(info(gid))}
            for gid in cd
        }
    return result


def _origin(inf: dict) -> dict:
    return {
        "kind": inf.get("kind"),
        "local": inf.get("local"),
        "instance_path": inf.get("instance_path"),
        "instance_id": inf.get("instance_id"),
        "module": inf.get("module"),
        "version": inf.get("version"),
        "delay": inf.get("delay"),
    }


def _node_catalog(flat: dict) -> dict:
    out = {}
    for node, inf in flat["nodes"].items():
        out[node] = {
            "kind": inf.get("kind"),
            "local": inf.get("local"),
            "instance_path": inf.get("instance_path"),
            "instance_id": inf.get("instance_id"),
            "module": inf.get("module"),
            "version": inf.get("version"),
            "delay": inf.get("delay"),
            "crossings": inf.get("crossings", []),
        }
    # 别名成员也可查
    for member, rep in flat["alias_index"].items():
        if member != rep and member not in out:
            base = dict(out.get(rep, {}))
            base["alias_of"] = rep
            out[member] = base
    return out


def _flat_brief(flat: dict) -> dict:
    net = flat["netlist"]
    return {
        "n_inputs": len(net["inputs"]),
        "n_gates": len(net["gates"]),
        "n_outputs": len(net["outputs"]),
        "instances": flat["instances"],
        "statistics": flat["statistics"],
        "module_versions": flat["module_versions"],
        "output_mapping": {o: flat["alias_index"].get(o, o)
                           for o in flat["outputs"]},
    }


# ---------------- 小工具 ----------------

def _name_version(doc: dict) -> tuple[str, str]:
    if not isinstance(doc, dict):
        raise HierarchyError("模块文档必须是 JSON 对象")
    name = doc.get("name")
    version = doc.get("version")
    if not isinstance(name, str) or not name.strip():
        raise HierarchyError("模块需要唯一 name")
    if version is None or not isinstance(version, (str, int)) \
            or isinstance(version, bool) or not str(version).strip():
        raise HierarchyError("模块需要不可为空的 version（字符串或整数）")
    return name.strip(), str(version)


def _body_summary(body) -> dict:
    return {
        "inputs": list(body.inputs),
        "outputs": list(body.outputs),
        "n_gates": len(body.gates),
        "n_instances": len(body.instances),
        "order": list(body.body_order),
        "references": sorted(f"{m}@{v}" for m, v in body.refs),
    }


def _composition_version(name: str) -> int:
    for c in store.list_compositions():
        if c["name"] == name:
            return c["version"]
    return 1


def _canon_json(doc: dict) -> str:
    return json.dumps(doc, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))

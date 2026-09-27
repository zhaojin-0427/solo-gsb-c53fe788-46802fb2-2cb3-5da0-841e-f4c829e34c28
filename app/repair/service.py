"""修复方案搜索业务服务（⑦）。

从已保存的 **unsafe** 分析记录（扁平 /api/records 或层次
/api/hierarchy/analyses）加载网表、参数与原反例，按用户可调范围/预算
调用 engine.search，落库审计；支持重放、导出与“预览为新参数版本”
（把候选网表+参数交给既有 /api/analyze 存一条新记录，不覆盖原记录）。
"""

from __future__ import annotations

import json

from ..models import parse_netlist
from ..params import parse_params
from ..verifier.z3verifier import ScaleLimit, verify
from . import engine, store as repair_store
from .. import storage as flat_store
from ..hierarchy import store as hier_store


class RepairServiceError(ValueError):
    """修复搜索服务层错误（400）。"""


def _load_source(source_kind: str, source_id: int) -> dict:
    if source_kind == "flat":
        rec = flat_store.get_analysis(source_id)
    elif source_kind == "hier":
        rec = hier_store.get_hier_analysis(source_id)
    else:
        raise RepairServiceError("source_kind 必须是 flat 或 hier")
    if rec is None:
        raise RepairServiceError(f"源记录 {source_kind}#{source_id} 不存在")
    result = rec["result_json"]
    if not isinstance(result, dict) or result.get("status") != "unsafe":
        raise RepairServiceError(
            f"源记录 {source_kind}#{source_id} 不是 unsafe 违规记录，"
            "无法据此驱动修复搜索")
    netlist = rec["netlist_json"] if source_kind == "flat" else rec["flat_json"]
    params = rec["params_json"]
    return {"kind": source_kind, "id": source_id, "record": rec,
            "netlist": netlist, "params": params, "result": result}


def _race_paths(result: dict) -> list[dict]:
    ce = result.get("counterexample") or {}
    return list(ce.get("race_paths") or [])


def run_search(source_kind: str, source_id: int, payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise RepairServiceError("请求体必须是 JSON 对象")
    source = _load_source(source_kind, source_id)
    nl = parse_netlist(source["netlist"])
    params = parse_params(nl, source["params"])

    spec = engine.parse_search_request(payload)

    audit: list[dict] = []
    try:
        result = engine.search(
            nl, params, spec,
            source_race_paths=_race_paths(source["result"]),
            bad_output=source["result"].get("bad_output"),
            audit=audit)
    except engine.RepairScaleLimit as e:
        result = {
            "status": "unknown", "reason": "scale_limit",
            "message": f"修复搜索超过规模上限，无法判定：{e}",
            "plans": [], "min_change_evidence": None,
            "unknown_reasons": {"scale_limit": 1},
            "uncovered": {"plans_with_pending_after_horizon": 0, "items": []},
            "statistics": {"audit": audit},
            "horizon": params.horizon,
            "limits": {"max_gates": engine.REPAIR_MAX_GATES,
                       "max_horizon": engine.REPAIR_MAX_HORIZON},
        }

    result["audit_log"] = audit
    source_race = _race_paths(source["result"])
    result["source"] = {
        "kind": source_kind, "id": source_id,
        "netlist_name": source["netlist"].get("name"),
        "bad_time": source["result"].get("bad_time"),
        "bad_output": source["result"].get("bad_output"),
        "violation": source["result"].get("violation"),
        "race_path_count":
            (source["result"].get("counterexample") or {}).get("race_path_count"),
        "race_paths": source_race,
    }
    result["request"] = _canonical_request(spec)

    summary = {
        "netlist_name": source["netlist"].get("name"),
        "bad_output": source["result"].get("bad_output"),
        "bad_time": source["result"].get("bad_time"),
    }
    saved = repair_store.save_search(
        source_kind, source_id, summary, result["request"],
        source["netlist"], source["params"], result)
    return {"ok": True, "record": saved, "result": result,
            "source": result["source"]}


def _canonical_request(spec: dict) -> dict:
    """用于落库/重放/去重的规范请求（端点已排序、无多余字段）。

    parse_search_request 的 gates/windows 为 {id: {...}} 字典、outputs 为
    {name: {pw/k}} 字典，这里展开成对外的数组形式。
    """
    gates = [{"id": gid, "range": [g["lo"], g["hi"]]}
              for gid, g in sorted(spec["gates"].items())]
    windows = [{"name": n, "range": [w["tmin"], w["tmax"]]}
               for n, w in sorted(spec["windows"].items())]
    outputs = []
    for name, e in sorted(spec["outputs"].items()):
        item = {"name": name}
        if e.get("pw") is not None:
            item["min_pulse_width_range"] = list(e["pw"])
        if e.get("k") is not None:
            item["max_transitions_range"] = list(e["k"])
        outputs.append(item)
    return {
        "gates": gates,
        "windows": windows,
        "outputs": outputs,
        "budget": {"max_changes": spec["max_changes"],
                  "max_total_deviation": spec["max_deviation"]},
    }


def replay(rid: int) -> dict:
    """用记录中的源快照与规范请求重新搜索（重放结果应与首次一致）。"""
    rec = repair_store.get_search(rid)
    if rec is None:
        raise RepairServiceError(f"修复搜索记录 {rid} 不存在")
    nl = parse_netlist(rec["netlist_json"])
    params = parse_params(nl, rec["params_json"])
    source_result = rec["result_json"].get("source") or {}
    spec = engine.parse_search_request(rec["request_json"])
    audit: list[dict] = []
    result = engine.search(
        nl, params, spec,
        source_race_paths=source_result.get("race_paths") or [],
        bad_output=source_result.get("bad_output"), audit=audit)
    result["audit_log"] = audit
    result["source"] = rec["result_json"].get("source")
    result["request"] = rec["request_json"]
    same = _same_outcome(rec["result_json"], result)
    return {"ok": True, "id": rid, "result": result,
            "replay_consistent": same}


def _same_outcome(a: dict, b: dict) -> bool:
    ka = [(p["changes"], p["total_deviation"],
            p["risk_eliminated_at"], p["key"]) for p in a.get("plans", [])]
    kb = [(p["changes"], p["total_deviation"],
            p["risk_eliminated_at"], p["key"]) for p in b.get("plans", [])]
    return a.get("status") == b.get("status") and ka == kb


def materialize(rid: int, plan_key: str) -> dict:
    """把某条方案预览为新参数版本：交既有 /api/analyze 存一条新记录，
    不覆盖原违规记录。返回新分析记录与验证结果。"""
    rec = repair_store.get_search(rid)
    if rec is None:
        raise RepairServiceError(f"修复搜索记录 {rid} 不存在")
    plan = next((p for p in rec["result_json"].get("plans", [])
                if p["key"] == plan_key), None)
    if plan is None:
        raise RepairServiceError("该方案不在搜索记录中（可能已被 Pareto 裁剪）")
    nl = plan["netlist"]
    params = plan["params"]
    # 复用既有解析 + 验证流水线，并以哈希去重方式另存新分析记录
    parsed_nl = parse_netlist(nl)
    parsed_p = parse_params(parsed_nl, params)
    try:
        verify_result = verify(parsed_nl, parsed_p)
    except ScaleLimit as e:
        verify_result = {"status": "unknown", "reason": "scale_limit",
                        "message": str(e), "horizon": parsed_p.horizon}
    saved = flat_store.save_analysis(nl, params, verify_result)
    return {"ok": True, "repair_search_id": rid, "plan_key": plan_key,
            "analysis_record": saved, "result": verify_result,
            "note": "已另存为新分析记录（原违规记录未改动）"}


def export_all(limit: int = 100) -> dict:
    records = []
    for item in repair_store.list_searches(limit):
        full = repair_store.get_search(item["id"])
        if full:
            records.append(full)
    return {"format": "repair-search-records/v1", "records": records}

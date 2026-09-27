"""层次化模块库与组合验证的 HTTP 路由（挂在 /api/hierarchy 下）。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..models import NetlistError
from . import service, store
from .models import HierarchyError

router = APIRouter(prefix="/api/hierarchy", tags=["hierarchy"])


def _err(status: int, msg: str):
    return JSONResponse(status_code=status,
                        content={"ok": False, "error": str(msg)})


@router.post("/modules/validate")
async def validate_module(req: Request):
    try:
        return service.validate_module_doc(await req.json())
    except HierarchyError as e:
        return _err(400, e)


@router.get("/modules")
async def modules():
    return {"modules": store.list_modules()}


@router.get("/modules/{name}/{version}")
async def one_module(name: str, version: str):
    doc = store.get_module_doc(name, version)
    if doc is None:
        return _err(404, f"模块 {name}@{version} 不存在")
    return {"ok": True, "module": doc}


@router.post("/modules")
async def create_module(req: Request):
    try:
        return {"ok": True, **service.register_module(await req.json())}
    except service.Conflict as e:
        return _err(409, e)
    except HierarchyError as e:
        return _err(400, e)


@router.post("/import")
async def import_bundle(req: Request):
    try:
        return {"ok": True, **service.import_bundle(await req.json())}
    except service.Conflict as e:
        return _err(409, e)
    except HierarchyError as e:
        return _err(400, e)


@router.get("/export")
async def export_bundle():
    return service.export_bundle()


# ---------------- 组合 ----------------

@router.post("/compositions/validate")
async def validate_composition(req: Request):
    try:
        return service.validate_composition(await req.json())
    except HierarchyError as e:
        return _err(400, e)


@router.get("/compositions")
async def compositions():
    return {"compositions": store.list_compositions()}


@router.put("/compositions/{name}")
async def put_composition(name: str, req: Request):
    try:
        doc = await req.json()
        if not isinstance(doc, dict):
            raise HierarchyError("组合文档必须是 JSON 对象")
        doc["name"] = name
        return {"ok": True, **service.save_composition(doc)}
    except HierarchyError as e:
        return _err(400, e)


@router.get("/compositions/{name}")
async def get_composition(name: str):
    doc = store.get_composition_doc(name)
    if doc is None:
        return _err(404, f"组合 {name} 不存在")
    meta = next((c for c in store.list_compositions() if c["name"] == name),
                None)
    return {"ok": True, "composition": doc, "meta": meta}


@router.delete("/compositions/{name}")
async def remove_composition(name: str):
    try:
        service.delete_composition(name)
        return {"ok": True}
    except HierarchyError as e:
        return _err(404, e)


@router.post("/compositions/validate-flatten")
async def validate_flatten_body(req: Request):
    """保存前干跑：给出展开结果与统计，不落库。"""
    try:
        payload = await req.json()
        doc = payload if isinstance(payload, dict) and "inputs" in payload \
            else payload.get("composition")
        flat = service.flatten_dry(doc)
        return {"ok": True, "flatten": _flat_public(flat)}
    except HierarchyError as e:
        return _err(400, e)


@router.get("/compositions/{name}/flatten")
async def flatten(name: str):
    try:
        flat = service.flatten_saved(name)
        return {"ok": True, **_flat_public(flat)}
    except HierarchyError as e:
        return _err(400, e)


@router.get("/compositions/{name}/revisions")
async def composition_revisions(name: str):
    try:
        return {"ok": True, "revisions": service.list_revisions(name)}
    except service.RevisionNotFound as e:
        return _err(404, e)
    except HierarchyError as e:
        return _err(400, e)


@router.get("/compositions/{name}/revisions/{revision}")
async def one_composition_revision(name: str, revision: int):
    try:
        return {"ok": True, "revision": service.get_revision(name, revision)}
    except HierarchyError as e:
        return _err(404, e)


@router.post("/compositions/{name}/audits")
async def create_behavior_audit(name: str, req: Request):
    try:
        payload = await req.json()
        if not isinstance(payload, dict):
            return _err(400, "审计请求必须是 JSON 对象")
        left = int(payload.get("left_revision"))
        right = int(payload.get("right_revision"))
        params = payload.get("params", {})
        return service.behavior_audit(name, left, right, params)
    except service.RevisionNotFound as e:
        return _err(404, e)
    except HierarchyError as e:
        return _err(400, e)
    except NetlistError as e:
        return _err(400, e)
    except (KeyError, TypeError, ValueError) as e:
        return _err(400, f"审计请求格式非法：{e}")


@router.get("/audits")
async def behavior_audits(limit: int = 100):
    return {"records": store.list_behavior_audits(limit)}


@router.get("/audits/export")
async def export_behavior_audits(limit: int = 100):
    records = []
    for item in store.list_behavior_audits(limit):
        full = store.get_behavior_audit(item["id"])
        if full:
            records.append(full)
    return {"format": "behavior-audit-records/v1", "records": records}


@router.get("/audits/{rid}")
async def one_behavior_audit(rid: int):
    rec = store.get_behavior_audit(rid)
    if rec is None:
        return _err(404, f"行为审计记录 {rid} 不存在")
    return {"ok": True, "record": rec}


@router.post("/audits/{rid}/replay")
async def replay_behavior_audit(rid: int):
    try:
        return service.replay_behavior_audit(rid)
    except HierarchyError as e:
        return _err(404, e)
    except NetlistError as e:
        return _err(400, e)


@router.post("/compositions/{name}/analyze")
async def analyze(name: str, req: Request):
    try:
        payload = await req.json()
        params = payload.get("params", {}) if isinstance(payload, dict) else {}
        d = service.analyze(name, params)
        return {"ok": True, **d}
    except HierarchyError as e:
        return _err(400, e)
    except NetlistError as e:
        return _err(400, e)


# ---------------- 层次分析记录 ----------------

@router.get("/analyses")
async def analyses(limit: int = 100):
    return {"records": store.list_hier_analyses(limit)}


@router.get("/analyses/{rid}")
async def one_analysis(rid: int):
    rec = store.get_hier_analysis(rid)
    if rec is None:
        return _err(404, f"层次分析记录 {rid} 不存在")
    return {"ok": True, "record": rec}


@router.post("/analyses/{rid}/replay")
async def replay(rid: int):
    try:
        return service.replay(rid)
    except HierarchyError as e:
        return _err(404, e)
    except NetlistError as e:
        return _err(400, e)


@router.get("/samples")
async def samples():
    import json
    import os
    data_dir = os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "data")
    out = []
    for fn in sorted(os.listdir(data_dir)):
        if fn.startswith("hier_sample_") and fn.endswith(".json"):
            with open(os.path.join(data_dir, fn), encoding="utf-8") as f:
                out.append(json.load(f))
    return {"samples": out}


def _flat_public(flat: dict) -> dict:
    return {
        "netlist": flat["netlist"],
        "inputs": flat["inputs"],
        "outputs": flat["outputs"],
        "output_mapping": flat.get("output_mapping", {}),
        "instances": flat["instances"],
        "nodes": flat["nodes"],
        "alias_index": flat["alias_index"],
        "module_versions": flat["module_versions"],
        "statistics": flat["statistics"],
    }

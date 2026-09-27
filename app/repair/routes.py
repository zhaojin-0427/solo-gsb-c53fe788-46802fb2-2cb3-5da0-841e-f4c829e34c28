"""反例驱动修复方案搜索（⑦）的 HTTP 路由（挂在 /api/repairs 下）。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..models import NetlistError
from . import engine, service, store as repair_store
from .service import RepairServiceError

router = APIRouter(prefix="/api/repairs", tags=["repairs"])


def _err(status: int, msg: str):
    return JSONResponse(status_code=status,
                        content={"ok": False, "error": str(msg)})


@router.get("/limits")
async def repair_limits():
    return {
        "max_gates": engine.REPAIR_MAX_GATES,
        "max_horizon": engine.REPAIR_MAX_HORIZON,
        "solver_timeout_ms": engine.REPAIR_SOLVER_TIMEOUT_MS,
        "max_models": engine.REPAIR_MAX_MODELS,
        "max_bad_keys": engine.REPAIR_MAX_BAD_KEYS,
        "max_safe_per_level": engine.REPAIR_MAX_SAFE_PER_LEVEL,
        "max_verifications": engine.REPAIR_MAX_VERIFICATIONS,
    }


@router.get("/sources")
async def sources():
    """可选源：扁平与层次库中状态为 unsafe 的分析记录。"""
    return {"flat": _flat_unsafe(), "hier": _hier_unsafe()}


def _flat_unsafe() -> list[dict]:
    from .. import storage as flat_store
    out = []
    for r in flat_store.list_analyses(200):
        if r.get("status") == "unsafe":
            out.append({"id": r["id"], "name": r.get("netlist_name"),
                        "netlist_hash": r.get("netlist_hash"),
                        "params_hash": r.get("params_hash"),
                        "created_at": r.get("created_at")})
    return out


def _hier_unsafe() -> list[dict]:
    from ..hierarchy import store as hs
    out = []
    for r in hs.list_hier_analyses(200):
        if r.get("status") == "unsafe":
            out.append({"id": r["id"], "comp_name": r.get("comp_name"),
                        "comp_version": r.get("comp_version"),
                        "created_at": r.get("created_at")})
    return out


@router.post("/search/{source_kind}/{source_id}")
async def search(source_kind: str, source_id: int, req: Request):
    try:
        payload = await req.json()
    except Exception:
        return _err(400, "请求体必须是 JSON")
    try:
        return service.run_search(source_kind, source_id, payload)
    except RepairServiceError as e:
        return _err(400, e)
    except engine.RepairError as e:
        return _err(400, e)
    except NetlistError as e:
        return _err(400, e)


@router.get("/records")
async def records(limit: int = 100):
    return {"records": repair_store.list_searches(limit)}


@router.get("/records/{rid}")
async def one_record(rid: int):
    rec = repair_store.get_search(rid)
    if rec is None:
        return _err(404, f"修复搜索记录 {rid} 不存在")
    return {"ok": True, "record": rec}


@router.post("/records/{rid}/replay")
async def replay(rid: int):
    try:
        return service.replay(rid)
    except RepairServiceError as e:
        return _err(404, e)
    except engine.RepairError as e:
        return _err(400, e)
    except NetlistError as e:
        return _err(400, e)


@router.post("/records/{rid}/materialize")
async def materialize(rid: int, req: Request):
    try:
        payload = await req.json()
        plan_key = payload.get("plan_key") if isinstance(payload, dict) else None
        if not isinstance(plan_key, str):
            return _err(400, "需要 plan_key")
        return service.materialize(rid, plan_key)
    except RepairServiceError as e:
        return _err(404, e)
    except NetlistError as e:
        return _err(400, e)


@router.get("/export")
async def export(limit: int = 100):
    return service.export_all(limit)

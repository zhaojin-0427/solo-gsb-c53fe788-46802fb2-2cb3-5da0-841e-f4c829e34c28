"""组合电路冒险验证台 —— FastAPI 后端入口。

接口：
  GET  /                      前端页面（静态）
  POST /api/validate          仅校验网表/参数，返回拓扑与自动时间上界
  POST /api/analyze           执行 z3 验证并存档
  GET  /api/records           历史分析记录列表
  GET  /api/records/{id}      单条记录（含完整结果）
  GET  /api/samples           内置示例

层次化模块库与组合验证接口统一挂在 /api/hierarchy（见 app/hierarchy/routes.py）。
"""

from __future__ import annotations

import json
import os

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .hierarchy.audit import (
    AUDIT_MAX_GATES,
    AUDIT_MAX_HORIZON,
    AUDIT_MAX_INPUT_ASSIGNMENTS,
    AUDIT_MAX_TRAJECTORIES,
    AUDIT_TIMEOUT_S,
)
from .hierarchy.routes import router as hierarchy_router
from .models import NetlistError, parse_netlist
from .params import parse_params
from .repair import engine as repair_engine
from .repair.routes import router as repair_router
from .storage import get_analysis, list_analyses, save_analysis
from .verifier.z3verifier import (
    LIMIT_CELLS,
    LIMIT_GATES,
    LIMIT_HORIZON,
    LIMIT_NODES,
    SOLVER_TIMEOUT_MS,
    ScaleLimit,
    verify,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DATA_DIR = os.path.join(BASE_DIR, "data")

app = FastAPI(title="组合电路冒险验证台", version="1.2")
app.include_router(hierarchy_router)
app.include_router(repair_router)


def _extract(payload: dict) -> tuple[dict, dict]:
    nl = payload.get("netlist")
    p = payload.get("params", {})
    if not isinstance(nl, dict) or not isinstance(p, dict):
        raise NetlistError("请求体需包含 netlist 对象与 params 对象")
    return nl, p


@app.exception_handler(NetlistError)
async def _netlist_err(_: Request, exc: NetlistError):
    return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})


@app.get("/api/limits")
async def get_limits():
    return {
        "max_gates": LIMIT_GATES,
        "max_nodes": LIMIT_NODES,
        "max_horizon": LIMIT_HORIZON,
        "max_cells": LIMIT_CELLS,
        "solver_timeout_ms": SOLVER_TIMEOUT_MS,
        "audit_max_gates": AUDIT_MAX_GATES,
        "audit_max_horizon": AUDIT_MAX_HORIZON,
        "audit_max_input_assignments": AUDIT_MAX_INPUT_ASSIGNMENTS,
        "audit_max_trajectories": AUDIT_MAX_TRAJECTORIES,
        "audit_timeout_seconds": AUDIT_TIMEOUT_S,
        "repair_max_gates": repair_engine.REPAIR_MAX_GATES,
        "repair_max_horizon": repair_engine.REPAIR_MAX_HORIZON,
        "repair_solver_timeout_ms": repair_engine.REPAIR_SOLVER_TIMEOUT_MS,
        "repair_max_verifications": repair_engine.REPAIR_MAX_VERIFICATIONS,
    }


@app.post("/api/validate")
async def validate(req: Request):
    payload = await req.json()
    nl_obj, p_obj = _extract(payload)
    nl = parse_netlist(nl_obj)
    params = parse_params(nl, p_obj)
    return {
        "ok": True,
        "summary": {
            "n_inputs": len(nl.inputs),
            "n_gates": len(nl.gates),
            "order": nl.order,
            "outputs": nl.outputs,
            "initial_values": _steady_state(nl, params.initial),
            "horizon": params.horizon,
            "longest_static_path": nl.longest_path_delay(),
        },
    }


def _steady_state(nl, initial: dict[str, int]) -> dict[str, int]:
    from .models import eval_gate
    vals = dict(initial)
    for gid in nl.order:
        g = nl.gate_map[gid]
        vals[gid] = eval_gate(g.type, [vals[i] for i in g.inputs])
    return vals


@app.post("/api/analyze")
async def analyze(req: Request):
    payload = await req.json()
    nl_obj, p_obj = _extract(payload)
    nl = parse_netlist(nl_obj)
    params = parse_params(nl, p_obj)
    try:
        result = verify(nl, params)
    except ScaleLimit as e:
        result = {
            "status": "unknown",
            "reason": "scale_limit",
            "message": f"超过规模上限，无法判定：{e}",
            "limits": {
                "max_gates": LIMIT_GATES, "max_nodes": LIMIT_NODES,
                "max_horizon": LIMIT_HORIZON, "max_cells": LIMIT_CELLS,
            },
            "horizon": params.horizon,
        }
    record = save_analysis(nl_obj, p_obj, result)
    return {"ok": True, "record": record, "result": result}


@app.get("/api/records")
async def records(limit: int = 100):
    return {"records": list_analyses(limit)}


@app.get("/api/records/{rid}")
async def one_record(rid: int):
    rec = get_analysis(rid)
    if rec is None:
        return JSONResponse(status_code=404,
                            content={"ok": False, "error": "记录不存在"})
    return {"ok": True, "record": rec}


@app.get("/api/samples")
async def samples():
    out = []
    for fn in sorted(os.listdir(DATA_DIR)):
        if fn.startswith("sample_") and fn.endswith(".json"):
            with open(os.path.join(DATA_DIR, fn), encoding="utf-8") as f:
                out.append(json.load(f))
    return {"samples": out}


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

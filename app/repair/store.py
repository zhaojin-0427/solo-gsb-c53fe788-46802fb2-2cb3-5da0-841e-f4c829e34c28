"""修复方案搜索的 SQLite 持久化（⑦）。

每条搜索记录保存完整快照：
* 源记录（扁平 analyses 或层次 hier_analyses）的网表、参数、原反例；
* 可调范围与预算（规范请求，供确定性重放）；
* 完整结果（至多 10 条 Pareto 方案、最小改动证据、unknown 原因、
  未覆盖边界、审计日志、统计）。

按 (源类型, 源记录 id, 请求规范哈希) 去重 upsert，便于重复发起复用。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager

DB_PATH = os.getenv("HAZARD_DB", "/workspace/data/hazard.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS repair_searches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_kind     TEXT NOT NULL,
    source_id       INTEGER NOT NULL,
    spec_hash       TEXT NOT NULL,
    source_summary TEXT NOT NULL,
    request_json    TEXT NOT NULL,
    netlist_json   TEXT NOT NULL,
    params_json    TEXT NOT NULL,
    result_json    TEXT NOT NULL,
    status         TEXT NOT NULL,
    created_at     REAL NOT NULL,
    UNIQUE(source_kind, source_id, spec_hash)
);
"""


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def spec_hash(request: dict) -> str:
    return hashlib.sha256(_canon(request)).hexdigest()[:16]


@contextmanager
def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _conn() as conn:
        conn.executescript(_SCHEMA)


def save_search(source_kind: str, source_id: int, source_summary: dict,
              request: dict, netlist_obj: dict, params_obj: dict,
              result: dict) -> dict:
    init_db()
    sh = spec_hash(request)
    now = time.time()
    with _conn() as conn:
        row = conn.execute(
            """SELECT id FROM repair_searches
               WHERE source_kind=? AND source_id=? AND spec_hash=?""",
            (source_kind, source_id, sh)).fetchone()
        if row:
            conn.execute(
                """UPDATE repair_searches SET source_summary=?, request_json=?,
                       netlist_json=?, params_json=?, result_json=?, status=?,
                       created_at=? WHERE id=?""",
                (json.dumps(source_summary, ensure_ascii=False),
                 json.dumps(request, ensure_ascii=False),
                 json.dumps(netlist_obj, ensure_ascii=False),
                 json.dumps(params_obj, ensure_ascii=False),
                 json.dumps(result, ensure_ascii=False),
                 result.get("status"), now, row["id"]))
            rid = row["id"]
            reused = True
        else:
            cur = conn.execute(
                """INSERT INTO repair_searches
                   (source_kind, source_id, spec_hash, source_summary, request_json,
                    netlist_json, params_json, result_json, status, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (source_kind, source_id, sh,
                 json.dumps(source_summary, ensure_ascii=False),
                 json.dumps(request, ensure_ascii=False),
                 json.dumps(netlist_obj, ensure_ascii=False),
                 json.dumps(params_obj, ensure_ascii=False),
                 json.dumps(result, ensure_ascii=False),
                 result.get("status"), now))
            rid = cur.lastrowid
            reused = False
    return {"id": rid, "spec_hash": sh, "reused": reused}


def list_searches(limit: int = 100) -> list[dict]:
    init_db()
    with _conn() as conn:
        rows = conn.execute(
            """SELECT id, source_kind, source_id, spec_hash, source_summary,
                      status, result_json, created_at FROM repair_searches
               ORDER BY created_at DESC, id DESC LIMIT ?""",
            (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["source_summary"] = json.loads(d["source_summary"])
        try:
            d["plan_count"] = len(json.loads(r["result_json"]).get("plans", []))
        except Exception:
            d["plan_count"] = 0
        out.append(d)
    return out


def get_search(rid: int) -> dict | None:
    init_db()
    with _conn() as conn:
        row = conn.execute("SELECT * FROM repair_searches WHERE id=?",
                          (rid,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    for k in ("source_summary", "request_json", "netlist_json",
               "params_json", "result_json"):
        d[k] = json.loads(d[k])
    return d

"""SQLite 持久化：分析记录按 网表哈希 + 参数哈希 留存。

同一网表（规范化 JSON）与同一参数组合的分析只存一条（去重 upsert），
便于回看历史结论；网表原文同时保存以便重新载入。
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
CREATE TABLE IF NOT EXISTS analyses (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    netlist_hash  TEXT NOT NULL,
    params_hash   TEXT NOT NULL,
    netlist_name  TEXT,
    netlist_json  TEXT NOT NULL,
    params_json   TEXT NOT NULL,
    status        TEXT NOT NULL,
    result_json   TEXT NOT NULL,
    created_at    REAL NOT NULL,
    UNIQUE(netlist_hash, params_hash)
);
"""


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def hashes(netlist_obj: dict, params_obj: dict) -> tuple[str, str]:
    return (
        hashlib.sha256(_canon(netlist_obj)).hexdigest()[:16],
        hashlib.sha256(_canon(params_obj)).hexdigest()[:16],
    )


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


def save_analysis(netlist_obj: dict, params_obj: dict, result: dict) -> dict:
    """upsert 一条分析记录，返回 {id, netlist_hash, params_hash, reused}。"""
    init_db()
    nh, ph = hashes(netlist_obj, params_obj)
    name = netlist_obj.get("name")
    now = time.time()
    with _conn() as conn:
        row = conn.execute(
            "SELECT id FROM analyses WHERE netlist_hash=? AND params_hash=?",
            (nh, ph)).fetchone()
        reused = row is not None
        if reused:
            conn.execute(
                """UPDATE analyses SET result_json=?, status=?, created_at=?
                   WHERE netlist_hash=? AND params_hash=?""",
                (json.dumps(result, ensure_ascii=False), result.get("status"),
                 now, nh, ph))
            rid = row["id"]
        else:
            cur = conn.execute(
                """INSERT INTO analyses
                   (netlist_hash, params_hash, netlist_name, netlist_json,
                    params_json, status, result_json, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (nh, ph, name,
                 json.dumps(netlist_obj, ensure_ascii=False),
                 json.dumps(params_obj, ensure_ascii=False),
                 result.get("status"),
                 json.dumps(result, ensure_ascii=False), now))
            rid = cur.lastrowid
    return {"id": rid, "netlist_hash": nh, "params_hash": ph, "reused": reused}


def list_analyses(limit: int = 100) -> list[dict]:
    init_db()
    with _conn() as conn:
        rows = conn.execute(
            """SELECT id, netlist_hash, params_hash, netlist_name, status,
                      created_at FROM analyses
               ORDER BY created_at DESC, id DESC LIMIT ?""",
            (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_analysis(rid: int) -> dict | None:
    init_db()
    with _conn() as conn:
        row = conn.execute("SELECT * FROM analyses WHERE id=?", (rid,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    for k in ("netlist_json", "params_json", "result_json"):
        d[k] = json.loads(d[k])
    return d

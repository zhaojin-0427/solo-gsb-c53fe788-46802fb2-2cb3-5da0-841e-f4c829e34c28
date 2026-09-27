"""层次模块库的 SQLite 持久化。

三张表：
* hier_modules      —— 模块版本（name, version）唯一，**只增不改不删**；
* hier_compositions          —— 顶层组合当前指针，名字唯一，版本即修订号；
* hier_composition_revisions —— 每次组合修订的不可变快照（原文、展开网表与
                                模块版本），供行为等价审计与旧记录重放；
* hier_analyses              —— 层次分析记录，按 组合 + 展开后网表哈希 + 参数哈希
                                去重，并快照展开网表与各模块版本，保证旧分析可重放；
* hier_behavior_audits       —— 两修订行为等价审计，按 组合 + 有序修订对 +
                                参数哈希去重，结果与双版本快照均可重放/导出。

模块/组合的 JSON 原文直接保存，读取后由 models.parse_body 在请求内校验
与构造 HBody（带一个小的 LRU 缓存）。
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
CREATE TABLE IF NOT EXISTS hier_modules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    version     TEXT NOT NULL,
    doc_json    TEXT NOT NULL,
    created_at  REAL NOT NULL,
    UNIQUE(name, version)
);
CREATE TABLE IF NOT EXISTS hier_compositions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    version     INTEGER NOT NULL DEFAULT 1,
    doc_json    TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS hier_composition_revisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    revision        INTEGER NOT NULL,
    doc_json        TEXT NOT NULL,
    doc_hash        TEXT NOT NULL,
    flat_json       TEXT,
    flat_hash       TEXT,
    versions_json   TEXT,
    created_at      REAL NOT NULL,
    UNIQUE(name, revision)
);
CREATE TABLE IF NOT EXISTS hier_analyses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    comp_name       TEXT NOT NULL,
    comp_version    INTEGER NOT NULL,
    flat_hash       TEXT NOT NULL,
    params_hash     TEXT NOT NULL,
    params_json     TEXT NOT NULL,
    flat_json       TEXT NOT NULL,
    versions_json   TEXT NOT NULL,
    status          TEXT NOT NULL,
    result_json     TEXT NOT NULL,
    created_at      REAL NOT NULL,
    UNIQUE(comp_name, flat_hash, params_hash)
);
CREATE TABLE IF NOT EXISTS hier_behavior_audits (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    comp_name         TEXT NOT NULL,
    left_revision     INTEGER NOT NULL,
    right_revision    INTEGER NOT NULL,
    params_hash       TEXT NOT NULL,
    left_flat_hash    TEXT NOT NULL,
    right_flat_hash   TEXT NOT NULL,
    params_json       TEXT NOT NULL,
    left_doc_json     TEXT NOT NULL,
    right_doc_json    TEXT NOT NULL,
    left_flat_json    TEXT NOT NULL,
    right_flat_json   TEXT NOT NULL,
    left_versions     TEXT NOT NULL,
    right_versions    TEXT NOT NULL,
    status            TEXT NOT NULL,
    result_json       TEXT NOT NULL,
    created_at        REAL NOT NULL,
    UNIQUE(comp_name, left_revision, right_revision, params_hash)
);
"""


class Conflict(Exception):
    """唯一约束冲突（模块版本已存在等）。"""


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def hashes(flat_netlist: dict, params_obj: dict) -> tuple[str, str]:
    return (
        hashlib.sha256(_canon(flat_netlist)).hexdigest()[:16],
        hashlib.sha256(_canon(params_obj)).hexdigest()[:16],
    )


@contextmanager
def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _conn() as conn:
        conn.executescript(_SCHEMA)


# ---------------- 模块（不可变版本） ----------------

def create_module(name: str, version: str, doc: dict) -> dict:
    init_db()
    now = time.time()
    with _conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM hier_modules WHERE name=? AND version=?",
            (name, version)).fetchone()
        if exists:
            raise Conflict(f"模块 {name}@{version} 已存在且不可修改")
        cur = conn.execute(
            """INSERT INTO hier_modules (name, version, doc_json, created_at)
               VALUES (?,?,?,?)""",
            (name, version, json.dumps(doc, ensure_ascii=False), now))
        mid = cur.lastrowid
    return {"id": mid, "name": name, "version": version}


def get_module_doc(name: str, version: str) -> dict | None:
    init_db()
    with _conn() as conn:
        row = conn.execute(
            "SELECT doc_json FROM hier_modules WHERE name=? AND version=?",
            (name, version)).fetchone()
    return json.loads(row["doc_json"]) if row else None


def list_modules() -> list[dict]:
    init_db()
    with _conn() as conn:
        rows = conn.execute(
            """SELECT id, name, version, created_at FROM hier_modules
               ORDER BY name, version, id""").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        doc = get_module_doc(d["name"], d["version"])
        d["n_inputs"] = len(doc.get("inputs", []))
        d["n_outputs"] = len(doc.get("outputs", []))
        d["n_gates"] = len(doc.get("gates", []))
        d["n_instances"] = len(doc.get("instances", []))
        out.append(d)
    return out


# ---------------- 组合（可整体替换，版本号自增） ----------------

def doc_hash(doc: dict) -> str:
    return hashlib.sha256(_canon(doc)).hexdigest()[:16]


def _dump(value, default="{}"):
    return json.dumps(value, ensure_ascii=False) if value is not None else default


def save_composition_revision(name: str, doc: dict, flat: dict | None = None,
                              versions: dict | None = None) -> dict:
    """原子更新当前组合并保存不可变修订快照。

    新组合写修订 1；内容变化时当前版本号自增并保存对应快照；内容未变时
    仅补齐旧库缺失的当前修订快照（用于从本功能上线前数据库平滑升级）。
    """
    init_db()
    now = time.time()
    payload = json.dumps(doc, ensure_ascii=False)
    dh = hashlib.sha256(_canon(doc)).hexdigest()[:16]
    flat_payload = json.dumps(flat, ensure_ascii=False) if flat is not None else None
    flat_h = doc_hash(flat) if flat is not None else None
    versions_payload = json.dumps(versions or {}, ensure_ascii=False)

    with _conn() as conn:
        row = conn.execute(
            "SELECT id, version, doc_json FROM hier_compositions WHERE name=?",
            (name,)).fetchone()
        if row is None:
            if flat is None:
                raise ValueError("创建组合修订必须提供展开快照")
            cur = conn.execute(
                """INSERT INTO hier_compositions
                   (name, version, doc_json, created_at, updated_at)
                   VALUES (?,1,?,?,?)""",
                (name, payload, now, now))
            conn.execute(
                """INSERT INTO hier_composition_revisions
                   (name, revision, doc_json, doc_hash, flat_json, flat_hash,
                    versions_json, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (name, 1, payload, dh, flat_payload, flat_h,
                 versions_payload, now))
            return {"id": cur.lastrowid, "name": name, "version": 1,
                    "reused": False, "unchanged": False, "snapshot": True}

        if row["doc_json"] == payload:
            rev = row["version"]
            snap = conn.execute(
                "SELECT id FROM hier_composition_revisions WHERE name=? AND revision=?",
                (name, rev)).fetchone()
            snapshot = snap is not None
            if not snap:
                if flat is None:
                    raise HierarchyStoreError("旧组合缺少修订快照，需要展开结果以补齐")
                conn.execute(
                    """INSERT INTO hier_composition_revisions
                       (name, revision, doc_json, doc_hash, flat_json, flat_hash,
                        versions_json, created_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (name, rev, payload, dh, flat_payload, flat_h,
                     versions_payload, now))
                snapshot = True
            return {"id": row["id"], "name": name, "version": rev,
                    "reused": True, "unchanged": True, "snapshot": snapshot}

        if flat is None:
            raise ValueError("创建新组合修订必须提供展开快照")
        new_rev = row["version"] + 1
        conn.execute(
            """UPDATE hier_compositions SET doc_json=?, version=?, updated_at=?
               WHERE name=?""",
            (payload, new_rev, now, name))
        conn.execute(
            """INSERT INTO hier_composition_revisions
               (name, revision, doc_json, doc_hash, flat_json, flat_hash,
                versions_json, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (name, new_rev, payload, dh, flat_payload, flat_h,
             versions_payload, now))
        return {"id": row["id"], "name": name, "version": new_rev,
                "reused": True, "unchanged": False, "snapshot": True}


class HierarchyStoreError(ValueError):
    """存储层无法补齐调用方应提供的数据。"""


# 兼容旧调用：未给展开快照时只更新当前指针。新业务应使用修订快照函数。
def upsert_composition(name: str, doc: dict) -> dict:
    """按名字创建组合；内容真正变化时整体替换并 version += 1，
    内容未变则保留原修订号（reused=True, unchanged=True）。"""
    init_db()
    now = time.time()
    payload = json.dumps(doc, ensure_ascii=False)
    with _conn() as conn:
        row = conn.execute(
            "SELECT id, version, doc_json FROM hier_compositions WHERE name=?",
            (name,)).fetchone()
        if row:
            unchanged = row["doc_json"] == payload
            if unchanged:
                return {"id": row["id"], "name": name,
                        "version": row["version"], "reused": True,
                        "unchanged": True}
            conn.execute(
                """UPDATE hier_compositions SET doc_json=?, version=version+1,
                       updated_at=? WHERE name=?""",
                (payload, now, name))
            return {"id": row["id"], "name": name,
                    "version": row["version"] + 1, "reused": True,
                    "unchanged": False}
        cur = conn.execute(
            """INSERT INTO hier_compositions
               (name, version, doc_json, created_at, updated_at)
               VALUES (?,1,?,?,?)""",
            (name, payload, now, now))
        return {"id": cur.lastrowid, "name": name, "version": 1,
                "reused": False, "unchanged": False}


def get_composition_doc(name: str) -> dict | None:
    init_db()
    with _conn() as conn:
        row = conn.execute(
            "SELECT doc_json FROM hier_compositions WHERE name=?",
            (name,)).fetchone()
    return json.loads(row["doc_json"]) if row else None


def list_compositions() -> list[dict]:
    init_db()
    with _conn() as conn:
        rows = conn.execute(
            """SELECT id, name, version, created_at, updated_at
               FROM hier_compositions ORDER BY updated_at DESC, id DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


def delete_composition(name: str) -> bool:
    init_db()
    with _conn() as conn:
        cur = conn.execute("DELETE FROM hier_compositions WHERE name=?",
                           (name,))
        # 当前组合删除后移除其可变修订列表；行为审计记录已内嵌快照，仍保留
        # 并可查看/重放，也不会阻碍同名组合重新从修订 1 创建。
        conn.execute("DELETE FROM hier_composition_revisions WHERE name=?",
                     (name,))
    return cur.rowcount > 0


# ---------------- 组合修订快照（行为审计使用） ----------------

def list_composition_revisions(name: str) -> list[dict]:
    init_db()
    with _conn() as conn:
        rows = conn.execute(
            """SELECT revision, doc_hash, flat_hash, created_at
               FROM hier_composition_revisions WHERE name=?
               ORDER BY revision DESC""",
            (name,)).fetchall()
    return [dict(r) for r in rows]


def get_composition_revision(name: str, revision: int) -> dict | None:
    init_db()
    with _conn() as conn:
        row = conn.execute(
            """SELECT * FROM hier_composition_revisions
               WHERE name=? AND revision=?""",
            (name, revision)).fetchone()
    if row is None:
        return None
    d = dict(row)
    for key in ("doc_json", "flat_json", "versions_json"):
        d[key] = json.loads(d[key]) if d[key] is not None else None
    return d


def update_revision_snapshot(name: str, revision: int, flat: dict,
                             versions: dict) -> bool:
    init_db()
    with _conn() as conn:
        cur = conn.execute(
            """UPDATE hier_composition_revisions
               SET flat_json=?, flat_hash=?, versions_json=?
               WHERE name=? AND revision=? AND flat_json IS NULL""",
            (json.dumps(flat, ensure_ascii=False), doc_hash(flat),
             json.dumps(versions, ensure_ascii=False), name, revision))
    return cur.rowcount > 0


def save_behavior_audit(record: dict) -> dict:
    """按 (组合, 有序修订对, 参数哈希) upsert 审计记录。"""
    init_db()
    left_rev, right_rev = sorted(
        (int(record["left_revision"]), int(record["right_revision"])))
    now = time.time()
    ph = hashes({}, record["params"])[1]
    values = (
        record["comp_name"], left_rev, right_rev, ph,
        record["left"]["flat_hash"], record["right"]["flat_hash"],
        json.dumps(record["params"], ensure_ascii=False),
        json.dumps(record["left"]["doc"], ensure_ascii=False),
        json.dumps(record["right"]["doc"], ensure_ascii=False),
        json.dumps(record["left"]["flat"], ensure_ascii=False),
        json.dumps(record["right"]["flat"], ensure_ascii=False),
        json.dumps(record["left"].get("versions") or {}, ensure_ascii=False),
        json.dumps(record["right"].get("versions") or {}, ensure_ascii=False),
        record["result"].get("status"),
        json.dumps(record["result"], ensure_ascii=False),
    )
    with _conn() as conn:
        row = conn.execute(
            """SELECT id FROM hier_behavior_audits
               WHERE comp_name=? AND left_revision=? AND right_revision=?
                 AND params_hash=?""",
            (record["comp_name"], left_rev, right_rev, ph)).fetchone()
        if row:
            conn.execute(
                """UPDATE hier_behavior_audits SET
                     left_flat_hash=?, right_flat_hash=?, params_json=?,
                     left_doc_json=?, right_doc_json=?, left_flat_json=?,
                     right_flat_json=?, left_versions=?, right_versions=?,
                     status=?, result_json=?, created_at=?
                   WHERE id=?""",
                values[4:] + (now, row["id"]))
            rid = row["id"]
            reused = True
        else:
            cur = conn.execute(
                """INSERT INTO hier_behavior_audits
                   (comp_name,left_revision,right_revision,params_hash,
                    left_flat_hash,right_flat_hash,params_json,left_doc_json,
                    right_doc_json,left_flat_json,right_flat_json,
                    left_versions,right_versions,status,result_json,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                values + (now,))
            rid = cur.lastrowid
            reused = False
    return {"id": rid, "left_revision": left_rev,
            "right_revision": right_rev, "params_hash": ph, "reused": reused}


def list_behavior_audits(limit: int = 100) -> list[dict]:
    init_db()
    with _conn() as conn:
        rows = conn.execute(
            """SELECT id, comp_name, left_revision, right_revision,
                      left_flat_hash, right_flat_hash, params_hash, status,
                      result_json, created_at FROM hier_behavior_audits
               ORDER BY created_at DESC, id DESC LIMIT ?""",
            (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        result = json.loads(d.pop("result_json"))
        dv = result.get("earliest_divergence")
        d["earliest_divergence"] = dv["time"] if dv else None
        d["reason"] = result.get("reason")
        out.append(d)
    return out


def get_behavior_audit(rid: int) -> dict | None:
    init_db()
    with _conn() as conn:
        row = conn.execute("SELECT * FROM hier_behavior_audits WHERE id=?",
                           (rid,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    for key in ("params_json", "left_doc_json", "right_doc_json",
                "left_flat_json", "right_flat_json", "left_versions",
                "right_versions", "result_json"):
        d[key] = json.loads(d[key])
    return d


# ---------------- 层次分析记录（可重放） ----------------

def save_hier_analysis(comp_name: str, comp_version: int,
                       flat_netlist: dict, params_obj: dict,
                       versions: dict, result: dict) -> dict:
    init_db()
    fh, ph = hashes(flat_netlist, params_obj)
    now = time.time()
    with _conn() as conn:
        row = conn.execute(
            """SELECT id FROM hier_analyses
               WHERE comp_name=? AND flat_hash=? AND params_hash=?""",
            (comp_name, fh, ph)).fetchone()
        if row:
            conn.execute(
                """UPDATE hier_analyses SET status=?, result_json=?,
                       comp_version=?, created_at=? WHERE id=?""",
                (result.get("status"),
                 json.dumps(result, ensure_ascii=False), comp_version,
                 now, row["id"]))
            return {"id": row["id"], "flat_hash": fh, "params_hash": ph,
                    "reused": True}
        cur = conn.execute(
            """INSERT INTO hier_analyses
               (comp_name, comp_version, flat_hash, params_hash, params_json,
                flat_json, versions_json, status, result_json, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (comp_name, comp_version, fh, ph,
             json.dumps(params_obj, ensure_ascii=False),
             json.dumps(flat_netlist, ensure_ascii=False),
             json.dumps(versions, ensure_ascii=False),
             result.get("status"),
             json.dumps(result, ensure_ascii=False), now))
        return {"id": cur.lastrowid, "flat_hash": fh, "params_hash": ph,
                "reused": False}


def list_hier_analyses(limit: int = 100) -> list[dict]:
    init_db()
    with _conn() as conn:
        rows = conn.execute(
            """SELECT id, comp_name, comp_version, flat_hash, params_hash,
                      status, created_at FROM hier_analyses
               ORDER BY created_at DESC, id DESC LIMIT ?""",
            (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_hier_analysis(rid: int) -> dict | None:
    init_db()
    with _conn() as conn:
        row = conn.execute("SELECT * FROM hier_analyses WHERE id=?",
                           (rid,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    for k in ("params_json", "flat_json", "versions_json", "result_json"):
        d[k] = json.loads(d[k])
    return d

"""SQLite 持久层：不可变规则版本与判定包，样品判定按版本递增。

* 规则以 content_hash 为唯一身份：同一哈希永远不会被覆盖，规则更新只产生新行。
* 判定包（package）整体快照存储；正式接收/补录不可修改，只追加新版本。
* 同一 idempotency_key 的重复正式接收返回原判定（幂等）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime
from typing import Optional

from fastapi import HTTPException

from .models import JudgmentResult, RuleSet, utc_now
from .versioning import canonical_json, hash_rules

DEFAULT_DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "app.db"
)
DB_PATH = os.environ.get("DEADLINE_DB", DEFAULT_DB)

_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


_conn: Optional[sqlite3.Connection] = None


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = _connect()
        init_db(_conn)
    return _conn


def init_db(conn: Optional[sqlite3.Connection] = None) -> None:
    conn = conn or get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS rule_sets (
            content_hash TEXT PRIMARY KEY,
            version      TEXT NOT NULL,
            name         TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at   TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS judgments (
            package_id      TEXT PRIMARY KEY,
            sample_id       TEXT,
            scope           TEXT NOT NULL,            -- batch | sample
            version_no      INTEGER NOT NULL,
            trial           INTEGER NOT NULL,
            request_id      TEXT,
            idempotency_key TEXT,
            rule_hash       TEXT NOT NULL REFERENCES rule_sets(content_hash),
            eval_time       TEXT NOT NULL,
            package_json    TEXT NOT NULL,
            request_json    TEXT,
            created_at      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_judgments_sample ON judgments(sample_id, version_no);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_judgments_idem
            ON judgments(idempotency_key) WHERE idempotency_key IS NOT NULL AND trial = 0;
        CREATE TABLE IF NOT EXISTS sample_versions (
            sample_id  TEXT NOT NULL,
            scope_key  TEXT NOT NULL,   -- batch 接收时为包内每个样品登记的 sample_id
            version_no INTEGER NOT NULL,
            package_id TEXT NOT NULL REFERENCES judgments(package_id),
            PRIMARY KEY (sample_id, version_no)
        );
        """
    )
    # 旧库迁移：包内可能冻结多个规则集版本（各时钟唯一匹配），rule_hash 退化为代表
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(judgments)").fetchall()}
    if "rule_hashes_json" not in cols:
        conn.execute("ALTER TABLE judgments ADD COLUMN rule_hashes_json TEXT")
    conn.commit()


# ---------------------------------------------------------------- 规则 ----

def register_rule_set(rule_set: RuleSet) -> tuple[str, bool]:
    """登记规则集。返回 (content_hash, created)。

    冲突策略：
    * 同一版本号绑定不同内容哈希 -> 409（版本标签不可被悄悄改写）；
    * 同一内容哈希使用不同版本号 -> 409（内容身份与其版本标签保持一致）；
    * 完全一致（哈希+版本号）-> 幂等返回 created=False。
    """
    h = hash_rules(rule_set)
    conn = get_conn()
    with _lock:
        same_hash = conn.execute(
            "SELECT version FROM rule_sets WHERE content_hash = ?", (h,)
        ).fetchone()
        same_label = conn.execute(
            "SELECT content_hash FROM rule_sets WHERE version = ?"
            " ORDER BY created_at DESC LIMIT 1",
            (rule_set.version,),
        ).fetchone()
        if same_label is not None and same_label["content_hash"] != h:
            raise HTTPException(
                409,
                f"规则版本标签 {rule_set.version} 已绑定内容 "
                f"{same_label['content_hash'][:12]}，不能以不同内容复用；"
                "规则更新请使用新版本号，旧判定不可改写",
            )
        if same_hash is not None and same_hash["version"] != rule_set.version:
            raise HTTPException(
                409,
                f"相同规则内容已以版本 {same_hash['version']} 登记，"
                "不能再绑定不同版本标签",
            )
        if same_hash is not None:
            return h, False
        conn.execute(
            "INSERT INTO rule_sets(content_hash, version, name, payload_json, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (h, rule_set.version, rule_set.name,
             canonical_json(rule_set.model_dump(mode="json")),
             utc_now().isoformat()),
        )
        conn.commit()
    return h, True


def get_rule_set_by_hash(content_hash: str) -> Optional[dict]:
    row = get_conn().execute(
        "SELECT * FROM rule_sets WHERE content_hash = ?", (content_hash,)
    ).fetchone()
    return dict(row) if row else None


def get_rule_payload(content_hash: str) -> RuleSet:
    row = get_rule_set_by_hash(content_hash)
    if not row:
        raise HTTPException(404, f"规则哈希未登记: {content_hash}")
    return RuleSet.model_validate(json.loads(row["payload_json"]))


def find_rule_by_version(version: str) -> Optional[tuple[str, RuleSet]]:
    row = get_conn().execute(
        "SELECT content_hash, payload_json FROM rule_sets WHERE version = ?"
        " ORDER BY created_at DESC", (version,)
    ).fetchone()
    if not row:
        return None
    return row["content_hash"], RuleSet.model_validate(json.loads(row["payload_json"]))


def list_rule_sets() -> list[dict]:
    rows = get_conn().execute(
        "SELECT content_hash, version, name, payload_json, created_at"
        " FROM rule_sets ORDER BY created_at"
    ).fetchall()
    out = []
    for r in rows:
        payload = json.loads(r["payload_json"])
        out.append({
            "content_hash": r["content_hash"], "version": r["version"],
            "name": r["name"], "item_count": len(payload.get("items", [])),
            "created_at": r["created_at"],
        })
    return out


def all_rule_sets_payload() -> list[tuple[str, RuleSet]]:
    """全部已登记规则集（含内容），用于构建适用性候选池。"""
    rows = get_conn().execute(
        "SELECT content_hash, payload_json FROM rule_sets ORDER BY created_at"
    ).fetchall()
    return [
        (r["content_hash"], RuleSet.model_validate(json.loads(r["payload_json"])))
        for r in rows
    ]


def all_formal_requests() -> list[dict]:
    """全部正式接收（非试算）且带请求快照的判定记录，供影响预览重放。"""
    rows = get_conn().execute(
        "SELECT package_id, sample_id, scope, request_json, package_json"
        " FROM judgments WHERE trial = 0 AND request_json IS NOT NULL"
        " ORDER BY created_at"
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 判定 ----

def _register_sample_versions(conn, package: JudgmentResult) -> None:
    if package.sample_id:
        # 样品级补录：只登记目标样品，版本视图不波及同批其它样品
        sample_ids = {package.sample_id}
    else:
        # 批次接收：包内每个样品共享同一批次版本
        sample_ids = {c.sample_id for c in package.clocks}
    for sid in sample_ids:
        conn.execute(
            "INSERT OR IGNORE INTO sample_versions(sample_id, scope_key, version_no,"
            " package_id) VALUES (?, ?, ?, ?)",
            (sid, sid, package.version_no, package.package_id),
        )


def save_judgment(
    package: JudgmentResult, *, trial: bool, idempotency_key: Optional[str],
    request_json: Optional[str] = None,
) -> JudgmentResult:
    if trial:
        return package
    conn = get_conn()
    payload = canonical_json(package.model_dump(mode="json"))
    with _lock:
        if idempotency_key:
            row = conn.execute(
                "SELECT package_json FROM judgments WHERE idempotency_key = ? AND trial = 0",
                (idempotency_key,),
            ).fetchone()
            if row:
                return JudgmentResult.model_validate(json.loads(row["package_json"]))
        try:
            hashes = sorted({c.matched_rule.content_hash
                             for c in package.clocks if c.matched_rule})
            conn.execute(
                "INSERT INTO judgments(package_id, sample_id, scope, version_no, trial,"
                " request_id, idempotency_key, rule_hash, rule_hashes_json, eval_time,"
                " package_json, request_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    package.package_id, package.sample_id,
                    "sample" if package.sample_id else "batch",
                    package.version_no, int(trial), package.request_id,
                    idempotency_key, package.rule.content_hash,
                    canonical_json(hashes),
                    package.eval_time.isoformat(), payload, request_json,
                    package.created_at.isoformat(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, f"判定持久化冲突: {exc}") from exc
        _register_sample_versions(conn, package)
        conn.commit()
    return package


def get_package(package_id: str) -> Optional[dict]:
    row = get_conn().execute(
        "SELECT package_json FROM judgments WHERE package_id = ?", (package_id,)
    ).fetchone()
    return json.loads(row["package_json"]) if row else None


def get_stored_request(package_id: str) -> Optional[dict]:
    row = get_conn().execute(
        "SELECT request_json FROM judgments WHERE package_id = ?", (package_id,)
    ).fetchone()
    return json.loads(row["request_json"]) if row and row["request_json"] else None


def get_package_model(package_id: str) -> Optional[JudgmentResult]:
    data = get_package(package_id)
    return JudgmentResult.model_validate(data) if data else None


def list_sample_versions(sample_id: str) -> list[dict]:
    rows = get_conn().execute(
        "SELECT j.package_id, j.version_no, j.scope, j.sample_id AS pkg_sample_id,"
        " j.eval_time, j.created_at, j.rule_hash FROM sample_versions sv"
        " JOIN judgments j ON j.package_id = sv.package_id"
        " WHERE sv.sample_id = ? AND (j.sample_id IS NULL OR j.sample_id = ?)"
        " ORDER BY j.version_no DESC, j.created_at DESC",
        (sample_id, sample_id),
    ).fetchall()
    return [dict(r) for r in rows]


def latest_sample_package(sample_id: str) -> Optional[dict]:
    # 优先返回样品级补录版本；无样品级版本时回退到批次版本。
    # 样品级包（sample_id=其它样品）对本样品不可见，避免补录串样品。
    row = get_conn().execute(
        "SELECT j.package_json FROM sample_versions sv JOIN judgments j"
        " ON j.package_id = sv.package_id WHERE sv.sample_id = ?"
        " AND (j.sample_id IS NULL OR j.sample_id = ?)"
        " ORDER BY (j.scope = 'sample') DESC, j.version_no DESC,"
        " j.created_at DESC LIMIT 1",
        (sample_id, sample_id),
    ).fetchone()
    return json.loads(row["package_json"]) if row else None


def next_sample_version(sample_id: str) -> int:
    row = get_conn().execute(
        "SELECT COALESCE(MAX(version_no), 0) AS m FROM sample_versions WHERE sample_id = ?",
        (sample_id,),
    ).fetchone()
    return int(row["m"]) + 1

"""store_sqlite.py —— 本地 SQLite 回退后端(从 main.py 原样搬出,P5:order)。

双轨回退里的 SQLite 这一轨。main 的数据分诊器在 `models.is_enabled()` 为假时落到这里。

铁律(e):DB 文件路径(ORDER_DB_FILE)与并发锁(ORDER_DB_LOCK)留在 main,
本模块**只通过参数接收** db_file / lock,自身不持有、不读 env、不 import main、不 import models。
锁对象由 main 创建并按引用传入,这里只 `with lock:`,不创建锁。

实现体与 SQL 与回复模板均与原 main 逐字一致,仅把 ORDER_DB_FILE→db_file、ORDER_DB_LOCK→lock、
order_db_connection()→order_db_connection(db_file)。receipt 的 SQLite 回退依赖 receipt 领域归一化,
随 P6 receipt_logic 一并搬入本模块。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from order_normalize import (
    ORDER_STATUS_ALL,
    ORDER_STATUS_CANCELLED,
    ORDER_STATUS_FETCHED,
    ORDER_STATUS_NEW,
    normalize_order_date_text,
    normalize_order_payload,
    now_iso,
)


def init_order_db(db_file: Path) -> None:
    db_file.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_file) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS order_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                source TEXT NOT NULL,
                store TEXT NOT NULL DEFAULT '',
                order_date TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'new',
                confirmed INTEGER NOT NULL DEFAULT 0,
                raw_ref TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(order_entries)").fetchall()
        }
        if "order_date" not in columns:
            conn.execute("ALTER TABLE order_entries ADD COLUMN order_date TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_order_entries_status ON order_entries(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_order_entries_confirmed ON order_entries(confirmed)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_order_entries_kind ON order_entries(kind)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_order_entries_order_date ON order_entries(order_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_order_entries_status_order_date ON order_entries(status, order_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_order_entries_raw_ref ON order_entries(raw_ref)")
        rows = conn.execute(
            "SELECT id, payload_json, created_at FROM order_entries WHERE order_date = ''"
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(str(row[1]))
            except json.JSONDecodeError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            order_date = normalize_order_date_text(payload.get("order_date")) if isinstance(payload, dict) else ""
            if order_date:
                conn.execute(
                    "UPDATE order_entries SET order_date = ? WHERE id = ?",
                    (str(order_date), int(row[0])),
                )
        conn.commit()


def order_db_connection(db_file: Path) -> sqlite3.Connection:
    init_order_db(db_file)
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    return conn


def row_to_order_payload(row: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(str(row["payload_json"]))
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    payload["id"] = int(row["id"])
    payload["kind"] = str(row["kind"])
    payload["source"] = str(row["source"])
    payload["store"] = str(row["store"] or payload.get("store") or "")
    payload["confirmed"] = bool(row["confirmed"])
    payload["status"] = str(row["status"])
    payload["raw_ref"] = str(row["raw_ref"] or payload.get("raw_ref") or "")
    payload["created_at"] = str(row["created_at"] or payload.get("created_at") or "")
    payload["order_date"] = str(row["order_date"] or payload.get("order_date") or "")
    normalized = normalize_order_payload(payload)
    normalized["id"] = int(row["id"])
    normalized["kind"] = str(row["kind"])
    normalized["source"] = str(row["source"])
    normalized["store"] = str(row["store"] or normalized.get("store") or "")
    normalized["confirmed"] = bool(row["confirmed"])
    normalized["status"] = str(row["status"])
    normalized["raw_ref"] = str(row["raw_ref"] or normalized.get("raw_ref") or "")
    normalized["created_at"] = str(row["created_at"] or normalized.get("created_at") or "")
    normalized["order_date"] = str(row["order_date"] or normalized.get("order_date") or "")
    return normalized


def summarize_order_for_reply(payload: dict[str, Any]) -> str:
    store = str(payload.get("store") or "未填门店")
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    if not items:
        return store
    first = items[0] if isinstance(items[0], dict) else {}
    name = str(first.get("name") or "未填商品")
    qty = first.get("qty")
    unit = str(first.get("unit") or "")
    qty_text = "" if qty is None else f"{qty}{unit}"
    more = "" if len(items) == 1 else f"等{len(items)}项"
    return f"{store} {name}{qty_text}{more}".strip()


def insert_order_payload(normalized: dict[str, Any], *, db_file: Path, lock: Any) -> dict[str, Any]:
    created_at = normalized.get("created_at") or now_iso()
    normalized["created_at"] = created_at
    normalized["order_date"] = str(normalized.get("order_date") or "")
    normalized["status"] = normalized.get("status") or "new"
    normalized["confirmed"] = bool(normalized.get("confirmed"))

    with lock:
        with order_db_connection(db_file) as conn:
            cursor = conn.execute(
                """
                INSERT INTO order_entries (
                    kind, source, store, order_date, status, confirmed, raw_ref,
                    created_at, updated_at, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(normalized.get("kind") or ""),
                    str(normalized.get("source") or ""),
                    str(normalized.get("store") or ""),
                    str(normalized.get("order_date") or ""),
                    str(normalized.get("status") or "new"),
                    1 if normalized.get("confirmed") else 0,
                    str(normalized.get("raw_ref") or ""),
                    created_at,
                    now_iso(),
                    json.dumps(normalized, ensure_ascii=False),
                ),
            )
            order_id = int(cursor.lastrowid)
            normalized["id"] = order_id
            conn.execute(
                "UPDATE order_entries SET payload_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(normalized, ensure_ascii=False), now_iso(), order_id),
            )
            conn.commit()
    return normalized


def query_order_payloads(
    *,
    db_file: Path,
    status: str | None = None,
    ids: list[int] | None = None,
    order_date: str | None = None,
) -> list[dict[str, Any]]:
    clauses = ["confirmed = 1", "status != ?"]
    params: list[Any] = [ORDER_STATUS_CANCELLED]
    if ids:
        placeholders = ",".join("?" for _ in ids)
        clauses.append(f"id IN ({placeholders})")
        params.extend(ids)
    else:
        if order_date is not None:
            clauses.append("order_date = ?")
            params.append(order_date)
    if status and status != ORDER_STATUS_ALL and not ids:
        clauses.append("status = ?")
        params.append(status)

    where_sql = " AND ".join(clauses)
    with order_db_connection(db_file) as conn:
        rows = conn.execute(
            f"SELECT * FROM order_entries WHERE {where_sql} ORDER BY id ASC",
            params,
        ).fetchall()
    return [row_to_order_payload(row) for row in rows]


def mark_order_payloads_fetched(ids: list[int], *, db_file: Path, lock: Any) -> dict[str, list[int]]:
    clean_ids = sorted({int(order_id) for order_id in ids if int(order_id) > 0})
    if not clean_ids:
        return {"succeeded": [], "failed": []}

    placeholders = ",".join("?" for _ in clean_ids)
    with lock:
        with order_db_connection(db_file) as conn:
            existing_rows = conn.execute(
                f"SELECT id FROM order_entries WHERE id IN ({placeholders}) AND status != ?",
                [*clean_ids, ORDER_STATUS_CANCELLED],
            ).fetchall()
            existing_ids = {int(row["id"]) for row in existing_rows}
            succeeded = [order_id for order_id in clean_ids if order_id in existing_ids]
            failed = [order_id for order_id in clean_ids if order_id not in existing_ids]

            if not succeeded:
                return {"succeeded": [], "failed": failed}

            update_placeholders = ",".join("?" for _ in succeeded)
            conn.execute(
                f"""
                UPDATE order_entries
                SET status = 'fetched',
                    updated_at = ?
                WHERE id IN ({update_placeholders})
                """,
                [now_iso(), *succeeded],
            )
            rows = conn.execute(
                f"SELECT * FROM order_entries WHERE id IN ({update_placeholders})",
                succeeded,
            ).fetchall()
            for row in rows:
                payload = row_to_order_payload(row)
                payload["status"] = "fetched"
                conn.execute(
                    "UPDATE order_entries SET payload_json = ? WHERE id = ?",
                    (json.dumps(payload, ensure_ascii=False), int(row["id"])),
                )
            conn.commit()
            return {"succeeded": succeeded, "failed": failed}


def unmark_order_payloads(ids: list[int], *, db_file: Path, lock: Any) -> dict[str, list[int]]:
    clean_ids = sorted({int(order_id) for order_id in ids if int(order_id) > 0})
    if not clean_ids:
        return {"succeeded": [], "failed": []}

    placeholders = ",".join("?" for _ in clean_ids)
    with lock:
        with order_db_connection(db_file) as conn:
            existing_rows = conn.execute(
                f"SELECT id FROM order_entries WHERE id IN ({placeholders}) AND status != ?",
                [*clean_ids, ORDER_STATUS_CANCELLED],
            ).fetchall()
            existing_ids = {int(row["id"]) for row in existing_rows}
            succeeded = [order_id for order_id in clean_ids if order_id in existing_ids]
            failed = [order_id for order_id in clean_ids if order_id not in existing_ids]

            if not succeeded:
                return {"succeeded": [], "failed": failed}

            update_placeholders = ",".join("?" for _ in succeeded)
            conn.execute(
                f"""
                UPDATE order_entries
                SET status = 'new',
                    updated_at = ?
                WHERE id IN ({update_placeholders})
                """,
                [now_iso(), *succeeded],
            )
            rows = conn.execute(
                f"SELECT * FROM order_entries WHERE id IN ({update_placeholders})",
                succeeded,
            ).fetchall()
            for row in rows:
                payload = row_to_order_payload(row)
                payload["status"] = ORDER_STATUS_NEW
                conn.execute(
                    "UPDATE order_entries SET payload_json = ? WHERE id = ?",
                    (json.dumps(payload, ensure_ascii=False), int(row["id"])),
                )
            conn.commit()
            return {"succeeded": succeeded, "failed": failed}


def cancel_latest_order_for_user(user_id: str, *, db_file: Path, lock: Any) -> str:
    with lock:
        with order_db_connection(db_file) as conn:
            rows = conn.execute(
                """
                SELECT * FROM order_entries
                WHERE confirmed = 1
                  AND status != ?
                  AND (raw_ref = ? OR raw_ref LIKE ?)
                ORDER BY id DESC
                LIMIT 1
                """,
                (ORDER_STATUS_CANCELLED, user_id, f"{user_id}:%"),
            ).fetchall()
            if not rows:
                return "没找到你最近确认的订单，暂时没有可撤回的。"

            row = rows[0]
            payload = row_to_order_payload(row)
            status = str(row["status"] or payload.get("status") or "")
            if status == ORDER_STATUS_FETCHED:
                return "这单已被排产/发货使用，不能直接撤回，需要联系数据部处理。"

            payload["status"] = ORDER_STATUS_CANCELLED
            payload["cancelled_at"] = now_iso()
            conn.execute(
                """
                UPDATE order_entries
                SET status = ?, payload_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    ORDER_STATUS_CANCELLED,
                    json.dumps(payload, ensure_ascii=False),
                    now_iso(),
                    int(row["id"]),
                ),
            )
            conn.commit()
            return f"好，刚那单（{summarize_order_for_reply(payload)}）撤回了，重新发我吧。"


__all__ = [
    "init_order_db",
    "order_db_connection",
    "row_to_order_payload",
    "summarize_order_for_reply",
    "insert_order_payload",
    "query_order_payloads",
    "mark_order_payloads_fetched",
    "unmark_order_payloads",
    "cancel_latest_order_for_user",
]

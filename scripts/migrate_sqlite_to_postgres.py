#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import models  # noqa: E402


def read_json_object(raw: Any) -> dict[str, Any]:
    try:
        data = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def read_json_file(path: Path) -> Any:
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"skip invalid JSON {path}: {exc}")
        return None


def migrate_orders(path: Path, dry_run: bool) -> int:
    if not path.exists():
        print(f"orders: skip missing {path}")
        return 0

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("select * from order_entries order by id asc").fetchall()

    migrated = 0
    for row in rows:
        payload = read_json_object(row["payload_json"])
        payload.update(
            {
                "kind": str(row["kind"] or payload.get("kind") or ""),
                "source": str(row["source"] or payload.get("source") or ""),
                "store": str(row["store"] or payload.get("store") or ""),
                "order_date": str(row["order_date"] or payload.get("order_date") or ""),
                "status": str(row["status"] or payload.get("status") or "new"),
                "confirmed": bool(row["confirmed"]),
                "raw_ref": str(row["raw_ref"] or payload.get("raw_ref") or ""),
                "created_at": str(row["created_at"] or payload.get("created_at") or ""),
                "legacy_source": "sqlite_order_entries",
                "legacy_id": int(row["id"]),
            }
        )
        if dry_run:
            migrated += 1
            continue
        models.insert_order_payload(payload)
        migrated += 1
    print(f"orders: migrated {migrated} rows from {path}")
    return migrated


def migrate_receipts(path: Path, dry_run: bool) -> int:
    if not path.exists():
        print(f"receipts: skip missing {path}")
        return 0

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("select * from receipt_entries order by id asc").fetchall()

    migrated = 0
    for row in rows:
        payload = read_json_object(row["payload_json"])
        payload.update(
            {
                "date": str(row["date"] or payload.get("date") or ""),
                "status": str(row["status"] or payload.get("status") or "confirmed"),
                "created_at": str(row["created_at"] or payload.get("created_at") or ""),
                "legacy_source": "sqlite_receipt_entries",
                "legacy_id": int(row["id"]),
            }
        )
        if dry_run:
            migrated += 1
            continue
        models.insert_receipt_payload(payload)
        migrated += 1
    print(f"receipts: migrated {migrated} rows from {path}")
    return migrated


def migrate_memory(path: Path, dry_run: bool) -> int:
    data = read_json_file(path)
    if not isinstance(data, dict):
        print(f"memory: skip missing or invalid {path}")
        return 0
    if not dry_run:
        models.save_memory(data)
    print(f"memory: migrated {len(data)} keys from {path}")
    return len(data)


def migrate_session_state(path: Path, dry_run: bool) -> int:
    data = read_json_file(path)
    if not isinstance(data, dict):
        print(f"session_state: skip missing or invalid {path}")
        return 0
    state = {str(key): value for key, value in data.items() if isinstance(value, dict)}
    if not dry_run:
        models.save_session_state(state)
    print(f"session_state: migrated {len(state)} keys from {path}")
    return len(state)


def migrate_interviews(path: Path, dry_run: bool) -> int:
    data = read_json_file(path)
    if isinstance(data, list):
        archive = {
            str(record.get("session_id")): record
            for record in data
            if isinstance(record, dict) and record.get("session_id")
        }
    elif isinstance(data, dict):
        archive = {
            str(key): value
            for key, value in data.items()
            if isinstance(value, dict)
        }
    else:
        print(f"interviews: skip missing or invalid {path}")
        return 0
    if not dry_run:
        models.save_interview_archive(archive)
    print(f"interviews: migrated {len(archive)} records from {path}")
    return len(archive)


def migrate_kf_cursors(path: Path, dry_run: bool) -> int:
    data = read_json_file(path)
    if not isinstance(data, dict):
        print(f"kf_cursors: skip missing or invalid {path}")
        return 0
    cursors = {str(key): str(value) for key, value in data.items() if value is not None}
    if not dry_run:
        models.save_kf_cursors(cursors)
    print(f"kf_cursors: migrated {len(cursors)} cursors from {path}")
    return len(cursors)


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate FoodWechatBotNew SQLite data to PostgreSQL.")
    parser.add_argument("--orders-db", default=os.getenv("ORDER_DB_FILE", "orders.db"))
    parser.add_argument("--receipts-db", default=os.getenv("RECEIPT_DB_FILE", "receipts.db"))
    parser.add_argument("--memory-file", default=os.getenv("MEMORY_FILE", "memory.json"))
    parser.add_argument("--session-state-file", default=os.getenv("SESSION_STATE_FILE", "session_state.json"))
    parser.add_argument("--interview-archive-file", default=os.getenv("INTERVIEW_ARCHIVE_FILE", "interviews.json"))
    parser.add_argument("--kf-cursor-file", default=os.getenv("WECOM_KF_CURSOR_FILE", "kf_cursors.json"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.dry_run and not models.is_enabled():
        raise SystemExit("Set DATABASE_BACKEND=postgres before running the real migration.")
    if not args.dry_run and not models.is_redis_cache_enabled():
        raise SystemExit("Set REDIS_URL before running the PostgreSQL migration.")

    migrate_orders(Path(args.orders_db), args.dry_run)
    migrate_receipts(Path(args.receipts_db), args.dry_run)
    migrate_memory(Path(args.memory_file), args.dry_run)
    migrate_session_state(Path(args.session_state_file), args.dry_run)
    migrate_interviews(Path(args.interview_archive_file), args.dry_run)
    migrate_kf_cursors(Path(args.kf_cursor_file), args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

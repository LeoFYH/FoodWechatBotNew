from __future__ import annotations

from typing import Any

from .constants import (
    RECEIPT_STATUS_ALL,
    RECEIPT_STATUS_CANCELLED,
    RECEIPT_STATUS_CONFIRMED,
    RECEIPT_STATUS_FETCHED,
    RECEIPT_STATUS_NEW,
)
from .db import connection, jsonb
from .production_receipt_items import insert_receipt_item, rows_to_payload_items
from .tenants import ensure_tenant
from .utils import as_datetime, legacy_fields, raw_ref_belongs_to_user, required_date


TABLE = "production_receipts"


def row_to_receipt_payload(conn: Any, row: dict[str, Any]) -> dict[str, Any]:
    items = conn.execute(
        """
        select *
        from production_receipt_items
        where receipt_id = %s
        order by line_no asc, id asc
        """,
        (row["id"],),
    ).fetchall()
    return {
        "id": f"r{int(row['id']):03d}",
        "date": row["receipt_date"].isoformat() if row.get("receipt_date") else "",
        "items": rows_to_payload_items(items),
    }


def _select_receipt_by_id(conn: Any, receipt_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        select *
        from production_receipts
        where id = %s
        """,
        (receipt_id,),
    ).fetchone()
    if not row:
        return None
    return row_to_receipt_payload(conn, row)


def receipt_id_label(receipt_id: int) -> str:
    return f"r{int(receipt_id):03d}"


def parse_receipt_id_values(ids: list[Any]) -> tuple[list[int], list[str]]:
    clean_ids: list[int] = []
    failed: list[str] = []
    seen: set[int] = set()
    for raw_id in ids:
        text = str(raw_id or "").strip()
        if text.lower().startswith("r"):
            text = text[1:]
        try:
            receipt_id = int(text)
        except ValueError:
            failed.append(str(raw_id))
            continue
        if receipt_id <= 0:
            failed.append(str(raw_id))
            continue
        if receipt_id in seen:
            continue
        seen.add(receipt_id)
        clean_ids.append(receipt_id)
    return sorted(clean_ids), failed


def receipt_status_to_storage_filter(status: str | None) -> str | None:
    if not status or status == RECEIPT_STATUS_NEW:
        return RECEIPT_STATUS_CONFIRMED
    if status == RECEIPT_STATUS_ALL:
        return None
    return status


def insert_receipt_payload(payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(payload)
    legacy_source, legacy_id = legacy_fields(payload)

    with connection() as conn:
        tenant_id = ensure_tenant(conn)
        if legacy_source and legacy_id is not None:
            existing = conn.execute(
                """
                select id
                from production_receipts
                where tenant_id = %s and legacy_source = %s and legacy_id = %s
                """,
                (tenant_id, legacy_source, legacy_id),
            ).fetchone()
            if existing:
                found = _select_receipt_by_id(conn, int(existing["id"]))
                if found:
                    return found

        receipt_date = required_date(payload.get("date"))
        created_at = as_datetime(payload.get("created_at"))
        row = conn.execute(
            """
            insert into production_receipts (
                tenant_id, receipt_date, status, source, raw_ref, raw_payload,
                legacy_source, legacy_id, created_at, updated_at
            )
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            returning id
            """,
            (
                tenant_id,
                receipt_date,
                payload.get("status") or "confirmed",
                payload.get("source") or "photo",
                payload.get("raw_ref") or "",
                jsonb(payload),
                legacy_source,
                legacy_id,
                created_at,
            ),
        ).fetchone()
        receipt_id = int(row["id"])

        for line_no, item in enumerate(payload.get("items") or [], start=1):
            if isinstance(item, dict):
                insert_receipt_item(conn, tenant_id, receipt_id, line_no, item)

        return _select_receipt_by_id(conn, receipt_id) or {
            "id": f"r{receipt_id:03d}",
            "date": str(receipt_date),
            "items": [],
        }


def query_receipt_payloads(receipt_date: str, status: str | None = None) -> list[dict[str, Any]]:
    with connection() as conn:
        tenant_id = ensure_tenant(conn)
        clauses = ["tenant_id = %s", "receipt_date = %s", "status <> %s"]
        params: list[Any] = [tenant_id, required_date(receipt_date), RECEIPT_STATUS_CANCELLED]
        storage_status = receipt_status_to_storage_filter(status)
        if storage_status:
            clauses.append("status = %s")
            params.append(storage_status)
        rows = conn.execute(
            f"""
            select *
            from production_receipts
            where {' and '.join(clauses)}
            order by id asc
            """,
            params,
        ).fetchall()
        return [row_to_receipt_payload(conn, row) for row in rows]


def update_receipt_status(ids: list[Any], target_status: str) -> dict[str, list[str]]:
    clean_ids, failed = parse_receipt_id_values(ids)
    if not clean_ids:
        return {"succeeded": [], "failed": failed}

    with connection() as conn:
        tenant_id = ensure_tenant(conn)
        rows = conn.execute(
            """
            select id
            from production_receipts
            where tenant_id = %s and id = any(%s) and status <> %s
            """,
            (tenant_id, clean_ids, RECEIPT_STATUS_CANCELLED),
        ).fetchall()
        existing_ids = {int(row["id"]) for row in rows}
        succeeded_ints = [receipt_id for receipt_id in clean_ids if receipt_id in existing_ids]
        failed.extend(receipt_id_label(receipt_id) for receipt_id in clean_ids if receipt_id not in existing_ids)
        if not succeeded_ints:
            return {"succeeded": [], "failed": failed}

        conn.execute(
            """
            update production_receipts
            set status = %s, updated_at = now()
            where tenant_id = %s and id = any(%s)
            """,
            (target_status, tenant_id, succeeded_ints),
        )
        return {
            "succeeded": [receipt_id_label(receipt_id) for receipt_id in succeeded_ints],
            "failed": failed,
        }


def mark_receipt_payloads_fetched(ids: list[Any]) -> dict[str, list[str]]:
    return update_receipt_status(ids, RECEIPT_STATUS_FETCHED)


def unmark_receipt_payloads(ids: list[Any]) -> dict[str, list[str]]:
    return update_receipt_status(ids, RECEIPT_STATUS_CONFIRMED)


def clear_receipts_by_date(receipt_date: str) -> dict[str, Any]:
    """强删某 receipt_date 当天的所有入库记录。按 date 列删、不按 created_at、不分门店。

    production_receipt_items 有 on delete cascade，随 production_receipts 自动删。
    """
    target = required_date(receipt_date)
    with connection() as conn:
        tenant_id = ensure_tenant(conn)
        rows = conn.execute(
            "select id from production_receipts where tenant_id = %s and receipt_date = %s order by id asc",
            (tenant_id, target),
        ).fetchall()
        ids = [int(row["id"]) for row in rows]
        if not ids:
            return {"deleted": 0, "deleted_ids": []}

        conn.execute(
            "delete from production_receipts where tenant_id = %s and receipt_date = %s",
            (tenant_id, target),
        )
        return {"deleted": len(ids), "deleted_ids": [receipt_id_label(receipt_id) for receipt_id in ids]}


def cancel_latest_receipt_for_user(user_id: str, receipt_date: str) -> dict[str, Any]:
    with connection() as conn:
        tenant_id = ensure_tenant(conn)
        rows = conn.execute(
            """
            select *
            from production_receipts
            where tenant_id = %s and receipt_date = %s and status <> %s
            order by id desc
            """,
            (tenant_id, required_date(receipt_date), RECEIPT_STATUS_CANCELLED),
        ).fetchall()
        for row in rows:
            raw_ref = row.get("raw_ref") or ""
            if not raw_ref_belongs_to_user(raw_ref, user_id):
                raw_payload = row.get("raw_payload") if isinstance(row.get("raw_payload"), dict) else {}
                if not raw_ref_belongs_to_user(str(raw_payload.get("raw_ref") or ""), user_id):
                    continue

            if row.get("status") == RECEIPT_STATUS_FETCHED:
                return {"outcome": "fetched", "payload": row_to_receipt_payload(conn, row)}

            conn.execute(
                """
                update production_receipts
                set status = %s, cancelled_at = now(), updated_at = now()
                where id = %s
                """,
                (RECEIPT_STATUS_CANCELLED, row["id"]),
            )
            payload = _select_receipt_by_id(conn, int(row["id"]))
            return {"outcome": "cancelled", "payload": payload}
        return {"outcome": "not_found"}

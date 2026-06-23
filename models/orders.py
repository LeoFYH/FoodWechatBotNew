from __future__ import annotations

from typing import Any

from .constants import (
    ORDER_KIND_BASE,
    ORDER_KIND_PATCH,
    ORDER_STATUS_ALL,
    ORDER_STATUS_CANCELLED,
    ORDER_STATUS_NEW,
)
from .db import connection, jsonb
from .order_fetch_events import insert_mark_fetched_event
from .order_items import insert_order_item, rows_to_payload_items
from .stores import ensure_store
from .tenants import ensure_tenant
from .utils import as_date, as_datetime, legacy_fields, required_date


TABLE = "orders"


def row_to_order_payload(conn: Any, row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("raw_payload") if isinstance(row.get("raw_payload"), dict) else {}
    payload = dict(payload)
    items = conn.execute(
        """
        select *
        from order_items
        where order_id = %s
        order by line_no asc, id asc
        """,
        (row["id"],),
    ).fetchall()

    payload.update(
        {
            "id": int(row["id"]),
            "kind": row.get("kind") or ORDER_KIND_BASE,
            "source": row.get("source") or "",
            "store": row.get("store_name_snapshot") or row.get("store_name") or payload.get("store") or "",
            "items": rows_to_payload_items(items),
            "confirmed": bool(row.get("confirmed")),
            "status": row.get("status") or "",
            "raw_ref": row.get("raw_ref") or "",
            "created_at": row["created_at"].isoformat(timespec="seconds") if row.get("created_at") else "",
            "order_date": row["order_date"].isoformat() if row.get("order_date") else "",
        }
    )

    if row.get("kind") == ORDER_KIND_PATCH:
        payload["change_type"] = row.get("change_type") or payload.get("change_type") or "add"
        payload["raw_text"] = row.get("raw_text") or payload.get("raw_text") or ""
    else:
        payload["order_no"] = row.get("order_no") or payload.get("order_no") or ""
        payload["orderer"] = row.get("orderer_name") or payload.get("orderer") or ""

    if row.get("deliver_date"):
        payload["deliver_date"] = row["deliver_date"].isoformat()
    elif "deliver_date" not in payload:
        payload["deliver_date"] = ""
    return payload


def _select_order_by_id(conn: Any, order_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        select o.*, s.name as store_name
        from orders o
        left join stores s on s.id = o.store_id
        where o.id = %s
        """,
        (order_id,),
    ).fetchone()
    if not row:
        return None
    return row_to_order_payload(conn, row)


def insert_order_payload(payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(payload)
    legacy_source, legacy_id = legacy_fields(payload)

    with connection() as conn:
        tenant_id = ensure_tenant(conn)
        if legacy_source and legacy_id is not None:
            existing = conn.execute(
                """
                select id
                from orders
                where tenant_id = %s and legacy_source = %s and legacy_id = %s
                """,
                (tenant_id, legacy_source, legacy_id),
            ).fetchone()
            if existing:
                found = _select_order_by_id(conn, int(existing["id"]))
                if found:
                    return found

        store_id = ensure_store(conn, tenant_id, str(payload.get("store") or ""))
        confirmed = bool(payload.get("confirmed"))
        created_at = as_datetime(payload.get("created_at"))
        order_date = required_date(payload.get("order_date"))
        deliver_date = as_date(payload.get("deliver_date"))
        row = conn.execute(
            """
            insert into orders (
                tenant_id, store_id, store_name_snapshot, kind, source, status, confirmed, confirmed_at,
                order_no, orderer_name, order_date, deliver_date, change_type,
                raw_text, raw_ref, raw_payload, legacy_source, legacy_id,
                created_at, updated_at
            )
            values (
                %s, %s, %s, %s, %s, %s, %s, case when %s then now() else null end,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, now()
            )
            returning id
            """,
            (
                tenant_id,
                store_id,
                payload.get("store") or "",
                payload.get("kind") or ORDER_KIND_BASE,
                payload.get("source") or "",
                payload.get("status") or "new",
                confirmed,
                confirmed,
                payload.get("order_no") or "",
                payload.get("orderer") or payload.get("orderer_name") or "",
                order_date,
                deliver_date,
                payload.get("change_type") or None,
                payload.get("raw_text") or "",
                payload.get("raw_ref") or "",
                jsonb(payload),
                legacy_source,
                legacy_id,
                created_at,
            ),
        ).fetchone()
        order_id = int(row["id"])

        for line_no, item in enumerate(payload.get("items") or [], start=1):
            if isinstance(item, dict):
                insert_order_item(conn, tenant_id, order_id, line_no, item)

        return _select_order_by_id(conn, order_id) or {**payload, "id": order_id}


def query_order_payloads(
    status: str | None = None,
    ids: list[int] | None = None,
    order_date: str | None = None,
) -> list[dict[str, Any]]:
    with connection() as conn:
        tenant_id = ensure_tenant(conn)
        clauses = ["o.tenant_id = %s", "o.confirmed = true", "o.status <> %s"]
        params: list[Any] = [tenant_id, ORDER_STATUS_CANCELLED]

        if ids:
            clean_ids = [int(order_id) for order_id in ids if int(order_id) > 0]
            if not clean_ids:
                return []
            clauses.append("o.id = any(%s)")
            params.append(clean_ids)
        elif order_date is not None:
            clauses.append("o.order_date = %s")
            params.append(required_date(order_date))

        if status and status != ORDER_STATUS_ALL and not ids:
            clauses.append("o.status = %s")
            params.append(status)

        rows = conn.execute(
            f"""
            select o.*, s.name as store_name
            from orders o
            left join stores s on s.id = o.store_id
            where {' and '.join(clauses)}
            order by o.id asc
            """,
            params,
        ).fetchall()
        return [row_to_order_payload(conn, row) for row in rows]


def mark_order_payloads_fetched(ids: list[int]) -> dict[str, list[int]]:
    clean_ids = sorted({int(order_id) for order_id in ids if int(order_id) > 0})
    if not clean_ids:
        return {"succeeded": [], "failed": []}

    with connection() as conn:
        tenant_id = ensure_tenant(conn)
        rows = conn.execute(
            """
            select id
            from orders
            where tenant_id = %s and id = any(%s) and status <> %s
            """,
            (tenant_id, clean_ids, ORDER_STATUS_CANCELLED),
        ).fetchall()
        existing_ids = {int(row["id"]) for row in rows}
        succeeded = [order_id for order_id in clean_ids if order_id in existing_ids]
        failed = [order_id for order_id in clean_ids if order_id not in existing_ids]
        if not succeeded:
            return {"succeeded": [], "failed": failed}

        conn.execute(
            """
            update orders
            set status = 'fetched', fetched_at = coalesce(fetched_at, now()), updated_at = now()
            where tenant_id = %s and id = any(%s)
            """,
            (tenant_id, succeeded),
        )
        for order_id in succeeded:
            insert_mark_fetched_event(conn, tenant_id, order_id)
        return {"succeeded": succeeded, "failed": failed}


def unmark_order_payloads(ids: list[int]) -> dict[str, list[int]]:
    clean_ids = sorted({int(order_id) for order_id in ids if int(order_id) > 0})
    if not clean_ids:
        return {"succeeded": [], "failed": []}

    with connection() as conn:
        tenant_id = ensure_tenant(conn)
        rows = conn.execute(
            """
            select id
            from orders
            where tenant_id = %s and id = any(%s) and status <> %s
            """,
            (tenant_id, clean_ids, ORDER_STATUS_CANCELLED),
        ).fetchall()
        existing_ids = {int(row["id"]) for row in rows}
        succeeded = [order_id for order_id in clean_ids if order_id in existing_ids]
        failed = [order_id for order_id in clean_ids if order_id not in existing_ids]
        if not succeeded:
            return {"succeeded": [], "failed": failed}

        conn.execute(
            """
            update orders
            set status = %s, fetched_at = null, updated_at = now()
            where tenant_id = %s and id = any(%s)
            """,
            (ORDER_STATUS_NEW, tenant_id, succeeded),
        )
        return {"succeeded": succeeded, "failed": failed}


def cancel_latest_order_for_user(user_id: str) -> dict[str, Any]:
    with connection() as conn:
        tenant_id = ensure_tenant(conn)
        rows = conn.execute(
            """
            select o.*, s.name as store_name
            from orders o
            left join stores s on s.id = o.store_id
            where o.tenant_id = %s
              and o.confirmed = true
              and o.status <> %s
              and (o.raw_ref = %s or o.raw_ref like %s)
            order by o.id desc
            limit 1
            """,
            (tenant_id, ORDER_STATUS_CANCELLED, user_id, f"{user_id}:%"),
        ).fetchall()
        if not rows:
            return {"outcome": "not_found"}
        row = rows[0]
        if row.get("status") == "fetched":
            return {"outcome": "fetched", "payload": row_to_order_payload(conn, row)}

        conn.execute(
            """
            update orders
            set status = %s, cancelled_at = now(), updated_at = now()
            where id = %s
            """,
            (ORDER_STATUS_CANCELLED, row["id"]),
        )
        payload = _select_order_by_id(conn, int(row["id"]))
        return {"outcome": "cancelled", "payload": payload}

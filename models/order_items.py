from __future__ import annotations

from typing import Any

from .db import jsonb
from .utils import to_json_number


TABLE = "order_items"


def rows_to_payload_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in rows:
        items.append(
            {
                "code": row.get("product_code") or "",
                "name": row.get("product_name") or "",
                "spec": row.get("spec") or "",
                "unit": row.get("unit") or "",
                "qty": to_json_number(row.get("qty")),
                "price": to_json_number(row.get("price")),
                "category": row.get("category") or "",
            }
        )
    return items


def insert_order_item(conn: Any, tenant_id: int, order_id: int, line_no: int, item: dict[str, Any]) -> None:
    conn.execute(
        """
        insert into order_items (
            tenant_id, order_id, line_no, product_code, product_name,
            spec, unit, qty, price, category, raw_payload
        )
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            tenant_id,
            order_id,
            line_no,
            item.get("code") or "",
            item.get("name") or "",
            item.get("spec") or "",
            item.get("unit") or "",
            item.get("qty"),
            item.get("price"),
            item.get("category") or "",
            jsonb(item),
        ),
    )


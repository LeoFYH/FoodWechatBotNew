from __future__ import annotations

from typing import Any

from .db import jsonb
from .utils import to_json_number


TABLE = "production_receipt_items"


def rows_to_payload_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in rows:
        items.append(
            {
                "code": row.get("product_code"),
                "name": row.get("product_name") or "",
                "spec": row.get("spec"),
                "unit": row.get("unit"),
                "qty": to_json_number(row.get("qty")),
            }
        )
    return items


def insert_receipt_item(conn: Any, tenant_id: int, receipt_id: int, line_no: int, item: dict[str, Any]) -> None:
    conn.execute(
        """
        insert into production_receipt_items (
            tenant_id, receipt_id, line_no, product_code, product_name,
            spec, unit, qty, raw_payload
        )
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            tenant_id,
            receipt_id,
            line_no,
            item.get("code"),
            item.get("name") or "",
            item.get("spec"),
            item.get("unit"),
            item.get("qty"),
            jsonb(item),
        ),
    )


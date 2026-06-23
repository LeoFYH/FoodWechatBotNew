from __future__ import annotations

from typing import Any


TABLE = "stores"


def ensure_store(conn: Any, tenant_id: int, store_name: str) -> int | None:
    store_name = str(store_name or "").strip()
    if not store_name:
        return None
    row = conn.execute(
        """
        insert into stores (tenant_id, name)
        values (%s, %s)
        on conflict (tenant_id, name)
        do update set updated_at = now()
        returning id
        """,
        (tenant_id, store_name),
    ).fetchone()
    return int(row["id"])


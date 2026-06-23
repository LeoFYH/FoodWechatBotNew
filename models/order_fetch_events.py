from __future__ import annotations

from typing import Any


TABLE = "order_fetch_events"


def insert_mark_fetched_event(conn: Any, tenant_id: int, order_id: int) -> None:
    conn.execute(
        """
        insert into order_fetch_events (tenant_id, order_id)
        values (%s, %s)
        """,
        (tenant_id, order_id),
    )


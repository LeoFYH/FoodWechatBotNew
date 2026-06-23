from __future__ import annotations

from .constants import CURSOR_CHANNEL_WECOM_KF
from .db import connection
from .tenants import ensure_tenant


TABLE = "channel_cursors"


def load_kf_cursors() -> dict[str, str]:
    with connection() as conn:
        tenant_id = ensure_tenant(conn)
        rows = conn.execute(
            """
            select cursor_key, cursor_value
            from channel_cursors
            where tenant_id = %s and channel = %s
            """,
            (tenant_id, CURSOR_CHANNEL_WECOM_KF),
        ).fetchall()
        return {str(row["cursor_key"]): str(row["cursor_value"] or "") for row in rows}


def save_kf_cursors(cursors: dict[str, str]) -> None:
    with connection() as conn:
        tenant_id = ensure_tenant(conn)
        keys = [str(key) for key in cursors.keys()]
        if keys:
            conn.execute(
                """
                delete from channel_cursors
                where tenant_id = %s and channel = %s and cursor_key <> all(%s)
                """,
                (tenant_id, CURSOR_CHANNEL_WECOM_KF, keys),
            )
        else:
            conn.execute(
                "delete from channel_cursors where tenant_id = %s and channel = %s",
                (tenant_id, CURSOR_CHANNEL_WECOM_KF),
            )

        for key, value in cursors.items():
            conn.execute(
                """
                insert into channel_cursors (
                    tenant_id, channel, cursor_key, cursor_value, updated_at
                )
                values (%s, %s, %s, %s, now())
                on conflict (tenant_id, channel, cursor_key)
                do update set cursor_value = excluded.cursor_value, updated_at = now()
                """,
                (tenant_id, CURSOR_CHANNEL_WECOM_KF, str(key), str(value)),
            )

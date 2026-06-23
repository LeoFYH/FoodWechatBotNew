from __future__ import annotations

from typing import Any

from .db import default_tenant_code


TABLE = "tenants"


def ensure_tenant(conn: Any, code: str | None = None) -> int:
    tenant_code = (code or default_tenant_code()).strip() or "default"
    row = conn.execute(
        """
        insert into tenants (code, name)
        values (%s, %s)
        on conflict (code)
        do update set updated_at = now()
        returning id
        """,
        (tenant_code, tenant_code),
    ).fetchone()
    return int(row["id"])


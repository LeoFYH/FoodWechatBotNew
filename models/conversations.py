from __future__ import annotations

from typing import Any

from .constants import (
    STATE_CHANNEL_MEMORY,
    STATE_CHANNEL_SESSION,
)
from .db import connection, jsonb
from .tenants import ensure_tenant


TABLE = "conversations"


def _load_state_map(channel: str) -> dict[str, Any]:
    with connection() as conn:
        tenant_id = ensure_tenant(conn)
        rows = conn.execute(
            """
            select external_session_key, state
            from conversations
            where tenant_id = %s and channel = %s and external_session_key is not null
            order by id asc
            """,
            (tenant_id, channel),
        ).fetchall()
        result: dict[str, Any] = {}
        for row in rows:
            state = row.get("state") if isinstance(row.get("state"), dict) else {}
            result[str(row["external_session_key"])] = state.get("value")
        return result


def _save_state_map(channel: str, data: dict[str, Any]) -> None:
    with connection() as conn:
        tenant_id = ensure_tenant(conn)
        keys = [str(key) for key in data.keys()]
        if keys:
            conn.execute(
                """
                delete from conversations
                where tenant_id = %s and channel = %s and external_session_key <> all(%s)
                """,
                (tenant_id, channel, keys),
            )
        else:
            conn.execute(
                "delete from conversations where tenant_id = %s and channel = %s",
                (tenant_id, channel),
            )

        for key, value in data.items():
            conn.execute(
                """
                insert into conversations (
                    tenant_id, channel, external_session_key, mode, state, updated_at
                )
                values (%s, %s, %s, 'interview', %s, now())
                on conflict (tenant_id, channel, external_session_key)
                do update set state = excluded.state, updated_at = now()
                """,
                (tenant_id, channel, str(key), jsonb({"value": value})),
            )


def load_memory() -> dict:
    return _load_state_map(STATE_CHANNEL_MEMORY)


def save_memory(memory: dict) -> None:
    _save_state_map(STATE_CHANNEL_MEMORY, memory)


def load_session_state() -> dict[str, dict[str, Any]]:
    raw_state = _load_state_map(STATE_CHANNEL_SESSION)
    return {
        str(user_id): record
        for user_id, record in raw_state.items()
        if isinstance(record, dict)
    }


def save_session_state(state: dict[str, dict[str, Any]]) -> None:
    _save_state_map(STATE_CHANNEL_SESSION, state)


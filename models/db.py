from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

from .constants import POSTGRES_BACKENDS

DEFAULT_POSTGRES_CONNECT_TIMEOUT_SECONDS = 5
DEFAULT_POSTGRES_STATEMENT_TIMEOUT_MS = 30_000
DEFAULT_POSTGRES_LOCK_TIMEOUT_MS = 10_000


def _get_int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return max(parsed, 0)


def is_enabled() -> bool:
    return os.getenv("DATABASE_BACKEND", "sqlite").strip().lower() in POSTGRES_BACKENDS


def database_url() -> str:
    return (
        os.getenv("DATABASE_URL")
        or os.getenv("POSTGRES_DSN")
        or os.getenv("PG_DSN")
        or ""
    ).strip()


def default_tenant_code() -> str:
    return os.getenv("DEFAULT_TENANT_CODE", "default").strip() or "default"


def connect_timeout_seconds() -> int:
    return _get_int_env("POSTGRES_CONNECT_TIMEOUT_SECONDS", DEFAULT_POSTGRES_CONNECT_TIMEOUT_SECONDS)


def statement_timeout_ms() -> int:
    return _get_int_env("POSTGRES_STATEMENT_TIMEOUT_MS", DEFAULT_POSTGRES_STATEMENT_TIMEOUT_MS)


def lock_timeout_ms() -> int:
    return _get_int_env("POSTGRES_LOCK_TIMEOUT_MS", DEFAULT_POSTGRES_LOCK_TIMEOUT_MS)


def _load_psycopg():
    try:
        import psycopg
        from psycopg.rows import dict_row
        from psycopg.types.json import Jsonb
    except ImportError as exc:
        raise RuntimeError(
            "PostgreSQL backend requires psycopg. Run: python3 -m pip install -r requirements.txt"
        ) from exc
    return psycopg, dict_row, Jsonb


@contextmanager
def connection() -> Iterator[Any]:
    dsn = database_url()
    if not dsn:
        raise RuntimeError("DATABASE_URL or POSTGRES_DSN is required when DATABASE_BACKEND=postgres")

    psycopg, dict_row, _jsonb = _load_psycopg()
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=connect_timeout_seconds()) as conn:
        current_statement_timeout = statement_timeout_ms()
        if current_statement_timeout > 0:
            conn.execute(f"set statement_timeout = {current_statement_timeout}")
        current_lock_timeout = lock_timeout_ms()
        if current_lock_timeout > 0:
            conn.execute(f"set lock_timeout = {current_lock_timeout}")
        yield conn


def jsonb(value: Any) -> Any:
    _psycopg, _dict_row, Jsonb = _load_psycopg()
    return Jsonb(value if value is not None else {})

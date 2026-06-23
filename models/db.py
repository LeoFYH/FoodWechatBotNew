from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

from .constants import POSTGRES_BACKENDS


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
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        yield conn


def jsonb(value: Any) -> Any:
    _psycopg, _dict_row, Jsonb = _load_psycopg()
    return Jsonb(value if value is not None else {})


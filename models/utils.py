from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any


def as_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def as_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def required_date(value: Any) -> date:
    return as_date(value) or datetime.now().date()


def as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if value not in (None, ""):
        text = str(value).strip()
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            pass
    return datetime.now()


def to_json_number(value: Any) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def raw_ref_belongs_to_user(raw_ref: str, user_id: str) -> bool:
    raw_ref = str(raw_ref or "")
    return raw_ref == user_id or raw_ref.startswith(f"{user_id}:")


def legacy_fields(payload: dict[str, Any]) -> tuple[str | None, int | None]:
    legacy_source = payload.get("legacy_source") or payload.get("_legacy_source")
    legacy_id = as_int(payload.get("legacy_id") or payload.get("_legacy_id"))
    if legacy_source and legacy_id is not None:
        return str(legacy_source), legacy_id
    return None, None

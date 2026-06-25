"""Redis-fronted PostgreSQL storage models for FoodWechatBotNew."""

from __future__ import annotations

import logging
import time
from typing import Any

from . import channel_cursors as pg_channel_cursors
from . import conversations as pg_conversations
from . import orders as pg_orders
from . import production_receipts as pg_production_receipts
from . import redis_cache
from .db import database_url, default_tenant_code, is_enabled

logger = logging.getLogger("wechatclaw")


def _elapsed_ms(started_at: float) -> int:
    return int((time.perf_counter() - started_at) * 1000)


def redis_url() -> str:
    return redis_cache.redis_url()


def is_redis_cache_enabled() -> bool:
    return redis_cache.is_enabled()


def _require_redis() -> None:
    redis_cache.require_available()


def _state_key(name: str) -> str:
    return redis_cache.make_key("state", name)


def _order_query_key(status: str | None, ids: list[int] | None, order_date: str | None) -> str:
    return redis_cache.make_key(
        "orders",
        "query",
        redis_cache.hash_args(
            {
                "ids": ids or [],
                "order_date": order_date,
                "status": status,
            }
        ),
    )


def _order_query_pattern() -> str:
    return redis_cache.make_key("orders", "query", "*")


def _receipt_query_key(receipt_date: str, status: str | None) -> str:
    return redis_cache.make_key(
        "receipts",
        "query",
        redis_cache.hash_args(
            {
                "date": receipt_date,
                "status": status,
            }
        ),
    )


def _receipt_query_pattern() -> str:
    return redis_cache.make_key("receipts", "query", "*")


def load_memory() -> dict:
    _require_redis()
    return redis_cache.load_or_set(_state_key("memory"), pg_conversations.load_memory)


def save_memory(memory: dict) -> None:
    _require_redis()
    redis_cache.record_operation("memory.save", {"memory": memory})
    pg_conversations.save_memory(memory)
    redis_cache.set_json(_state_key("memory"), memory)


def load_session_state() -> dict[str, dict[str, Any]]:
    _require_redis()
    return redis_cache.load_or_set(_state_key("session_state"), pg_conversations.load_session_state)


def save_session_state(state: dict[str, dict[str, Any]]) -> None:
    _require_redis()
    redis_cache.record_operation("session_state.save", {"state": state})
    pg_conversations.save_session_state(state)
    redis_cache.set_json(_state_key("session_state"), state)


def insert_order_payload(payload: dict[str, Any]) -> dict[str, Any]:
    _require_redis()
    started_at = time.perf_counter()
    redis_cache.record_operation("orders.insert", {"payload": payload})
    redis_record_ms = _elapsed_ms(started_at)

    pg_started_at = time.perf_counter()
    result = pg_orders.insert_order_payload(payload)
    pg_ms = _elapsed_ms(pg_started_at)

    invalidate_started_at = time.perf_counter()
    redis_cache.delete_pattern(_order_query_pattern())
    redis_invalidate_ms = _elapsed_ms(invalidate_started_at)

    redis_set_ms = 0
    if result.get("id") is not None:
        set_started_at = time.perf_counter()
        redis_cache.set_json(redis_cache.make_key("orders", "id", result["id"]), result)
        redis_set_ms = _elapsed_ms(set_started_at)
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    logger.info(
        "storage_order_insert_done order_id=%s lines=%s redis_record_ms=%s pg_ms=%s "
        "redis_invalidate_ms=%s redis_set_ms=%s total_ms=%s",
        result.get("id"),
        len(items),
        redis_record_ms,
        pg_ms,
        redis_invalidate_ms,
        redis_set_ms,
        _elapsed_ms(started_at),
    )
    return result


def query_order_payloads(
    status: str | None = None,
    ids: list[int] | None = None,
    order_date: str | None = None,
) -> list[dict[str, Any]]:
    _require_redis()
    return redis_cache.load_or_set(
        _order_query_key(status, ids, order_date),
        lambda: pg_orders.query_order_payloads(status=status, ids=ids, order_date=order_date),
    )


def mark_order_payloads_fetched(ids: list[int]) -> dict[str, list[int]]:
    _require_redis()
    redis_cache.record_operation("orders.mark_fetched", {"ids": ids})
    result = pg_orders.mark_order_payloads_fetched(ids)
    redis_cache.delete_pattern(_order_query_pattern())
    return result


def unmark_order_payloads(ids: list[int]) -> dict[str, list[int]]:
    _require_redis()
    redis_cache.record_operation("orders.unmark", {"ids": ids})
    result = pg_orders.unmark_order_payloads(ids)
    redis_cache.delete_pattern(_order_query_pattern())
    return result


def cancel_latest_order_for_user(user_id: str) -> dict[str, Any]:
    _require_redis()
    redis_cache.record_operation("orders.cancel_latest", {"user_id": user_id})
    result = pg_orders.cancel_latest_order_for_user(user_id)
    redis_cache.delete_pattern(_order_query_pattern())
    return result


def insert_receipt_payload(payload: dict[str, Any]) -> dict[str, Any]:
    _require_redis()
    redis_cache.record_operation("production_receipts.insert", {"payload": payload})
    result = pg_production_receipts.insert_receipt_payload(payload)
    redis_cache.delete_pattern(_receipt_query_pattern())
    return result


def query_receipt_payloads(date: str, status: str | None = None) -> list[dict[str, Any]]:
    _require_redis()
    return redis_cache.load_or_set(
        _receipt_query_key(date, status),
        lambda: pg_production_receipts.query_receipt_payloads(date, status=status),
    )


def mark_receipt_payloads_fetched(ids: list[Any]) -> dict[str, list[str]]:
    _require_redis()
    redis_cache.record_operation("production_receipts.mark_fetched", {"ids": ids})
    result = pg_production_receipts.mark_receipt_payloads_fetched(ids)
    redis_cache.delete_pattern(_receipt_query_pattern())
    return result


def unmark_receipt_payloads(ids: list[Any]) -> dict[str, list[str]]:
    _require_redis()
    redis_cache.record_operation("production_receipts.unmark", {"ids": ids})
    result = pg_production_receipts.unmark_receipt_payloads(ids)
    redis_cache.delete_pattern(_receipt_query_pattern())
    return result


def cancel_latest_receipt_for_user(user_id: str, receipt_date: str) -> dict[str, Any]:
    _require_redis()
    redis_cache.record_operation(
        "production_receipts.cancel_latest",
        {"receipt_date": receipt_date, "user_id": user_id},
    )
    result = pg_production_receipts.cancel_latest_receipt_for_user(user_id, receipt_date)
    redis_cache.delete_pattern(_receipt_query_pattern())
    return result


def load_kf_cursors() -> dict[str, str]:
    _require_redis()
    return redis_cache.load_or_set(_state_key("kf_cursors"), pg_channel_cursors.load_kf_cursors)


def save_kf_cursors(cursors: dict[str, str]) -> None:
    _require_redis()
    redis_cache.record_operation("kf_cursors.save", {"cursors": cursors})
    pg_channel_cursors.save_kf_cursors(cursors)
    redis_cache.set_json(_state_key("kf_cursors"), cursors)


__all__ = [
    "cancel_latest_order_for_user",
    "cancel_latest_receipt_for_user",
    "database_url",
    "default_tenant_code",
    "insert_order_payload",
    "insert_receipt_payload",
    "is_enabled",
    "is_redis_cache_enabled",
    "load_kf_cursors",
    "load_memory",
    "load_session_state",
    "mark_order_payloads_fetched",
    "mark_receipt_payloads_fetched",
    "query_order_payloads",
    "query_receipt_payloads",
    "redis_url",
    "save_kf_cursors",
    "save_memory",
    "save_session_state",
    "unmark_order_payloads",
    "unmark_receipt_payloads",
]

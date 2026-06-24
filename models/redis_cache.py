from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Callable, TypeVar


DEFAULT_CACHE_TTL_SECONDS = 300
DEFAULT_STREAM_MAXLEN = 10000
DEFAULT_REDIS_SOCKET_TIMEOUT_SECONDS = 5
DEFAULT_REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS = 5

T = TypeVar("T")
_MISSING = object()


def _get_bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _get_int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return max(parsed, 1)


def redis_url() -> str:
    return os.getenv("REDIS_URL", "").strip()


def cache_enabled() -> bool:
    return _get_bool_env("REDIS_CACHE_ENABLED", True)


def key_prefix() -> str:
    return os.getenv("REDIS_KEY_PREFIX", "foodwechatbot").strip() or "foodwechatbot"


def tenant_code() -> str:
    return os.getenv("DEFAULT_TENANT_CODE", "default").strip() or "default"


def cache_ttl_seconds() -> int:
    raw_value = os.getenv("REDIS_CACHE_TTL_SECONDS", str(DEFAULT_CACHE_TTL_SECONDS))
    try:
        value = int(raw_value)
    except ValueError:
        return DEFAULT_CACHE_TTL_SECONDS
    return max(value, 1)


def stream_key() -> str:
    configured = os.getenv("REDIS_OPERATION_STREAM", "").strip()
    if configured:
        return configured
    return make_key("storage", "events")


def stream_maxlen() -> int:
    raw_value = os.getenv("REDIS_STREAM_MAXLEN", str(DEFAULT_STREAM_MAXLEN))
    try:
        value = int(raw_value)
    except ValueError:
        return DEFAULT_STREAM_MAXLEN
    return max(value, 100)


def socket_timeout_seconds() -> int:
    return _get_int_env("REDIS_SOCKET_TIMEOUT_SECONDS", DEFAULT_REDIS_SOCKET_TIMEOUT_SECONDS)


def socket_connect_timeout_seconds() -> int:
    return _get_int_env(
        "REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS",
        DEFAULT_REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS,
    )


def is_enabled() -> bool:
    return cache_enabled() and bool(redis_url())


def _load_redis_client():
    try:
        import redis
    except ImportError as exc:
        raise RuntimeError(
            "PostgreSQL backend requires Redis cache. Run: python3 -m pip install -r requirements.txt"
        ) from exc
    return redis.Redis.from_url(
        redis_url(),
        decode_responses=True,
        socket_timeout=socket_timeout_seconds(),
        socket_connect_timeout=socket_connect_timeout_seconds(),
    )


def client():
    if not cache_enabled():
        raise RuntimeError("Redis cache is disabled. Set REDIS_CACHE_ENABLED=true for PostgreSQL backend.")
    if not redis_url():
        raise RuntimeError("REDIS_URL is required when DATABASE_BACKEND=postgres.")
    return _load_redis_client()


def require_available() -> None:
    client().ping()


def make_key(*parts: Any) -> str:
    clean_parts = [str(part).strip(":") for part in parts if str(part)]
    return ":".join([key_prefix(), tenant_code(), *clean_parts])


def hash_args(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def get_json(cache_key: str) -> Any:
    raw = client().get(cache_key)
    if raw is None:
        return _MISSING
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        client().delete(cache_key)
        return _MISSING


def set_json(cache_key: str, value: Any, ttl_seconds: int | None = None) -> None:
    ttl = ttl_seconds or cache_ttl_seconds()
    raw = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    client().set(cache_key, raw, ex=ttl)


def load_or_set(cache_key: str, loader: Callable[[], T], ttl_seconds: int | None = None) -> T:
    cached = get_json(cache_key)
    if cached is not _MISSING:
        return cached
    value = loader()
    set_json(cache_key, value, ttl_seconds=ttl_seconds)
    return value


def delete_pattern(pattern: str) -> int:
    redis_client = client()
    deleted = 0
    batch: list[str] = []
    for cache_key in redis_client.scan_iter(pattern, count=200):
        batch.append(cache_key)
        if len(batch) >= 200:
            deleted += redis_client.delete(*batch)
            batch = []
    if batch:
        deleted += redis_client.delete(*batch)
    return deleted


def record_operation(operation: str, payload: dict[str, Any]) -> str:
    event = {
        "operation": operation,
        "tenant": tenant_code(),
        "created_at": str(time.time()),
        "payload": json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":")),
    }
    return str(client().xadd(stream_key(), event, maxlen=stream_maxlen(), approximate=True))

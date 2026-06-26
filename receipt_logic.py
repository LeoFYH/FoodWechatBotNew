"""receipt_logic.py —— 产成品入库(receipt)领域纯逻辑层(从 main.py 原样搬出,P6)。

receipt 的领域常量 + 归一化 + 缺字段校验 + 回复模板 + 状态/编号 helper。
纯函数:只依赖 order_normalize 的归一化原语,绝不 import main、不碰 SQLite、不读 env、不持 client。
main 通过 `from receipt_logic import *` 门面 re-export(RECEIPT 常量与函数,调用点/测试可见性不变);
store_sqlite(receipt 后端)与 vision_import(receipt 照片)按 DAG import 本模块。

注意:summarize_receipt_for_reply 是写库后的撤回回复模板,原样保留,内容待 intent 终局阶段统一处理。
"""

from __future__ import annotations

from typing import Any

from order_normalize import (
    clean_order_value,
    fallback_order_date,
    normalize_number,
    normalize_order_date_text,
    now_iso,
    optional_text,
)


RECEIPT_STATUS_NEW = "new"
RECEIPT_STATUS_CONFIRMED = "confirmed"
RECEIPT_STATUS_FETCHED = "fetched"
RECEIPT_STATUS_CANCELLED = "cancelled"
RECEIPT_STATUS_ALL = "all"
RECEIPT_API_STATUSES = {RECEIPT_STATUS_NEW, RECEIPT_STATUS_FETCHED, RECEIPT_STATUS_ALL}
RECEIPT_STORAGE_STATUSES = {RECEIPT_STATUS_CONFIRMED, RECEIPT_STATUS_FETCHED, RECEIPT_STATUS_CANCELLED}


def summarize_receipt_for_reply(payload: dict[str, Any]) -> str:
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    if not items:
        return str(payload.get("date") or "今天")
    first = items[0] if isinstance(items[0], dict) else {}
    name = str(first.get("name") or "未填成品")
    qty = first.get("qty")
    unit = str(first.get("unit") or "")
    qty_text = "" if qty is None else f"{qty}{unit}"
    more = "" if len(items) == 1 else f"等{len(items)}项"
    return f"{name}{qty_text}{more}".strip()


def normalize_receipt_item(item: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        "code": optional_text(item.get("code"), null_for_empty=True),
        "name": optional_text(item.get("name")) or "",
        "spec": optional_text(item.get("spec"), null_for_empty=True),
        "unit": optional_text(item.get("unit"), null_for_empty=True),
        "qty": normalize_number(item.get("qty")),
    }
    return normalized


def normalize_receipt_payload(data: dict[str, Any]) -> dict[str, Any]:
    created_at = clean_order_value(data.get("created_at")) or now_iso()
    date = normalize_order_date_text(data.get("date")) or fallback_order_date(created_at)
    status = clean_order_value(data.get("status")) or RECEIPT_STATUS_CONFIRMED
    if status == RECEIPT_STATUS_NEW:
        status = RECEIPT_STATUS_CONFIRMED
    if status not in RECEIPT_STORAGE_STATUSES:
        status = RECEIPT_STATUS_CONFIRMED
    items = data.get("items")
    if not isinstance(items, list):
        items = []

    normalized_items = [
        normalize_receipt_item(item)
        for item in items
        if isinstance(item, dict)
    ]
    normalized_items = [
        item
        for item in normalized_items
        if item.get("name") or item.get("qty") is not None
    ]

    payload: dict[str, Any] = {
        "date": date,
        "items": normalized_items,
        "status": status,
        "created_at": created_at,
    }
    if data.get("id") not in (None, ""):
        payload["id"] = str(data.get("id"))
    if data.get("raw_ref"):
        payload["raw_ref"] = clean_order_value(data.get("raw_ref"))
    return payload


def receipt_missing_fields(payload: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if not payload.get("date"):
        missing.append("入库日期")
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        missing.append("成品和数量")
        return missing

    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            missing.append(f"第{index}项成品")
            continue
        if not item.get("name"):
            missing.append(f"第{index}项成品名称")
        if item.get("qty") is None:
            missing.append(f"第{index}项数量")
    return missing


def receipt_status_to_storage_filter(status: str | None) -> str | None:
    if not status or status == RECEIPT_STATUS_NEW:
        return RECEIPT_STATUS_CONFIRMED
    if status == RECEIPT_STATUS_ALL:
        return None
    return status


def receipt_id_label(receipt_id: int) -> str:
    return f"r{int(receipt_id):03d}"


def parse_receipt_id_values(ids: list[Any]) -> tuple[list[int], list[str]]:
    clean_ids: list[int] = []
    failed: list[str] = []
    seen: set[int] = set()
    for raw_id in ids:
        text = str(raw_id or "").strip()
        if text.lower().startswith("r"):
            text = text[1:]
        try:
            receipt_id = int(text)
        except ValueError:
            failed.append(str(raw_id))
            continue
        if receipt_id <= 0:
            failed.append(str(raw_id))
            continue
        if receipt_id in seen:
            continue
        seen.add(receipt_id)
        clean_ids.append(receipt_id)
    return sorted(clean_ids), failed


__all__ = [
    "RECEIPT_STATUS_NEW",
    "RECEIPT_STATUS_CONFIRMED",
    "RECEIPT_STATUS_FETCHED",
    "RECEIPT_STATUS_CANCELLED",
    "RECEIPT_STATUS_ALL",
    "RECEIPT_API_STATUSES",
    "RECEIPT_STORAGE_STATUSES",
    "summarize_receipt_for_reply",
    "normalize_receipt_item",
    "normalize_receipt_payload",
    "receipt_missing_fields",
    "receipt_status_to_storage_filter",
    "receipt_id_label",
    "parse_receipt_id_values",
]

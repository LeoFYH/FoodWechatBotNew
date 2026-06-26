"""order_normalize.py —— 订单归一化纯函数层（从 main.py 原样搬出）。

本模块只做"把各种来源（Excel/照片/文字）的订单数据清洗、归一化成统一结构"，
全是纯函数：只依赖标准库 + openpyxl 的日期换算，绝不 import main，无任何 env/锁/client 依赖，
因此可被 main.py 通过 `from order_normalize import *` 门面 re-export，调用点与测试可见性完全不变。

注意（重构约定）：format_order_draft_summary 等"草稿回显模板"原样保留，
内容不在本阶段优化——草稿回显是写库前用户确认依据，必须逐字模板、绝不过 LLM。
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any

from openpyxl.utils.datetime import from_excel


ORDER_KIND_BASE = "base"
ORDER_KIND_PATCH = "patch"
ORDER_SOURCE_EXCEL = "excel"
ORDER_SOURCE_PHOTO = "photo"
ORDER_SOURCE_TEXT = "text"
ORDER_STATUS_NEW = "new"
ORDER_STATUS_FETCHED = "fetched"
ORDER_STATUS_CANCELLED = "cancelled"
ORDER_STATUS_ALL = "all"
ORDER_CHANGE_ADD = "add"
ORDER_CHANGE_MODIFY = "modify"
ORDER_KINDS = {ORDER_KIND_BASE, ORDER_KIND_PATCH}
ORDER_SOURCES = {ORDER_SOURCE_EXCEL, ORDER_SOURCE_PHOTO, ORDER_SOURCE_TEXT}
ORDER_STATUSES = {ORDER_STATUS_NEW, ORDER_STATUS_FETCHED, ORDER_STATUS_ALL}
ORDER_CHANGE_TYPES = {ORDER_CHANGE_ADD, ORDER_CHANGE_MODIFY}

BASE_ORDER_FIELDS = [
    "id",
    "kind",
    "source",
    "store",
    "order_no",
    "orderer",
    "order_date",
    "deliver_date",
    "items",
    "confirmed",
    "status",
    "raw_ref",
    "created_at",
]
PATCH_ORDER_FIELDS = [
    "id",
    "kind",
    "source",
    "store",
    "items",
    "change_type",
    "order_date",
    "deliver_date",
    "confirmed",
    "status",
    "raw_text",
    "raw_ref",
    "created_at",
]


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def clean_export_value(value: str) -> str:
    value = value.strip()
    value = re.sub(r"\s+", " ", value)
    return value.strip(" ，,。.;；")


def clean_order_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return clean_export_value(str(value))


def optional_text(value: Any, *, null_for_empty: bool = False) -> str | None:
    cleaned = clean_order_value(value)
    if cleaned:
        return cleaned
    if null_for_empty:
        return None
    return ""


def normalize_number(value: Any) -> int | float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else value

    text = str(value).strip()
    if not text:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    if not match:
        return None
    number = float(match.group(0))
    return int(number) if number.is_integer() else number


def normalize_date_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 20000 <= float(value) <= 80000:
        try:
            return from_excel(value).date().isoformat()
        except (TypeError, ValueError):
            pass
    return clean_order_value(value)


def make_iso_date(year: int, month: int, day: int) -> str:
    return datetime(year, month, day).date().isoformat()


def normalize_order_date_text(value: Any) -> str:
    text = normalize_date_text(value)
    if not text:
        return ""

    year = datetime.now().year
    full_match = re.search(
        r"(?<!\d)(20\d{2})[.\-/年](\d{1,2})[.\-/月](\d{1,2})(?:日)?",
        text,
    )
    if full_match:
        try:
            return make_iso_date(
                int(full_match.group(1)),
                int(full_match.group(2)),
                int(full_match.group(3)),
            )
        except ValueError:
            return text

    short_match = re.search(
        r"(?<!\d)(\d{1,2})[.\-/月](\d{1,2})(?:日)?",
        text,
    )
    if short_match:
        try:
            return make_iso_date(year, int(short_match.group(1)), int(short_match.group(2)))
        except ValueError:
            return text

    return text


def extract_explicit_order_date(text: str) -> str:
    if not text:
        return ""

    patterns = [
        r"(?<!\d)(20\d{2})[.\-/年](\d{1,2})[.\-/月](\d{1,2})(?:日)?\s*(?:订|下单|订单)",
        r"(?<!\d)(\d{1,2})[.\-/月](\d{1,2})(?:日)?\s*(?:订|下单|订单)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        try:
            if len(match.groups()) == 3:
                return make_iso_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            return make_iso_date(datetime.now().year, int(match.group(1)), int(match.group(2)))
        except ValueError:
            return ""
    return ""


def fallback_order_date(created_at: str) -> str:
    if created_at:
        try:
            return datetime.fromisoformat(created_at).date().isoformat()
        except ValueError:
            pass
    return datetime.now().date().isoformat()


def parse_iso_date(value: Any) -> date | None:
    text = normalize_order_date_text(value)
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def repair_photo_order_dates(data: dict[str, Any]) -> dict[str, Any]:
    if clean_order_value(data.get("source")) != ORDER_SOURCE_PHOTO:
        return data

    order_dt = parse_iso_date(data.get("order_date"))
    if order_dt is None:
        return data

    repaired = dict(data)
    current_year = datetime.now().year
    deliver_dt = parse_iso_date(data.get("deliver_date"))
    target_year = deliver_dt.year if deliver_dt else current_year

    if order_dt.year == target_year:
        return repaired

    candidate: date | None = None
    try:
        candidate = order_dt.replace(year=target_year)
    except ValueError:
        candidate = None

    if candidate and deliver_dt and abs((deliver_dt - candidate).days) <= 14:
        repaired["order_date"] = candidate.isoformat()
        return repaired

    if candidate and not deliver_dt and abs(order_dt.year - current_year) > 1:
        repaired["order_date"] = candidate.isoformat()

    return repaired


def normalize_deliver_date_text(value: Any) -> str:
    text = normalize_date_text(value)
    if not text:
        return ""

    today = datetime.now().date()
    if "后天" in text:
        return (today + timedelta(days=2)).isoformat()
    if any(word in text for word in ("明天", "明日", "明早", "明晚", "明晨")):
        return (today + timedelta(days=1)).isoformat()
    if any(word in text for word in ("今天", "今日", "今晚", "今早")):
        return today.isoformat()
    return text


def generate_contract_order_no(store: str, order_date: str) -> str:
    date_part = order_date or datetime.now().strftime("%Y-%m-%d")
    store_part = store or "未确认门店"
    raw = f"{store_part}-{date_part}"
    return re.sub(r"\s+", "", raw)


def normalize_base_item(item: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        "code": optional_text(item.get("code")),
        "name": optional_text(item.get("name")),
        "spec": optional_text(item.get("spec")),
        "unit": optional_text(item.get("unit")),
        "qty": normalize_number(item.get("qty")),
        "price": normalize_number(item.get("price")),
        "category": optional_text(item.get("category")),
    }
    return normalized


def normalize_patch_item(item: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        "code": optional_text(item.get("code"), null_for_empty=True),
        "name": optional_text(item.get("name")),
        "spec": optional_text(item.get("spec"), null_for_empty=True),
        "unit": optional_text(item.get("unit"), null_for_empty=True),
        "qty": normalize_number(item.get("qty")),
    }
    return normalized


def normalize_order_items(data: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    items = data.get("items")
    if not isinstance(items, list):
        items = []

    normalized_items: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        normalized = normalize_patch_item(item) if kind == ORDER_KIND_PATCH else normalize_base_item(item)
        if normalized.get("name") or normalized.get("code") or normalized.get("qty") is not None:
            normalized_items.append(normalized)
    return normalized_items


def normalize_order_payload(data: dict[str, Any]) -> dict[str, Any]:
    data = repair_photo_order_dates(data)
    source = clean_order_value(data.get("source"))
    kind = clean_order_value(data.get("kind"))
    if kind not in ORDER_KINDS:
        kind = ORDER_KIND_PATCH if source == ORDER_SOURCE_TEXT else ORDER_KIND_BASE
    if source not in ORDER_SOURCES:
        source = ORDER_SOURCE_TEXT if kind == ORDER_KIND_PATCH else ORDER_SOURCE_EXCEL

    status = clean_order_value(data.get("status")) or ORDER_STATUS_NEW
    if status not in ORDER_STATUSES:
        status = ORDER_STATUS_NEW

    created_at = clean_order_value(data.get("created_at")) or now_iso()
    store = optional_text(data.get("store")) or ("未确认门店" if source == ORDER_SOURCE_EXCEL else "")
    normalized: dict[str, Any] = {
        "kind": kind,
        "source": source,
        "store": store,
        "items": normalize_order_items(data, kind),
        "confirmed": bool(data.get("confirmed")),
        "status": status,
        "raw_ref": optional_text(data.get("raw_ref")) or "",
        "created_at": created_at,
    }

    if data.get("id") not in (None, ""):
        try:
            normalized["id"] = int(data["id"])
        except (TypeError, ValueError):
            pass

    if kind == ORDER_KIND_BASE:
        order_date = normalize_order_date_text(data.get("order_date")) or fallback_order_date(created_at)
        deliver_date = normalize_deliver_date_text(data.get("deliver_date"))
        normalized["order_no"] = optional_text(data.get("order_no")) or generate_contract_order_no(
            store,
            order_date,
        )
        normalized["orderer"] = optional_text(data.get("orderer")) or ""
        normalized["order_date"] = order_date
        normalized["deliver_date"] = deliver_date
        return {field: normalized.get(field) for field in BASE_ORDER_FIELDS if field in normalized}

    change_type = clean_order_value(data.get("change_type")) or ORDER_CHANGE_ADD
    if change_type not in ORDER_CHANGE_TYPES:
        change_type = ORDER_CHANGE_MODIFY if "改" in str(data.get("raw_text") or "") else ORDER_CHANGE_ADD
    normalized["change_type"] = change_type
    explicit_order_date = extract_explicit_order_date(str(data.get("raw_text") or ""))
    normalized["order_date"] = normalize_order_date_text(data.get("order_date")) or explicit_order_date or fallback_order_date(created_at)
    normalized["deliver_date"] = normalize_deliver_date_text(data.get("deliver_date"))
    normalized["raw_text"] = optional_text(data.get("raw_text")) or ""
    return {field: normalized.get(field) for field in PATCH_ORDER_FIELDS if field in normalized}


def normalize_order_draft(data: dict[str, Any]) -> dict[str, Any]:
    if not data:
        return {}
    return normalize_order_payload(data)


def order_draft_missing_fields(draft: dict[str, Any]) -> list[str]:
    if not draft:
        return ["订单内容"]

    missing: list[str] = []
    kind = draft.get("kind")
    if not draft.get("store"):
        missing.append("门店/区域")
    if kind == ORDER_KIND_PATCH and not draft.get("change_type"):
        missing.append("变更类型")

    items = draft.get("items")
    if not isinstance(items, list) or not items:
        missing.append("商品和数量")
        return missing

    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            missing.append(f"第{index}项商品")
            continue
        if not item.get("name"):
            missing.append(f"第{index}项商品名称")
        if item.get("qty") is None:
            missing.append(f"第{index}项数量")

    return missing


def format_order_draft_summary(draft: dict[str, Any]) -> str:
    if not draft:
        return "暂无订单草稿"

    kind_label = "基础订单" if draft.get("kind") == ORDER_KIND_BASE else "文字补丁"
    source_label = {"excel": "Excel", "photo": "照片", "text": "文字"}.get(str(draft.get("source")), str(draft.get("source") or ""))
    lines = [
        f"类型：{kind_label}",
        f"来源：{source_label}",
        f"门店/区域：{draft.get('store') or '未填写'}",
    ]

    if draft.get("kind") == ORDER_KIND_BASE:
        lines.append(f"订单号：{draft.get('order_no') or '自动生成'}")
        if draft.get("orderer"):
            lines.append(f"下单人：{draft.get('orderer')}")
        if draft.get("order_date"):
            lines.append(f"下单日期：{draft.get('order_date')}")
        if draft.get("deliver_date"):
            lines.append(f"送达日期：{draft.get('deliver_date')}")
    else:
        change_label = "加货" if draft.get("change_type") == ORDER_CHANGE_ADD else "改量"
        lines.append(f"变更类型：{change_label}")
        if draft.get("order_date"):
            lines.append(f"下单日期：{draft.get('order_date')}")
        if draft.get("deliver_date"):
            lines.append(f"送达备注：{draft.get('deliver_date')}")

    items = draft.get("items") if isinstance(draft.get("items"), list) else []
    if not items:
        lines.append("商品：未填写")
    else:
        lines.append("商品：")
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            parts = [
                str(item.get("code") or "").strip(),
                str(item.get("name") or "未填写商品").strip(),
                str(item.get("spec") or "").strip(),
                f"{item.get('qty') if item.get('qty') is not None else '未填写数量'}{item.get('unit') or ''}",
            ]
            if item.get("price") is not None:
                parts.append(f"单价{item.get('price')}")
            if item.get("category"):
                parts.append(str(item.get("category")))
            lines.append(f"{index}. {' / '.join(part for part in parts if part)}")

    return "\n".join(lines)


__all__ = [
    # 订单领域常量
    "ORDER_KIND_BASE",
    "ORDER_KIND_PATCH",
    "ORDER_SOURCE_EXCEL",
    "ORDER_SOURCE_PHOTO",
    "ORDER_SOURCE_TEXT",
    "ORDER_STATUS_NEW",
    "ORDER_STATUS_FETCHED",
    "ORDER_STATUS_CANCELLED",
    "ORDER_STATUS_ALL",
    "ORDER_CHANGE_ADD",
    "ORDER_CHANGE_MODIFY",
    "ORDER_KINDS",
    "ORDER_SOURCES",
    "ORDER_STATUSES",
    "ORDER_CHANGE_TYPES",
    "BASE_ORDER_FIELDS",
    "PATCH_ORDER_FIELDS",
    # 共享纯原语
    "now_iso",
    "clean_export_value",
    # 归一化/格式化函数
    "clean_order_value",
    "optional_text",
    "normalize_number",
    "normalize_date_text",
    "make_iso_date",
    "normalize_order_date_text",
    "extract_explicit_order_date",
    "fallback_order_date",
    "parse_iso_date",
    "repair_photo_order_dates",
    "normalize_deliver_date_text",
    "generate_contract_order_no",
    "normalize_base_item",
    "normalize_patch_item",
    "normalize_order_items",
    "normalize_order_payload",
    "normalize_order_draft",
    "order_draft_missing_fields",
    "format_order_draft_summary",
]

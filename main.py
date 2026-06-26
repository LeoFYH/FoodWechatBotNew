import asyncio
import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import socket
import sqlite3
import struct
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import unquote

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from Crypto.Cipher import AES
import httpx
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils.datetime import from_excel
from openpyxl.utils import get_column_letter
from openai import OpenAI
from pydantic import BaseModel, Field
from wx_crypt import WXBizMsgCrypt, WxChannel_Wecom

from services.business_intent import (
    INTENT_CANCEL,
    INTENT_CHAT,
    INTENT_CONFIRM,
    INTENT_EXIT,
    INTENT_MODIFY,
    INTENT_REJECT,
    INTENT_UNCLEAR,
    BusinessIntent,
    classify_business_intent,
)
import agent_router
import models
import store_sqlite  # 本地 SQLite 回退后端（双轨的 SQLite 这一轨；lock/db_file 由 main 注入）
from order_normalize import *  # 订单归一化纯函数层（门面 re-export，调用点/测试可见性不变）
from excel_import import *  # Excel 订单解析层（门面 re-export）
from llm_json import *  # 大模型输出 JSON 提取通用 helper（门面 re-export）
from vision_import import *  # 视觉/照片解析层（client/model 由 main 注入；门面 re-export）
from order_export import *  # 订单 xlsx 导出层（records/export_dir 由 main 注入；门面 re-export）
from store_sqlite import summarize_order_for_reply  # 撤回回复模板（models 分支格式化用；SQLite 分支在 store_sqlite 内自用）
from receipt_logic import *  # 入库(receipt)领域：RECEIPT 常量 + 归一化/missing/summarize/status helper（门面 re-export）
from commands import *  # 命令分类规则（确定性，无 LLM）：危险动作硬判 + 命令词表（门面 re-export）
from wecom import *  # 企业微信加解密 + 协议解析（无状态；token/aes_key/corp_id 由 main 注入；门面 re-export）

load_dotenv()

DEFAULT_LLM_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL_NAME = "qwen3-vl-plus"
DEFAULT_MAX_HISTORY_MESSAGES = 20
DEFAULT_SYSTEM_PROMPT = "你是运行在微信里的公司客服助手。说话自然、简洁，像真人客服；客户闲聊可以接一两句，但不要把客服窗口变成闲聊室。涉及价格、交期、投诉、纠纷、退款、发票等事项，不要瞎承诺，明确转人工处理。"
CUSTOMER_CHAT_PROMPT = "你是运行在微信里的公司客服。普通聊天要自然、简短，像真人客服。不要进入需求访谈，不要问客户工作流程、审批、排班、数据整理、痛点、频率，也不要说自己只有需求访谈模式。客户问订单/入库怎么用时，只给简短操作提示。涉及价格、交期、投诉、纠纷、退款、发票等事项，不要承诺，回复转人工处理。不要声称自己正在入库、同步、查询数据库、生成单号或保存成功；这些业务动作只能由程序命令层完成。"
DEFAULT_WECOM_BOT_NAME = "食品厂机器人"
DEFAULT_WECOM_KF_SYNC_LIMIT = 100
DEFAULT_HTTP_TIMEOUT_SECONDS = 20
DEFAULT_EXPORT_DIR = "exports"
DEFAULT_SESSION_STATE_FILE = "session_state.json"
DEFAULT_ORDER_DB_FILE = "orders.db"
DEFAULT_RECEIPT_DB_FILE = "receipts.db"
DEFAULT_VISION_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_VISION_MODEL = "qwen3-vl-plus"
# EXCEL_MAX_SCAN_ROWS / EXCEL_MAX_SCAN_COLUMNS 已移至 excel_import.py（门面 re-export）。
APP_BUILD_LABEL = os.getenv("APP_BUILD_LABEL", "excel-diagnostics-2026-06-24")


def get_int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default

    try:
        parsed = int(value)
    except ValueError:
        logging.warning("Invalid %s=%s, using default=%s", name, value, default)
        return default

    if parsed < 1:
        logging.warning("Invalid %s=%s, using default=%s", name, value, default)
        return default

    return parsed


def get_bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_system_prompt() -> str:
    prompt_file = os.getenv("PROMPT_FILE")
    if prompt_file:
        try:
            return Path(prompt_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            logging.warning("Failed to read PROMPT_FILE=%s: %s", prompt_file, exc)

    return os.getenv("SYSTEM_PROMPT") or DEFAULT_SYSTEM_PROMPT


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format=LOG_FORMAT,
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger("wechatclaw")
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))

log_file = os.getenv("LOG_FILE")
if log_file:
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(file_handler)

app = FastAPI(title="WechatClaw Phase 0.5 AI Backend MVP")

LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", DEFAULT_LLM_BASE_URL)
MODEL_NAME = os.getenv("MODEL_NAME") or DEFAULT_MODEL_NAME
MAX_HISTORY_MESSAGES = get_int_env("MAX_HISTORY_MESSAGES", DEFAULT_MAX_HISTORY_MESSAGES)
PROMPT_FILE = os.getenv("PROMPT_FILE")
SYSTEM_PROMPT = load_system_prompt()
PROMPT_TURN_CONTEXT = get_bool_env("PROMPT_TURN_CONTEXT", bool(PROMPT_FILE))
MEMORY_FILE = Path(os.getenv("MEMORY_FILE", "memory.json"))
SESSION_STATE_FILE = Path(os.getenv("SESSION_STATE_FILE", DEFAULT_SESSION_STATE_FILE))
ORDER_DB_FILE = Path(os.getenv("ORDER_DB_FILE", DEFAULT_ORDER_DB_FILE))
RECEIPT_DB_FILE = Path(os.getenv("RECEIPT_DB_FILE", DEFAULT_RECEIPT_DB_FILE))
VISION_API_KEY = os.getenv("VISION_API_KEY") or os.getenv("DASHSCOPE_API_KEY", "")
VISION_BASE_URL = os.getenv("VISION_BASE_URL", DEFAULT_VISION_BASE_URL)
VISION_MODEL = os.getenv("VISION_MODEL", DEFAULT_VISION_MODEL)
ROBOT_API_TOKEN = os.getenv("ROBOT_API_TOKEN", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
MEMORY_LOCK = Lock()
WECOM_CALLBACK_TOKEN = os.getenv("WECOM_CALLBACK_TOKEN") or os.getenv("WX_BOT_TOKEN")
WECOM_ENCODING_AES_KEY = os.getenv("WECOM_ENCODING_AES_KEY") or os.getenv("WX_BOT_AES_KEY")
WECOM_CORP_ID = os.getenv("WECOM_CORP_ID") or os.getenv("WX_BOT_CORP_ID", "")
WECOM_BOT_NAME = os.getenv("WECOM_BOT_NAME", DEFAULT_WECOM_BOT_NAME)
WECOM_KF_CORP_ID = os.getenv("WECOM_KF_CORP_ID") or WECOM_CORP_ID
WECOM_KF_SECRET = os.getenv("WECOM_KF_SECRET")
WECOM_KF_CALLBACK_TOKEN = os.getenv("WECOM_KF_CALLBACK_TOKEN")
WECOM_KF_ENCODING_AES_KEY = os.getenv("WECOM_KF_ENCODING_AES_KEY")
WECOM_KF_CURSOR_FILE = Path(os.getenv("WECOM_KF_CURSOR_FILE", "kf_cursors.json"))
WECOM_KF_SYNC_LIMIT = get_int_env("WECOM_KF_SYNC_LIMIT", DEFAULT_WECOM_KF_SYNC_LIMIT)
HTTP_TIMEOUT_SECONDS = get_int_env("HTTP_TIMEOUT_SECONDS", DEFAULT_HTTP_TIMEOUT_SECONDS)
EXPORT_DIR = Path(os.getenv("EXPORT_DIR", DEFAULT_EXPORT_DIR))
EXPORT_TOKEN = os.getenv("EXPORT_TOKEN")
SEEN_WECOM_MSG_IDS: set[str] = set()
SEEN_WECOM_MSG_IDS_LOCK = Lock()
SEEN_WECOM_KF_MSG_IDS: set[str] = set()
SEEN_WECOM_KF_MSG_IDS_LOCK = Lock()
WECOM_KF_CURSOR_LOCK = Lock()
WECOM_KF_ACCESS_TOKEN_LOCK = Lock()
SESSION_STATE_LOCK = Lock()
ORDER_LOCK = Lock()
ORDER_DB_LOCK = Lock()
RECEIPT_DB_LOCK = Lock()
WECOM_KF_ACCESS_TOKEN = ""
WECOM_KF_ACCESS_TOKEN_EXPIRES_AT = 0.0

if not LLM_API_KEY:
    raise RuntimeError("LLM_API_KEY is missing. Check your .env file.")

client = OpenAI(
    api_key=LLM_API_KEY,
    base_url=LLM_BASE_URL,
)
vision_client = OpenAI(
    api_key=VISION_API_KEY,
    base_url=VISION_BASE_URL,
) if VISION_API_KEY else None


class ChatRequest(BaseModel):
    user_id: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)


class ChatResponse(BaseModel):
    user_id: str
    answer: str
    history_length: int


class MemoryLengthResponse(BaseModel):
    user_id: str
    history_length: int


class DeleteMemoryResponse(BaseModel):
    deleted: bool
    user_id: str


# WecomMessage / WecomKfEvent 已移至 wecom.py（门面 re-export）。


class WecomKfApiError(RuntimeError):
    def __init__(self, path: str, data: dict[str, Any]) -> None:
        self.path = path
        self.data = data
        self.errcode = data.get("errcode")
        super().__init__(f"WeCom API {path} failed: {data}")


class MarkFetchedRequest(BaseModel):
    ids: list[int] = Field(default_factory=list)


class IdsRequest(BaseModel):
    ids: list[Any] = Field(default_factory=list)


class TextOrderImportRequest(BaseModel):
    user_id: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    confirm: bool = False
    raw_ref: str | None = None


def load_memory() -> dict:
    if models.is_enabled():
        return models.load_memory()

    if not MEMORY_FILE.exists():
        return {}

    raw_memory = MEMORY_FILE.read_text(encoding="utf-8").strip()
    if not raw_memory:
        return {}

    try:
        data = json.loads(raw_memory)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="memory.json is not valid JSON") from exc

    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail="memory.json must contain a JSON object")

    return data


def save_memory(memory: dict) -> None:
    if models.is_enabled():
        models.save_memory(memory)
        return

    MEMORY_FILE.write_text(
        json.dumps(memory, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_session_state() -> dict[str, dict[str, Any]]:
    if models.is_enabled():
        return models.load_session_state()

    if not SESSION_STATE_FILE.exists():
        return {}

    raw_state = SESSION_STATE_FILE.read_text(encoding="utf-8").strip()
    if not raw_state:
        return {}

    try:
        data = json.loads(raw_state)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="session_state.json is not valid JSON") from exc

    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail="session_state.json must contain a JSON object")

    state: dict[str, dict[str, Any]] = {}
    for user_id, record in data.items():
        if isinstance(record, dict):
            state[str(user_id)] = record
    return state


def save_session_state(state: dict[str, dict[str, Any]]) -> None:
    if models.is_enabled():
        models.save_session_state(state)
        return

    SESSION_STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def insert_order_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_order_payload(payload)
    if normalized.get("confirmed"):
        missing = order_draft_missing_fields(normalized)
        if missing:
            raise ValueError("confirmed order missing fields: " + ",".join(missing))

    if models.is_enabled():
        return models.insert_order_payload(normalized)

    return store_sqlite.insert_order_payload(normalized, db_file=ORDER_DB_FILE, lock=ORDER_DB_LOCK)


def query_order_payloads(
    status: str | None = None,
    ids: list[int] | None = None,
    order_date: str | None = None,
) -> list[dict[str, Any]]:
    if models.is_enabled():
        return models.query_order_payloads(status=status, ids=ids, order_date=order_date)

    return store_sqlite.query_order_payloads(
        db_file=ORDER_DB_FILE, status=status, ids=ids, order_date=order_date
    )


def mark_order_payloads_fetched(ids: list[int]) -> dict[str, list[int]]:
    if models.is_enabled():
        return models.mark_order_payloads_fetched(ids)

    return store_sqlite.mark_order_payloads_fetched(ids, db_file=ORDER_DB_FILE, lock=ORDER_DB_LOCK)


def unmark_order_payloads(ids: list[int]) -> dict[str, list[int]]:
    if models.is_enabled():
        return models.unmark_order_payloads(ids)

    return store_sqlite.unmark_order_payloads(ids, db_file=ORDER_DB_FILE, lock=ORDER_DB_LOCK)


def cancel_latest_order_for_user(user_id: str) -> str:
    if models.is_enabled():
        result = models.cancel_latest_order_for_user(user_id)
        if result.get("outcome") == "not_found":
            return "没找到你最近确认的订单，暂时没有可撤回的。"
        if result.get("outcome") == "fetched":
            return "这单已被排产/发货使用，不能直接撤回，需要联系数据部处理。"
        payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
        return f"好，刚那单（{summarize_order_for_reply(payload)}）撤回了，重新发我吧。"

    return store_sqlite.cancel_latest_order_for_user(user_id, db_file=ORDER_DB_FILE, lock=ORDER_DB_LOCK)


def cancel_latest_receipt_for_user(user_id: str) -> str:
    today = datetime.now().date().isoformat()
    if models.is_enabled():
        result = models.cancel_latest_receipt_for_user(user_id, today)
        if result.get("outcome") == "cancelled":
            payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
            return f"好，刚那条入库记录（{summarize_receipt_for_reply(payload)}）撤回了。"
        if result.get("outcome") == "fetched":
            return "这条入库记录已被入库工具使用，不能直接撤回，需要联系数据部处理。"
        return "没找到你今天确认的入库记录，暂时没有可撤回的。"

    return store_sqlite.cancel_latest_receipt_for_user(user_id, today, db_file=RECEIPT_DB_FILE, lock=RECEIPT_DB_LOCK)


def order_draft_has_content(draft: dict[str, Any]) -> bool:
    if not isinstance(draft, dict):
        return False
    if draft.get("store"):
        return True
    items = draft.get("items") if isinstance(draft.get("items"), list) else []
    return any(
        isinstance(item, dict) and (item.get("name") or item.get("qty") is not None)
        for item in items
    )


def receipt_draft_has_content(draft: dict[str, Any]) -> bool:
    if not isinstance(draft, dict):
        return False
    items = draft.get("items") if isinstance(draft.get("items"), list) else []
    return any(
        isinstance(item, dict) and (item.get("name") or item.get("qty") is not None)
        for item in items
    )


def missing_fields_reply(missing: list[str], *, receipt: bool = False) -> str:
    if not missing:
        return ""
    subject = "这条入库记录" if receipt else "这单"
    return f"{subject}还差：{'、'.join(missing)}。补我一下就行，发“取消”可以清空草稿。"


def insert_receipt_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_receipt_payload(payload)
    missing = receipt_missing_fields(normalized)
    if missing:
        raise ValueError("receipt missing fields: " + ",".join(missing))

    if models.is_enabled():
        return models.insert_receipt_payload(normalized)

    return store_sqlite.insert_receipt_payload(normalized, db_file=RECEIPT_DB_FILE, lock=RECEIPT_DB_LOCK)


def query_receipt_payloads(date: str) -> list[dict[str, Any]]:
    return query_receipt_payloads_by_status(date, RECEIPT_STATUS_NEW)


def update_receipt_payload_status(
    ids: list[Any],
    target_status: str,
) -> dict[str, list[str]]:
    if models.is_enabled():
        if target_status == RECEIPT_STATUS_FETCHED:
            return models.mark_receipt_payloads_fetched(ids)
        return models.unmark_receipt_payloads(ids)

    return store_sqlite.update_receipt_payload_status(ids, target_status, db_file=RECEIPT_DB_FILE, lock=RECEIPT_DB_LOCK)


def mark_receipt_payloads_fetched(ids: list[Any]) -> dict[str, list[str]]:
    return update_receipt_payload_status(ids, RECEIPT_STATUS_FETCHED)


def unmark_receipt_payloads(ids: list[Any]) -> dict[str, list[str]]:
    return update_receipt_payload_status(ids, RECEIPT_STATUS_CONFIRMED)


def query_receipt_payloads_by_status(date: str, status: str | None = None) -> list[dict[str, Any]]:
    if models.is_enabled():
        return models.query_receipt_payloads(date, status=status)

    return store_sqlite.query_receipt_payloads_by_status(date, status, db_file=RECEIPT_DB_FILE)


SESSION_MODE_CHAT = "chat"
SESSION_MODE_ORDER = "order"
SESSION_MODE_RECEIPT = "receipt"
SESSION_MODES = {SESSION_MODE_CHAT, SESSION_MODE_ORDER, SESSION_MODE_RECEIPT}

# 命令分类规则与词表（含危险动作确认/取消/退出/撤回的硬判）已移至 commands.py（门面 re-export）。

GLOBAL_ROUTE_CHAT = "chat"
GLOBAL_ROUTE_ORDER_TEXT = "order_text"
GLOBAL_ROUTE_ENTER_ORDER = "enter_order"
GLOBAL_ROUTE_ENTER_RECEIPT = "enter_receipt"
GLOBAL_ROUTE_ORDER_QUERY = "order_query"
GLOBAL_ROUTE_UNCLEAR = "unclear"

# 订单领域常量（ORDER_KIND_*/ORDER_SOURCE_*/ORDER_STATUS_*/ORDER_CHANGE_*/
# ORDER_KINDS/ORDER_SOURCES/ORDER_STATUSES/ORDER_CHANGE_TYPES/BASE_ORDER_FIELDS/
# PATCH_ORDER_FIELDS）已移至 order_normalize.py，经 `from order_normalize import *` re-export。
# RECEIPT_STATUS_* / RECEIPT_API_STATUSES / RECEIPT_STORAGE_STATUSES 已移至 receipt_logic.py（门面 re-export）。

BASE_ITEM_FIELDS = [
    "code",
    "name",
    "spec",
    "unit",
    "qty",
    "price",
    "category",
]
PATCH_ITEM_FIELDS = [
    "code",
    "name",
    "spec",
    "unit",
    "qty",
]

# ORDER_SUMMARY_HEADERS / ORDER_CONTRACT_EXPORT_HEADERS 已移至 order_export.py（门面 re-export）。
# EXCEL_HEADER_ALIASES / EXCEL_METADATA_LABELS 已移至 excel_import.py（门面 re-export）。


def mode_display_name(mode: str) -> str:
    if mode == SESSION_MODE_ORDER:
        return "订单模式"
    if mode == SESSION_MODE_RECEIPT:
        return "入库模式"
    return "普通聊天模式"


def draft_mode_display_name(mode: str) -> str:
    if mode == SESSION_MODE_ORDER:
        return "订单草稿"
    if mode == SESSION_MODE_RECEIPT:
        return "入库草稿"
    return "业务草稿"


def has_raw_order_draft(record: dict[str, Any]) -> bool:
    draft = record.get("order_draft")
    return isinstance(draft, dict) and order_draft_has_content(normalize_order_draft(draft))


def has_raw_receipt_draft(record: dict[str, Any]) -> bool:
    draft = record.get("receipt_draft")
    return isinstance(draft, dict) and receipt_draft_has_content(normalize_receipt_payload(draft))


def conflicting_draft_mode_for_record(record: dict[str, Any], target_mode: str) -> str | None:
    if target_mode == SESSION_MODE_ORDER and has_raw_receipt_draft(record):
        return SESSION_MODE_RECEIPT
    if target_mode == SESSION_MODE_RECEIPT and has_raw_order_draft(record):
        return SESSION_MODE_ORDER
    return None


def business_mode_switch_blocked_reply(conflict_mode: str, target_mode: str) -> str:
    return (
        f"现在还有一份{draft_mode_display_name(conflict_mode)}待确认，"
        f"我先不切到{mode_display_name(target_mode)}，避免把数据记串。"
        "请先回复“确认”保存，或发“取消/退出”清掉后再切换。"
    )


def switch_session_mode(user_id: str, mode: str, *, clear_drafts: bool = False) -> None:
    if mode not in SESSION_MODES:
        raise ValueError(f"Invalid session mode: {mode}")

    record = get_session_record(user_id)
    record["mode"] = mode
    if clear_drafts or mode == SESSION_MODE_CHAT:
        record.pop("order_draft", None)
        record.pop("receipt_draft", None)
    elif mode == SESSION_MODE_ORDER:
        record.pop("receipt_draft", None)
    elif mode == SESSION_MODE_RECEIPT:
        record.pop("order_draft", None)
    save_session_record(user_id, record)


def try_switch_business_mode(user_id: str, target_mode: str) -> str | None:
    record = get_session_record(user_id)
    conflict_mode = conflicting_draft_mode_for_record(record, target_mode)
    if conflict_mode:
        return business_mode_switch_blocked_reply(conflict_mode, target_mode)
    switch_session_mode(user_id, target_mode)
    return None


def clear_current_business_draft(user_id: str, mode: str | None = None) -> None:
    mode = mode or get_session_mode(user_id)
    record = get_session_record(user_id)
    if mode == SESSION_MODE_ORDER:
        record.pop("order_draft", None)
    elif mode == SESSION_MODE_RECEIPT:
        record.pop("receipt_draft", None)
    else:
        record.pop("order_draft", None)
        record.pop("receipt_draft", None)
        record["mode"] = SESSION_MODE_CHAT
    save_session_record(user_id, record)


def exit_business_mode(user_id: str) -> str:
    previous_mode = get_session_mode(user_id)
    record = get_session_record(user_id)
    record["mode"] = SESSION_MODE_CHAT
    record.pop("order_draft", None)
    record.pop("receipt_draft", None)
    save_session_record(user_id, record)
    if previous_mode == SESSION_MODE_ORDER:
        return "已退出订单模式，有事随时叫我。"
    if previous_mode == SESSION_MODE_RECEIPT:
        return "已退出入库模式，有事随时叫我。"
    return "现在是普通聊天模式。"


def build_status_message(user_id: str) -> str:
    mode = get_session_mode(user_id)
    record = get_session_record(user_id)
    has_order = has_raw_order_draft(record)
    has_receipt = has_raw_receipt_draft(record)
    if has_order and has_receipt:
        draft_text = "同时发现订单和入库草稿，请先发“取消/退出”清掉后重来"
    elif has_order:
        draft_text = "有一份订单草稿待确认"
    elif has_receipt:
        draft_text = "有一份入库草稿待确认"
    else:
        draft_text = "没有待确认草稿"
    return f"你现在在{mode_display_name(mode)}，{draft_text}。"


def build_mode_help_message(user_id: str) -> str:
    current = mode_display_name(get_session_mode(user_id))
    return (
        f"我现在在{current}。\n"
        "可用模式：\n"
        "1. 普通聊天：正常问事、闲聊两句都可以。\n"
        "2. 订单模式：发“订单”进入，支持订单文字、Excel、照片；确认后写订单库。\n"
        "3. 入库模式：发“入库”进入，发产成品照片；确认后写入库库。\n"
        "常用命令：退出、取消、状态、撤回。"
    )


def build_order_storage_query_reply(user_id: str) -> str:
    draft = get_order_draft(user_id)
    if order_draft_has_content(draft):
        return "现在还有一张订单草稿没入库。确认没问题请回“确认 / 对 / ok / yes”；要改就直接发修改内容。"
    return (
        "我这里不编入库结果。订单只有在你回复“确认入库”后才会写进 orders.db。\n"
        "Web 工具同步时按订单的 order_date 拉取；如果工具是空，先确认刚才那单是否已经保存，以及工具选的下单日期是否和订单里的 order_date 一致。"
    )


ORDER_LIKE_KEYWORDS = {
    "加",
    "订",
    "下单",
    "补",
    "追",
    "改",
    "换",
    "门店",
    "箱",
    "件",
    "袋",
    "盒",
    "包",
}


def looks_like_order_message(message: str) -> bool:
    text = message.strip()
    command = normalize_command(text)
    if not text:
        return False
    if strip_order_inline_prefix(text) is not None:
        return True
    if re.search(r"\d+(?:\.\d+)?\s*(箱|件|袋|盒|包|斤|公斤|kg|KG|份|个|瓶|桶|条|只)", text):
        return True
    if re.search(r"(加|订|下单|补|追|改|换).*\d+", text):
        return True
    if command_contains_any(command, ORDER_LIKE_KEYWORDS) and re.search(r"\d", text):
        return True
    return False


def count_keyword_occurrences(text: str, keywords: set[str]) -> int:
    return sum(text.count(keyword) for keyword in keywords if keyword)


def looks_like_receipt_business_message(message: str) -> bool:
    command = normalize_command(message)
    if is_business_query_or_negated(command) or is_question_like_command(command):
        return False
    if "入库" in command and command_contains_any(command, {"产成品", "成品", "车间", "照片", "图片", "清单", "记录", "记一下"}):
        return True
    return command_contains_any(command, {"产成品", "成品"}) and command_contains_any(command, {"照片", "图片", "车间", "入库"})


def build_download_url(path: str) -> str:
    if PUBLIC_BASE_URL:
        return f"{PUBLIC_BASE_URL}{path}"
    return path


def build_order_export_message() -> str:
    url = build_download_url("/exports/orders.xlsx")
    if EXPORT_TOKEN:
        url = f"{url}?token=导出口令"
        return f"订单表可以导出。管理员打开这个地址并填写导出口令：{url}"
    return f"订单表可以导出：{url}"


def get_session_record(user_id: str) -> dict[str, Any]:
    with SESSION_STATE_LOCK:
        state = load_session_state()
        record = state.get(user_id)
        if isinstance(record, dict):
            return dict(record)
    return {}


def save_session_record(user_id: str, record: dict[str, Any]) -> None:
    record["updated_at"] = now_iso()
    with SESSION_STATE_LOCK:
        state = load_session_state()
        state[user_id] = record
        save_session_state(state)


def get_session_mode(user_id: str) -> str:
    mode = str(get_session_record(user_id).get("mode") or SESSION_MODE_CHAT)
    if mode not in SESSION_MODES:
        return SESSION_MODE_CHAT
    return mode


def set_session_mode(user_id: str, mode: str) -> None:
    switch_session_mode(user_id, mode)


def get_order_draft(user_id: str) -> dict[str, Any]:
    draft = get_session_record(user_id).get("order_draft")
    if isinstance(draft, dict):
        return normalize_order_draft(draft)
    return normalize_order_draft({})


def save_order_draft(user_id: str, draft: dict[str, Any]) -> None:
    record = get_session_record(user_id)
    record["mode"] = SESSION_MODE_ORDER
    record["order_draft"] = normalize_order_draft(draft)
    record.pop("receipt_draft", None)
    save_session_record(user_id, record)


def clear_order_draft(user_id: str, *, next_mode: str = SESSION_MODE_ORDER) -> None:
    if next_mode not in SESSION_MODES:
        raise ValueError(f"Invalid session mode: {next_mode}")
    record = get_session_record(user_id)
    record["mode"] = next_mode
    record.pop("order_draft", None)
    if next_mode == SESSION_MODE_CHAT:
        record.pop("receipt_draft", None)
    save_session_record(user_id, record)


def get_receipt_draft(user_id: str) -> dict[str, Any]:
    draft = get_session_record(user_id).get("receipt_draft")
    if isinstance(draft, dict):
        return normalize_receipt_payload(draft)
    return {}


def save_receipt_draft(user_id: str, draft: dict[str, Any]) -> None:
    record = get_session_record(user_id)
    record["mode"] = SESSION_MODE_RECEIPT
    record["receipt_draft"] = normalize_receipt_payload(draft)
    record.pop("order_draft", None)
    save_session_record(user_id, record)


def clear_receipt_draft(user_id: str, *, next_mode: str = SESSION_MODE_RECEIPT) -> None:
    if next_mode not in SESSION_MODES:
        raise ValueError(f"Invalid session mode: {next_mode}")
    record = get_session_record(user_id)
    record["mode"] = next_mode
    record.pop("receipt_draft", None)
    if next_mode == SESSION_MODE_CHAT:
        record.pop("order_draft", None)
    save_session_record(user_id, record)


ORDER_SKILL_FILE = Path(
    os.getenv("ORDER_SKILL_FILE", str(Path(__file__).resolve().parent / "skills" / "order" / "SKILL.md"))
)


def save_confirmed_order(user_id: str, draft: dict[str, Any]) -> tuple[int, int]:
    started_at = time.perf_counter()
    payload = normalize_order_payload(draft)
    missing = order_draft_missing_fields(payload)
    if missing:
        raise ValueError("order draft missing fields: " + ",".join(missing))

    payload["confirmed"] = True
    payload["status"] = ORDER_STATUS_NEW
    payload["raw_ref"] = payload.get("raw_ref") or user_id
    saved = insert_order_payload(payload)
    line_count = len(saved.get("items") or [])
    logger.info(
        "order_confirm_saved user_id=%s order_id=%s source=%s lines=%s elapsed_ms=%s",
        user_id,
        saved.get("id"),
        payload.get("source"),
        line_count,
        int((time.perf_counter() - started_at) * 1000),
    )
    return int(saved["id"]), line_count


def save_confirmed_receipt(draft: dict[str, Any]) -> tuple[str, int]:
    payload = normalize_receipt_payload(draft)
    missing = receipt_missing_fields(payload)
    if missing:
        raise ValueError("receipt draft missing fields: " + ",".join(missing))
    saved = insert_receipt_payload(payload)
    return str(saved["id"]), len(saved.get("items") or [])


def user_order_count(user_id: str) -> int:
    raw_ref_prefixes = (user_id, f"{user_id}:")
    return sum(
        1
        for record in query_order_payloads()
        if str(record.get("raw_ref") or "").startswith(raw_ref_prefixes)
    )


@app.on_event("startup")
async def log_startup() -> None:
    logger.warning(
        "foodwechatbot_startup build=%s file=%s cwd=%s pid=%s database_backend=%s",
        APP_BUILD_LABEL,
        Path(__file__).resolve(),
        Path.cwd(),
        os.getpid(),
        os.getenv("DATABASE_BACKEND", "sqlite"),
    )


def collect_order_records() -> list[dict[str, str]]:
    records = query_order_payloads()

    normalized_records: list[dict[str, str]] = []
    for record in records:
        items = record.get("items") if isinstance(record.get("items"), list) else []
        if not items:
            items = [{}]
        for line_no, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                item = {}
            normalized_records.append(
                {
                    "id": str(record.get("id") or ""),
                    "kind": str(record.get("kind") or ""),
                    "source": str(record.get("source") or ""),
                    "status": str(record.get("status") or ""),
                    "confirmed": "是" if record.get("confirmed") else "否",
                    "store": str(record.get("store") or ""),
                    "order_no": str(record.get("order_no") or ""),
                    "orderer": str(record.get("orderer") or ""),
                    "order_date": str(record.get("order_date") or ""),
                    "deliver_date": str(record.get("deliver_date") or ""),
                    "change_type": str(record.get("change_type") or ""),
                    "line_no": str(line_no),
                    "code": str(item.get("code") or ""),
                    "name": str(item.get("name") or ""),
                    "spec": str(item.get("spec") or ""),
                    "unit": str(item.get("unit") or ""),
                    "qty": "" if item.get("qty") is None else str(item.get("qty")),
                    "price": "" if item.get("price") is None else str(item.get("price")),
                    "category": str(item.get("category") or ""),
                    "raw_text": str(record.get("raw_text") or ""),
                    "raw_ref": str(record.get("raw_ref") or ""),
                    "created_at": str(record.get("created_at") or ""),
                }
            )
    return normalized_records


RECEIPT_SKILL_FILE = Path(
    os.getenv("RECEIPT_SKILL_FILE", str(Path(__file__).resolve().parent / "skills" / "receipt" / "SKILL.md"))
)


def get_wecom_crypto() -> WXBizMsgCrypt:
    if not WECOM_CALLBACK_TOKEN or not WECOM_ENCODING_AES_KEY:
        raise HTTPException(
            status_code=500,
            detail="WECOM_CALLBACK_TOKEN and WECOM_ENCODING_AES_KEY must be configured",
        )

    return WXBizMsgCrypt(
        WECOM_CALLBACK_TOKEN,
        WECOM_ENCODING_AES_KEY,
        WECOM_CORP_ID,
        channel=WxChannel_Wecom,
    )


def require_query_param(request: Request, name: str) -> str:
    value = request.query_params.get(name)
    if not value:
        raise HTTPException(status_code=400, detail=f"missing query parameter: {name}")
    return value


def strip_bot_mention(content: str) -> str:
    content = content.strip()
    if WECOM_BOT_NAME:
        content = content.replace(f"@{WECOM_BOT_NAME}", "").strip()
    return content


def is_duplicate_wecom_message(msg_id: str) -> bool:
    if not msg_id:
        return False

    with SEEN_WECOM_MSG_IDS_LOCK:
        if msg_id in SEEN_WECOM_MSG_IDS:
            return True
        SEEN_WECOM_MSG_IDS.add(msg_id)
        if len(SEEN_WECOM_MSG_IDS) > 1000:
            for old_msg_id in list(SEEN_WECOM_MSG_IDS)[:200]:
                SEEN_WECOM_MSG_IDS.discard(old_msg_id)
        return False


def answer_wecom_message(message: WecomMessage) -> str:
    if message.msg_type == "event":
        return "我已加入，可以在群里 @我 提问。"

    if message.msg_type not in {"text", "mixed"}:
        return "目前我先支持文字消息，图片、文件后面再接。"

    content = strip_bot_mention(message.content)
    if not content:
        return "我在，直接说你的问题。"

    if message.chat_type == "group":
        memory_user_id = f"group:{message.chat_id}"
        llm_message = f"{message.sender_name}: {content}"
    else:
        memory_user_id = f"user:{message.sender_user_id or message.chat_id}"
        llm_message = content

    raw_ref = f"wecom:{message.chat_id or message.sender_user_id}:{message.msg_id}"
    return handle_user_message(memory_user_id, llm_message, raw_ref=raw_ref).answer


def get_wecom_kf_crypto() -> WXBizMsgCrypt:
    if not WECOM_KF_CORP_ID:
        raise HTTPException(status_code=500, detail="WECOM_KF_CORP_ID must be configured")
    if not WECOM_KF_CALLBACK_TOKEN or not WECOM_KF_ENCODING_AES_KEY:
        raise HTTPException(
            status_code=500,
            detail="WECOM_KF_CALLBACK_TOKEN and WECOM_KF_ENCODING_AES_KEY must be configured",
        )

    return WXBizMsgCrypt(
        WECOM_KF_CALLBACK_TOKEN,
        WECOM_KF_ENCODING_AES_KEY,
        WECOM_KF_CORP_ID,
        channel=WxChannel_Wecom,
    )


def get_wecom_kf_access_token() -> str:
    global WECOM_KF_ACCESS_TOKEN, WECOM_KF_ACCESS_TOKEN_EXPIRES_AT

    now = time.time()
    with WECOM_KF_ACCESS_TOKEN_LOCK:
        if WECOM_KF_ACCESS_TOKEN and WECOM_KF_ACCESS_TOKEN_EXPIRES_AT > now + 60:
            return WECOM_KF_ACCESS_TOKEN

        if not WECOM_KF_CORP_ID or not WECOM_KF_SECRET:
            raise RuntimeError("WECOM_KF_CORP_ID and WECOM_KF_SECRET must be configured")

        response = httpx.get(
            "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
            params={
                "corpid": WECOM_KF_CORP_ID,
                "corpsecret": WECOM_KF_SECRET,
            },
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()

        if data.get("errcode") != 0:
            raise RuntimeError(f"WeCom gettoken failed: {data}")

        access_token = data.get("access_token")
        if not access_token:
            raise RuntimeError(f"WeCom gettoken missing access_token: {data}")

        WECOM_KF_ACCESS_TOKEN = access_token
        WECOM_KF_ACCESS_TOKEN_EXPIRES_AT = now + int(data.get("expires_in", 7200)) - 300
        return WECOM_KF_ACCESS_TOKEN


def load_kf_cursors() -> dict[str, str]:
    if models.is_enabled():
        return models.load_kf_cursors()

    if not WECOM_KF_CURSOR_FILE.exists():
        return {}

    raw_cursors = WECOM_KF_CURSOR_FILE.read_text(encoding="utf-8").strip()
    if not raw_cursors:
        return {}

    data = json.loads(raw_cursors)
    if not isinstance(data, dict):
        logger.warning("kf_cursor_file_invalid path=%s", WECOM_KF_CURSOR_FILE)
        return {}

    return {str(key): str(value) for key, value in data.items() if value is not None}


def save_kf_cursors(cursors: dict[str, str]) -> None:
    if models.is_enabled():
        models.save_kf_cursors(cursors)
        return

    WECOM_KF_CURSOR_FILE.write_text(
        json.dumps(cursors, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_kf_cursor(open_kfid: str) -> str:
    if not open_kfid:
        return ""

    with WECOM_KF_CURSOR_LOCK:
        return load_kf_cursors().get(open_kfid, "")


def set_kf_cursor(open_kfid: str, cursor: str) -> None:
    if not open_kfid or not cursor:
        return

    with WECOM_KF_CURSOR_LOCK:
        cursors = load_kf_cursors()
        cursors[open_kfid] = cursor
        save_kf_cursors(cursors)


def is_duplicate_wecom_kf_message(msg_id: str) -> bool:
    if not msg_id:
        return False

    with SEEN_WECOM_KF_MSG_IDS_LOCK:
        if msg_id in SEEN_WECOM_KF_MSG_IDS:
            return True
        SEEN_WECOM_KF_MSG_IDS.add(msg_id)
        if len(SEEN_WECOM_KF_MSG_IDS) > 5000:
            for old_msg_id in list(SEEN_WECOM_KF_MSG_IDS)[:1000]:
                SEEN_WECOM_KF_MSG_IDS.discard(old_msg_id)
        return False


def post_wecom_kf_api(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    access_token = get_wecom_kf_access_token()
    response = httpx.post(
        f"https://qyapi.weixin.qq.com/cgi-bin/{path}",
        params={"access_token": access_token},
        json=payload,
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("errcode") != 0:
        raise WecomKfApiError(path, data)
    return data


def get_wecom_kf_service_state(open_kfid: str, external_userid: str) -> int | None:
    data = post_wecom_kf_api(
        "kf/service_state/get",
        {
            "open_kfid": open_kfid,
            "external_userid": external_userid,
        },
    )
    service_state = data.get("service_state")
    try:
        state = int(service_state)
    except (TypeError, ValueError):
        logger.warning(
            "wecom_kf_service_state_invalid open_kfid=%s external_userid=%s data=%s",
            open_kfid,
            external_userid,
            data,
        )
        return None

    logger.info(
        "wecom_kf_service_state open_kfid=%s external_userid=%s state=%s servicer=%s",
        open_kfid,
        external_userid,
        state,
        data.get("servicer_userid"),
    )
    return state


def transfer_wecom_kf_to_ai(open_kfid: str, external_userid: str) -> None:
    data = post_wecom_kf_api(
        "kf/service_state/trans",
        {
            "open_kfid": open_kfid,
            "external_userid": external_userid,
            "service_state": 1,
        },
    )
    logger.info(
        "wecom_kf_service_state_trans_to_ai open_kfid=%s external_userid=%s msg_code=%s",
        open_kfid,
        external_userid,
        data.get("msg_code"),
    )


def ensure_wecom_kf_ai_session(open_kfid: str, external_userid: str) -> None:
    state = get_wecom_kf_service_state(open_kfid, external_userid)
    if state in (0, 1, None):
        return

    try:
        transfer_wecom_kf_to_ai(open_kfid, external_userid)
    except WecomKfApiError as exc:
        logger.warning(
            "wecom_kf_service_state_trans_to_ai_failed open_kfid=%s external_userid=%s state=%s errcode=%s data=%s",
            open_kfid,
            external_userid,
            state,
            exc.errcode,
            exc.data,
        )
        raise


def send_wecom_kf_text(open_kfid: str, external_userid: str, content: str) -> None:
    if not open_kfid or not external_userid:
        raise RuntimeError("open_kfid and external_userid are required to send WeCom KF message")

    payload = {
        "touser": external_userid,
        "open_kfid": open_kfid,
        "msgtype": "text",
        "text": {"content": content},
    }
    ensure_wecom_kf_ai_session(open_kfid, external_userid)

    try:
        post_wecom_kf_api("kf/send_msg", payload)
    except WecomKfApiError as exc:
        if exc.errcode != 95018:
            raise

        logger.warning(
            "wecom_kf_send_state_invalid_retry open_kfid=%s external_userid=%s data=%s",
            open_kfid,
            external_userid,
            exc.data,
        )
        transfer_wecom_kf_to_ai(open_kfid, external_userid)
        post_wecom_kf_api("kf/send_msg", payload)

    logger.info("wecom_kf_send_success open_kfid=%s external_userid=%s", open_kfid, external_userid)


def get_wecom_kf_media(media_id: str) -> tuple[bytes, str, str]:
    if not media_id:
        raise RuntimeError("media_id is required")

    access_token = get_wecom_kf_access_token()
    response = httpx.get(
        "https://qyapi.weixin.qq.com/cgi-bin/media/get",
        params={"access_token": access_token, "media_id": media_id},
        timeout=HTTP_TIMEOUT_SECONDS,
        follow_redirects=True,
    )
    response.raise_for_status()

    content_type = response.headers.get("content-type", "").split(";")[0].strip()
    if content_type == "application/json":
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError("WeCom media/get returned invalid JSON") from exc
        raise RuntimeError(f"WeCom media/get failed: {data}")

    filename = ""
    disposition = response.headers.get("content-disposition", "")
    encoded_match = re.search(r"filename\*=(?:UTF-8''|utf-8'')?([^;]+)", disposition)
    if encoded_match:
        filename = unquote(encoded_match.group(1).strip().strip('"'))
    match = re.search(r'filename="?([^";]+)"?', disposition)
    if match and not filename:
        filename = match.group(1)
    return response.content, content_type, filename


def handle_wecom_kf_sync_item(item: dict[str, Any]) -> None:
    msg_id = str(item.get("msgid") or "")
    if is_duplicate_wecom_kf_message(msg_id):
        logger.info("wecom_kf_duplicate_message msg_id=%s", msg_id)
        return

    msg_type = item.get("msgtype")
    open_kfid = str(item.get("open_kfid") or item.get("event", {}).get("open_kfid") or "")
    external_userid = str(item.get("external_userid") or item.get("event", {}).get("external_userid") or "")

    logger.info(
        "wecom_kf_sync_item msg_id=%s msg_type=%s open_kfid=%s external_userid=%s origin=%s",
        msg_id,
        msg_type,
        open_kfid,
        external_userid,
        item.get("origin"),
    )

    if msg_type == "event":
        logger.info("wecom_kf_event msg_id=%s event=%s", msg_id, item.get("event", {}))
        return

    session_id = f"kf:{open_kfid}:{external_userid}" if open_kfid and external_userid else ""
    raw_ref = f"kf:{open_kfid}:{external_userid}:{msg_id}"

    if msg_type == "image":
        media_id = str(item.get("image", {}).get("media_id") or "")
        if not media_id or not open_kfid or not external_userid:
            logger.info("wecom_kf_image_skipped msg_id=%s", msg_id)
            return
        try:
            media_bytes, content_type, _filename = get_wecom_kf_media(media_id)
            if get_session_mode(session_id) == SESSION_MODE_RECEIPT:
                answer = handle_receipt_photo_input(session_id, media_bytes, content_type, raw_ref).answer
            else:
                answer = handle_photo_order_input(session_id, media_bytes, content_type, raw_ref).answer
        except Exception as exc:
            logger.exception("wecom_kf_image_order_failed msg_id=%s media_id=%s error=%s", msg_id, media_id, exc)
            answer = "这张图片处理失败了，请稍后再试，或直接用文字发送门店、商品和数量。"
        send_wecom_kf_text(open_kfid, external_userid, answer)
        return

    if msg_type == "file":
        media_id = str(item.get("file", {}).get("media_id") or "")
        filename = str(item.get("file", {}).get("filename") or item.get("file", {}).get("file_name") or media_id)
        if not media_id or not open_kfid or not external_userid:
            logger.info("wecom_kf_file_skipped msg_id=%s", msg_id)
            return
        try:
            media_bytes, content_type, downloaded_name = get_wecom_kf_media(media_id)
            filename = downloaded_name or filename
            extension = Path(filename).suffix.lower()
            logger.info(
                "wecom_kf_file_received msg_id=%s filename=%s content_type=%s size=%s signature=%s",
                msg_id,
                filename,
                content_type,
                len(media_bytes),
                excel_file_signature(media_bytes),
            )
            if extension not in {".xlsx", ".xlsm"} and "spreadsheet" not in content_type:
                answer = "这个文件我暂时只支持标准 Excel 订单表。"
            else:
                try:
                    send_wecom_kf_text(open_kfid, external_userid, "已收到Excel，正在解析订单内容，稍等一下。")
                except Exception as send_exc:
                    logger.warning(
                        "wecom_kf_file_processing_notice_failed msg_id=%s error=%s",
                        msg_id,
                        send_exc,
                    )
                answer = handle_excel_order_input(session_id, media_bytes, raw_ref=f"{raw_ref}:{filename}").answer
        except Exception as exc:
            logger.exception("wecom_kf_file_order_failed msg_id=%s media_id=%s error=%s", msg_id, media_id, exc)
            answer = "这个文件处理失败了。请确认是标准 Excel 订单表后重发。"
        send_wecom_kf_text(open_kfid, external_userid, answer)
        return

    if msg_type != "text":
        if open_kfid and external_userid:
            send_wecom_kf_text(open_kfid, external_userid, "目前我支持文字加单、订单照片和标准 Excel。")
        return

    content = str(item.get("text", {}).get("content") or "").strip()
    if not content or not open_kfid or not external_userid:
        logger.info("wecom_kf_text_skipped msg_id=%s", msg_id)
        return

    answer = handle_user_message(session_id, content, raw_ref=raw_ref).answer
    send_wecom_kf_text(open_kfid, external_userid, answer)


def sync_wecom_kf_messages(event: WecomKfEvent) -> None:
    if not event.token or not event.open_kfid:
        logger.warning("wecom_kf_event_missing_token_or_open_kfid event=%s", event.model_dump())
        return

    cursor = get_kf_cursor(event.open_kfid)

    for _ in range(20):
        payload = {
            "cursor": cursor,
            "token": event.token,
            "limit": WECOM_KF_SYNC_LIMIT,
            "open_kfid": event.open_kfid,
        }
        data = post_wecom_kf_api("kf/sync_msg", payload)
        next_cursor = str(data.get("next_cursor") or "")
        if next_cursor:
            set_kf_cursor(event.open_kfid, next_cursor)
            cursor = next_cursor

        for item in data.get("msg_list", []):
            if isinstance(item, dict):
                handle_wecom_kf_sync_item(item)

        if int(data.get("has_more", 0)) != 1:
            return

    logger.warning("wecom_kf_sync_stopped_after_page_limit open_kfid=%s", event.open_kfid)


def process_wecom_kf_event(event: WecomKfEvent) -> None:
    try:
        sync_wecom_kf_messages(event)
    except Exception as exc:
        logger.exception("wecom_kf_process_failed open_kfid=%s error=%s", event.open_kfid, exc)


def request_client_host(request: Request) -> str:
    return request.client.host if request.client else ""


def request_query_keys(request: Request) -> str:
    return ",".join(sorted(request.query_params.keys()))


def parse_ids_param(ids: str | None) -> list[int]:
    if not ids:
        return []
    parsed: list[int] = []
    for part in ids.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            parsed.append(int(part))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid id: {part}") from exc
    return parsed


def validate_iso_date_param(value: str, name: str) -> str:
    if not value:
        raise HTTPException(status_code=400, detail=f"{name} is required")
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{name} must be YYYY-MM-DD") from exc


def require_robot_api_token(request: Request) -> None:
    authorization = request.headers.get("authorization", "")
    expected = f"Bearer {ROBOT_API_TOKEN}" if ROBOT_API_TOKEN else ""
    if not expected or not hmac.compare_digest(authorization, expected):
        raise HTTPException(
            status_code=401,
            detail="invalid robot api token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_export_token(request: Request) -> str:
    token = request.query_params.get("token", "")
    if EXPORT_TOKEN and token != EXPORT_TOKEN:
        raise HTTPException(status_code=403, detail="invalid export token")
    return token


from dispatch import *  # 分发与处理逻辑（门面 re-export；供 main 内/routers/tests 引用）


# ============================== 路由装配 ==============================
# 路由已迁至 routers/（APIRouter，不带 prefix → URL 路径逐字不变）。在此（main 末尾、
# 所有 def/class 定义之后）import 并 include，避免 routers->main 的 import 期循环。
from routers.wecom import router as wecom_router  # noqa: E402
from routers.robot import router as robot_router  # noqa: E402
from routers.web import router as web_router  # noqa: E402

app.include_router(wecom_router)
app.include_router(robot_router)
app.include_router(web_router)

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
import time
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import Any

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from Crypto.Cipher import AES
import httpx
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openai import OpenAI
from pydantic import BaseModel, Field
from wx_crypt import WXBizMsgCrypt, WxChannel_Wecom


load_dotenv()

DEFAULT_LLM_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL_NAME = "qwen3-vl-plus"
DEFAULT_MAX_HISTORY_MESSAGES = 20
DEFAULT_SYSTEM_PROMPT = "你是一个运行在微信里的 AI 助手，回答要简洁、有帮助。"
DEFAULT_WECOM_BOT_NAME = "食品厂机器人"
DEFAULT_WECOM_KF_SYNC_LIMIT = 100
DEFAULT_HTTP_TIMEOUT_SECONDS = 20
DEFAULT_EXPORT_DIR = "exports"
DEFAULT_INTERVIEW_IDLE_ARCHIVE_SECONDS = 300
DEFAULT_INTERVIEW_ARCHIVE_POLL_SECONDS = 60
DEFAULT_SESSION_STATE_FILE = "session_state.json"
DEFAULT_ORDER_DB_FILE = "orders.db"
DEFAULT_RECEIPT_DB_FILE = "receipts.db"
DEFAULT_VISION_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_VISION_MODEL = "qwen3-vl-plus"


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


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("wechatclaw")

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
INTERVIEW_ARCHIVE_FILE = Path(os.getenv("INTERVIEW_ARCHIVE_FILE", "interviews.json"))
INTERVIEW_IDLE_ARCHIVE_SECONDS = get_int_env(
    "INTERVIEW_IDLE_ARCHIVE_SECONDS",
    DEFAULT_INTERVIEW_IDLE_ARCHIVE_SECONDS,
)
INTERVIEW_ARCHIVE_POLL_SECONDS = get_int_env(
    "INTERVIEW_ARCHIVE_POLL_SECONDS",
    DEFAULT_INTERVIEW_ARCHIVE_POLL_SECONDS,
)
SEEN_WECOM_MSG_IDS: set[str] = set()
SEEN_WECOM_MSG_IDS_LOCK = Lock()
SEEN_WECOM_KF_MSG_IDS: set[str] = set()
SEEN_WECOM_KF_MSG_IDS_LOCK = Lock()
WECOM_KF_CURSOR_LOCK = Lock()
WECOM_KF_ACCESS_TOKEN_LOCK = Lock()
INTERVIEW_ARCHIVE_LOCK = Lock()
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


class WecomMessage(BaseModel):
    msg_type: str
    chat_type: str
    chat_id: str
    msg_id: str
    sender_user_id: str
    sender_name: str
    content: str


class WecomKfEvent(BaseModel):
    token: str
    open_kfid: str
    event: str
    create_time: str


class WecomKfApiError(RuntimeError):
    def __init__(self, path: str, data: dict[str, Any]) -> None:
        self.path = path
        self.data = data
        self.errcode = data.get("errcode")
        super().__init__(f"WeCom API {path} failed: {data}")


class MarkFetchedRequest(BaseModel):
    ids: list[int] = Field(default_factory=list)


class TextOrderImportRequest(BaseModel):
    user_id: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    confirm: bool = False
    raw_ref: str | None = None


def load_memory() -> dict:
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
    MEMORY_FILE.write_text(
        json.dumps(memory, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_session_state() -> dict[str, dict[str, Any]]:
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
    SESSION_STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def init_order_db() -> None:
    ORDER_DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(ORDER_DB_FILE) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS order_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                source TEXT NOT NULL,
                store TEXT NOT NULL DEFAULT '',
                order_date TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'new',
                confirmed INTEGER NOT NULL DEFAULT 0,
                raw_ref TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(order_entries)").fetchall()
        }
        if "order_date" not in columns:
            conn.execute("ALTER TABLE order_entries ADD COLUMN order_date TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_order_entries_status ON order_entries(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_order_entries_confirmed ON order_entries(confirmed)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_order_entries_kind ON order_entries(kind)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_order_entries_order_date ON order_entries(order_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_order_entries_status_order_date ON order_entries(status, order_date)")
        rows = conn.execute(
            "SELECT id, payload_json, created_at FROM order_entries WHERE order_date = ''"
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(str(row[1]))
            except json.JSONDecodeError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            order_date = normalize_order_date_text(payload.get("order_date")) if isinstance(payload, dict) else ""
            if order_date:
                conn.execute(
                    "UPDATE order_entries SET order_date = ? WHERE id = ?",
                    (str(order_date), int(row[0])),
                )
        conn.commit()


def order_db_connection() -> sqlite3.Connection:
    init_order_db()
    conn = sqlite3.connect(ORDER_DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def row_to_order_payload(row: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(str(row["payload_json"]))
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    payload["id"] = int(row["id"])
    payload["kind"] = str(row["kind"])
    payload["source"] = str(row["source"])
    payload["store"] = str(row["store"] or payload.get("store") or "")
    payload["confirmed"] = bool(row["confirmed"])
    payload["status"] = str(row["status"])
    payload["raw_ref"] = str(row["raw_ref"] or payload.get("raw_ref") or "")
    payload["created_at"] = str(row["created_at"] or payload.get("created_at") or "")
    payload["order_date"] = str(row["order_date"] or payload.get("order_date") or "")
    normalized = normalize_order_payload(payload)
    normalized["id"] = int(row["id"])
    normalized["kind"] = str(row["kind"])
    normalized["source"] = str(row["source"])
    normalized["store"] = str(row["store"] or normalized.get("store") or "")
    normalized["confirmed"] = bool(row["confirmed"])
    normalized["status"] = str(row["status"])
    normalized["raw_ref"] = str(row["raw_ref"] or normalized.get("raw_ref") or "")
    normalized["created_at"] = str(row["created_at"] or normalized.get("created_at") or "")
    normalized["order_date"] = str(row["order_date"] or normalized.get("order_date") or "")
    return normalized


def insert_order_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_order_payload(payload)
    if normalized.get("confirmed"):
        missing = order_draft_missing_fields(normalized)
        if missing:
            raise ValueError("confirmed order missing fields: " + ",".join(missing))

    created_at = normalized.get("created_at") or now_iso()
    normalized["created_at"] = created_at
    normalized["order_date"] = str(normalized.get("order_date") or "")
    normalized["status"] = normalized.get("status") or "new"
    normalized["confirmed"] = bool(normalized.get("confirmed"))

    with ORDER_DB_LOCK:
        with order_db_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO order_entries (
                    kind, source, store, order_date, status, confirmed, raw_ref,
                    created_at, updated_at, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(normalized.get("kind") or ""),
                    str(normalized.get("source") or ""),
                    str(normalized.get("store") or ""),
                    str(normalized.get("order_date") or ""),
                    str(normalized.get("status") or "new"),
                    1 if normalized.get("confirmed") else 0,
                    str(normalized.get("raw_ref") or ""),
                    created_at,
                    now_iso(),
                    json.dumps(normalized, ensure_ascii=False),
                ),
            )
            order_id = int(cursor.lastrowid)
            normalized["id"] = order_id
            conn.execute(
                "UPDATE order_entries SET payload_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(normalized, ensure_ascii=False), now_iso(), order_id),
            )
            conn.commit()
    return normalized


def query_order_payloads(
    status: str | None = None,
    ids: list[int] | None = None,
    order_date: str | None = None,
) -> list[dict[str, Any]]:
    clauses = ["confirmed = 1"]
    params: list[Any] = []
    if ids:
        placeholders = ",".join("?" for _ in ids)
        clauses.append(f"id IN ({placeholders})")
        params.extend(ids)
    else:
        if order_date is not None:
            clauses.append("order_date = ?")
            params.append(order_date)
    if status and status != ORDER_STATUS_ALL and not ids:
        clauses.append("status = ?")
        params.append(status)

    where_sql = " AND ".join(clauses)
    with order_db_connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM order_entries WHERE {where_sql} ORDER BY id ASC",
            params,
        ).fetchall()
    return [row_to_order_payload(row) for row in rows]


def mark_order_payloads_fetched(ids: list[int]) -> dict[str, list[int]]:
    clean_ids = sorted({int(order_id) for order_id in ids if int(order_id) > 0})
    if not clean_ids:
        return {"succeeded": [], "failed": []}

    placeholders = ",".join("?" for _ in clean_ids)
    with ORDER_DB_LOCK:
        with order_db_connection() as conn:
            existing_rows = conn.execute(
                f"SELECT id FROM order_entries WHERE id IN ({placeholders})",
                clean_ids,
            ).fetchall()
            existing_ids = {int(row["id"]) for row in existing_rows}
            succeeded = [order_id for order_id in clean_ids if order_id in existing_ids]
            failed = [order_id for order_id in clean_ids if order_id not in existing_ids]

            if not succeeded:
                return {"succeeded": [], "failed": failed}

            update_placeholders = ",".join("?" for _ in succeeded)
            conn.execute(
                f"""
                UPDATE order_entries
                SET status = 'fetched',
                    updated_at = ?
                WHERE id IN ({update_placeholders})
                """,
                [now_iso(), *succeeded],
            )
            rows = conn.execute(
                f"SELECT * FROM order_entries WHERE id IN ({update_placeholders})",
                succeeded,
            ).fetchall()
            for row in rows:
                payload = row_to_order_payload(row)
                payload["status"] = "fetched"
                conn.execute(
                    "UPDATE order_entries SET payload_json = ? WHERE id = ?",
                    (json.dumps(payload, ensure_ascii=False), int(row["id"])),
                )
            conn.commit()
            return {"succeeded": succeeded, "failed": failed}


def init_receipt_db() -> None:
    RECEIPT_DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(RECEIPT_DB_FILE) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS receipt_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_receipt_entries_date ON receipt_entries(date)")
        conn.commit()


def receipt_db_connection() -> sqlite3.Connection:
    init_receipt_db()
    conn = sqlite3.connect(RECEIPT_DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


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


def row_to_receipt_payload(row: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(str(row["payload_json"]))
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload["date"] = str(row["date"] or payload.get("date") or "")
    normalized = normalize_receipt_payload(payload)
    normalized["id"] = f"r{int(row['id']):03d}"
    normalized["date"] = str(row["date"] or normalized.get("date") or "")
    return {
        "id": normalized["id"],
        "date": normalized["date"],
        "items": normalized.get("items") or [],
    }


def insert_receipt_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_receipt_payload(payload)
    missing = receipt_missing_fields(normalized)
    if missing:
        raise ValueError("receipt missing fields: " + ",".join(missing))

    with RECEIPT_DB_LOCK:
        with receipt_db_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO receipt_entries (
                    date, created_at, updated_at, payload_json
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    str(normalized.get("date") or ""),
                    str(normalized.get("created_at") or now_iso()),
                    now_iso(),
                    json.dumps(normalized, ensure_ascii=False),
                ),
            )
            receipt_id = int(cursor.lastrowid)
            normalized["id"] = f"r{receipt_id:03d}"
            conn.execute(
                "UPDATE receipt_entries SET payload_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(normalized, ensure_ascii=False), now_iso(), receipt_id),
            )
            conn.commit()
    return {
        "id": str(normalized["id"]),
        "date": str(normalized.get("date") or ""),
        "items": normalized.get("items") or [],
    }


def query_receipt_payloads(date: str) -> list[dict[str, Any]]:
    with receipt_db_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM receipt_entries WHERE date = ? ORDER BY id ASC",
            (date,),
        ).fetchall()
    return [row_to_receipt_payload(row) for row in rows]


def load_interview_archive() -> dict[str, dict[str, Any]]:
    if not INTERVIEW_ARCHIVE_FILE.exists():
        return {}

    raw_archive = INTERVIEW_ARCHIVE_FILE.read_text(encoding="utf-8").strip()
    if not raw_archive:
        return {}

    try:
        data = json.loads(raw_archive)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="interviews.json is not valid JSON") from exc

    if isinstance(data, list):
        return {
            str(record.get("session_id")): record
            for record in data
            if isinstance(record, dict) and record.get("session_id")
        }
    if isinstance(data, dict):
        return {
            str(session_id): record
            for session_id, record in data.items()
            if isinstance(record, dict)
        }

    raise HTTPException(status_code=500, detail="interviews.json must contain an object or array")


def save_interview_archive(archive: dict[str, dict[str, Any]]) -> None:
    INTERVIEW_ARCHIVE_FILE.write_text(
        json.dumps(archive, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


EXPORT_HEADERS = [
    "会话ID",
    "更新时间",
    "公司",
    "姓名",
    "职位",
    "负责内容",
    "流程",
    "频率",
    "最费时间",
    "出错后果",
    "现用工具/费用",
    "改善尝试",
    "数据/规则",
    "不在范围",
    "原始对话",
]


RECAP_FIELD_PATTERNS = {
    "company": [r"公司[：:]\s*(.+)", r"企业[：:]\s*(.+)"],
    "name": [r"姓名[：:]\s*(.+)", r"联系人[：:]\s*(.+)"],
    "title": [r"职位[：:]\s*(.+)", r"岗位[：:]\s*(.+)"],
    "responsibility": [r"负责内容[：:]\s*(.+)", r"负责[：:]\s*(.+)"],
    "flow": [r"流程[：:]\s*(.+)"],
    "frequency": [r"频率[：:]\s*(.+)"],
    "time_cost": [r"最费时间[：:]\s*(.+)", r"最花时间[：:]\s*(.+)"],
    "error_impact": [r"出错后果[：:]\s*(.+)", r"错误后果[：:]\s*(.+)"],
    "current_tools": [r"现在用[：:]\s*(.+)", r"现用工具/费用[：:]\s*(.+)"],
    "improvement": [r"改善尝试[：:]\s*(.+)", r"之前试过[：:]\s*(.+)"],
    "data_rules": [r"规则在[：:]\s*(.+)", r"数据/规则[：:]\s*(.+)"],
    "out_of_scope": [r"不在范围[：:]\s*(.+)"],
}

INTERVIEW_EXPORT_FIELDS = [
    "company",
    "name",
    "title",
    "responsibility",
    "flow",
    "frequency",
    "time_cost",
    "error_impact",
    "current_tools",
    "improvement",
    "data_rules",
    "out_of_scope",
]

INTERVIEW_FIELD_FALLBACKS = {
    "company": "未确认公司",
    "name": "未询问",
    "title": "未确认职位",
    "responsibility": "未明确说明，按对话暂归为其日常负责事项",
    "flow": "未完整说明，按原始对话暂整理",
    "frequency": "未提及，暂按按需/低频处理",
    "time_cost": "未提及，暂按存在人工耗时处理",
    "error_impact": "未提及明确错误，暂记为未出过错",
    "current_tools": "未提及，暂记为现有人工/常用工具处理",
    "improvement": "未提及，暂记为未尝试",
    "data_rules": "未提及，暂按经验/内部文件处理",
    "out_of_scope": "未提及，暂记为无特殊情况",
}

ARCHIVE_EVENT_TYPES = {"close_session", "session_close", "archive_session"}

SESSION_MODE_INTERVIEW = "interview"
SESSION_MODE_ORDER = "order"
SESSION_MODE_RECEIPT = "receipt"
SESSION_MODES = {SESSION_MODE_INTERVIEW, SESSION_MODE_ORDER, SESSION_MODE_RECEIPT}

ORDER_MODE_COMMANDS = {"订单", "录单", "下单", "订单模式", "开始订单", "开始录单"}
RECEIPT_MODE_COMMANDS = {"入库", "入库模式", "产成品入库", "开始入库", "成品入库"}
INTERVIEW_MODE_COMMANDS = {"问诊", "访谈", "需求访谈", "问诊模式", "访谈模式", "结束订单", "退出订单", "结束入库", "退出入库"}
ORDER_EXPORT_COMMANDS = {"导出订单", "订单导出", "下载订单", "订单表", "导出订单表"}
ORDER_CONFIRM_COMMANDS = {"确认", "确认订单", "保存", "保存订单", "提交", "提交订单"}
ORDER_CANCEL_COMMANDS = {"取消", "取消订单", "清空", "清空订单"}

ORDER_KIND_BASE = "base"
ORDER_KIND_PATCH = "patch"
ORDER_SOURCE_EXCEL = "excel"
ORDER_SOURCE_PHOTO = "photo"
ORDER_SOURCE_TEXT = "text"
ORDER_STATUS_NEW = "new"
ORDER_STATUS_FETCHED = "fetched"
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

ORDER_SUMMARY_HEADERS = [
    "门店/区域",
    "商品",
    "单位",
    "数量合计",
    "订单行数",
    "最近创建时间",
]

ORDER_CONTRACT_EXPORT_HEADERS = [
    "ID",
    "类型",
    "来源",
    "状态",
    "已确认",
    "门店/区域",
    "订单号",
    "下单人",
    "下单日期",
    "送达日期",
    "变更类型",
    "行号",
    "商品编码",
    "商品名称",
    "规格",
    "单位",
    "数量",
    "单价",
    "分类",
    "原始文本",
    "原始引用",
    "创建时间",
]

EXCEL_HEADER_ALIASES = {
    "store": {"门店", "门店/区域", "区域", "店铺", "店名", "客户", "客户名称", "收货方", "门店名称"},
    "order_no": {"订单号", "单号", "订单编号", "编号"},
    "orderer": {"下单人", "订货人", "订货员", "制单人", "联系人"},
    "order_date": {"下单日期", "订单日期", "日期", "制单日期"},
    "deliver_date": {"送达日期", "送货日期", "配送日期", "交付日期", "到货日期"},
    "code": {"商品编码", "编码", "货号", "商品代码", "code", "物料编码"},
    "name": {"商品名称", "品名", "名称", "商品", "name", "物料名称"},
    "spec": {"规格", "规格型号", "型号", "包装规格", "spec"},
    "unit": {"单位", "unit"},
    "qty": {"数量", "订货数量", "下单数量", "箱数", "件数", "qty"},
    "price": {"单价", "价格", "price"},
    "category": {"分类", "类别", "品类", "category"},
}

EXCEL_METADATA_LABELS = {
    "store": {"门店", "门店/区域", "区域", "店铺", "客户", "收货方"},
    "order_no": {"订单号", "单号", "订单编号"},
    "orderer": {"下单人", "订货人", "联系人"},
    "order_date": {"下单日期", "订单日期"},
    "deliver_date": {"送达日期", "送货日期", "配送日期", "到货日期"},
}


def clean_export_value(value: str) -> str:
    value = value.strip()
    value = re.sub(r"\s+", " ", value)
    return value.strip(" ，,。.;；")


def extract_first_match(text: str, patterns: list[str]) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.MULTILINE)
        if match:
            return clean_export_value(match.group(1))
    return ""


def extract_field_from_conversation(messages: list[dict[str, str]], patterns: list[str]) -> str:
    for message in messages:
        if message.get("role") != "user":
            continue
        value = extract_first_match(message.get("content", ""), patterns)
        if value:
            return value
    return ""


def latest_recap_text(messages: list[dict[str, str]]) -> str:
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        content = message.get("content", "")
        if "流程" in content and ("频率" in content or "最费时间" in content):
            return content
    return ""


def conversation_to_text(messages: list[dict[str, str]]) -> str:
    lines: list[str] = []
    for message in messages:
        role = message.get("role", "")
        content = message.get("content", "")
        if not content:
            continue
        prefix = "用户" if role == "user" else "机器人" if role == "assistant" else role
        lines.append(f"{prefix}: {content}")
    return "\n".join(lines)


def extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("missing JSON object")

    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("JSON root must be an object")
    return data


def normalize_command(message: str) -> str:
    return re.sub(r"\s+", "", message.strip()).lower()


def strip_order_inline_prefix(message: str) -> str | None:
    match = re.match(r"^\s*(订单|录单|下单)(?:\s*[：:]|\s+)(.+)$", message, flags=re.DOTALL)
    if not match:
        return None
    return match.group(2).strip()


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
    mode = str(get_session_record(user_id).get("mode") or SESSION_MODE_INTERVIEW)
    if mode not in SESSION_MODES:
        return SESSION_MODE_INTERVIEW
    return mode


def set_session_mode(user_id: str, mode: str) -> None:
    if mode not in SESSION_MODES:
        raise ValueError(f"Invalid session mode: {mode}")

    record = get_session_record(user_id)
    record["mode"] = mode
    save_session_record(user_id, record)


def get_order_draft(user_id: str) -> dict[str, Any]:
    draft = get_session_record(user_id).get("order_draft")
    if isinstance(draft, dict):
        return normalize_order_draft(draft)
    return normalize_order_draft({})


def save_order_draft(user_id: str, draft: dict[str, Any]) -> None:
    record = get_session_record(user_id)
    record["mode"] = SESSION_MODE_ORDER
    record["order_draft"] = normalize_order_draft(draft)
    save_session_record(user_id, record)


def clear_order_draft(user_id: str) -> None:
    record = get_session_record(user_id)
    record["mode"] = SESSION_MODE_ORDER
    record.pop("order_draft", None)
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
    save_session_record(user_id, record)


def clear_receipt_draft(user_id: str) -> None:
    record = get_session_record(user_id)
    record["mode"] = SESSION_MODE_RECEIPT
    record.pop("receipt_draft", None)
    save_session_record(user_id, record)


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


def llm_parse_order_draft(existing_draft: dict[str, Any], message: str) -> dict[str, Any]:
    existing_kind = existing_draft.get("kind") if isinstance(existing_draft, dict) else ""
    if existing_kind == ORDER_KIND_BASE:
        schema_hint = """
输出基础订单 JSON：
{
  "kind":"base","source":"photo","store":"","order_no":"","orderer":"",
  "order_date":"","deliver_date":"",
  "items":[{"code":"","name":"","spec":"","unit":"","qty":0,"price":null,"category":""}],
  "confirmed":false,"status":"new","raw_ref":"","created_at":""
}
""".strip()
        task_hint = "已有草稿是照片/Excel基础订单。新消息通常是在纠错或补充字段，请合并到已有基础订单里。"
    else:
        schema_hint = """
输出文字补丁 JSON：
{
  "kind":"patch","source":"text","store":"",
  "items":[{"code":null,"name":"","spec":null,"unit":"","qty":0}],
  "change_type":"add","order_date":"","deliver_date":"",
  "confirmed":false,"status":"new","raw_text":"","raw_ref":"","created_at":""
}
""".strip()
        task_hint = "新消息是群里文字加货/改量。只负责问清门店、商品、数量，不要挂靠到具体订单。"

    today = datetime.now().date().isoformat()
    prompt = f"""
你是馄饨侯订单机器人。请按接口契约把微信消息整理成 Web 工具可直接使用的 JSON。

只输出一个 JSON 对象，不要解释，不要 Markdown。

{task_hint}

今天日期：{today}

字段要求：
- base 用于标准 Excel 或照片订单；patch 用于文字加货/改量。
- store 是门店/区域，必须尽量从原文提取。
- order_date 是下单日期/归属日期，是 Web 工具归批字段。文字里出现“6.21订”“6月21日订”“2026-6-21下单”时，必须填 order_date=YYYY-MM-DD。
- qty、price 输出数字；缺失用 null。
- code 可能为空或 "#N/A"，照实保留。
- 文本里出现“加、追加、再来、补”通常 change_type=add；出现“改、换成、数量改为”通常 change_type=modify。
- deliver_date 只是可选送达备注。文字里出现送达/到货时间时可以填；不要用 deliver_date 替代 order_date。
- created_at 表示消息收到时间，不要从用户文本推断，不要填送达时间。
- 信息没出现不要编造，留空字符串或 null。

{schema_hint}

已有草稿：
{json.dumps(existing_draft, ensure_ascii=False)}

新消息：
{message}
""".strip()

    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": "你只输出可解析 JSON。"},
            {"role": "user", "content": prompt},
        ],
        temperature=0,
    )
    raw = response.choices[0].message.content or ""
    parsed = extract_json_object(raw)
    if parsed.get("kind") == ORDER_KIND_PATCH:
        parsed["raw_text"] = parsed.get("raw_text") or message
    explicit_order_date = extract_explicit_order_date(message)
    if explicit_order_date:
        parsed["order_date"] = explicit_order_date
    parsed["created_at"] = existing_draft.get("created_at") if isinstance(existing_draft, dict) else ""
    parsed["created_at"] = parsed["created_at"] or now_iso()
    return normalize_order_draft(parsed)


def normalize_excel_header(value: Any) -> str:
    text = clean_order_value(value).lower()
    return re.sub(r"[\s:_：/\\（）()\[\]【】\-]+", "", text)


def excel_header_key(value: Any) -> str | None:
    normalized = normalize_excel_header(value)
    if not normalized:
        return None
    for key, aliases in EXCEL_HEADER_ALIASES.items():
        for alias in aliases:
            if normalized == normalize_excel_header(alias):
                return key
    return None


def find_excel_header_row(rows: list[tuple[Any, ...]]) -> tuple[int, dict[int, str]]:
    best_index = -1
    best_map: dict[int, str] = {}
    best_score = 0
    for index, row in enumerate(rows[:30]):
        header_map: dict[int, str] = {}
        for column_index, value in enumerate(row):
            key = excel_header_key(value)
            if key and key not in header_map.values():
                header_map[column_index] = key
        score = len(header_map)
        if score > best_score:
            best_index = index
            best_map = header_map
            best_score = score

    if best_score < 2 or "name" not in best_map.values() or "qty" not in best_map.values():
        raise ValueError("Excel header row not found; expected product name and quantity columns")
    return best_index, best_map


def extract_excel_metadata(rows: list[tuple[Any, ...]], header_index: int) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for row in rows[: max(header_index, 1)]:
        cells = list(row)
        for index, value in enumerate(cells):
            text = clean_order_value(value)
            if not text:
                continue
            for field, labels in EXCEL_METADATA_LABELS.items():
                if metadata.get(field):
                    continue
                for label in labels:
                    label_text = normalize_excel_header(label)
                    value_text = normalize_excel_header(text)
                    if value_text == label_text and index + 1 < len(cells):
                        metadata[field] = cells[index + 1]
                    elif value_text.startswith(label_text) and len(text) > len(label):
                        metadata[field] = re.sub(rf"^{re.escape(label)}\s*[：: ]*", "", text).strip()
    return metadata


def row_value_by_header(row: tuple[Any, ...], header_map: dict[int, str], field: str) -> Any:
    for index, key in header_map.items():
        if key == field and index < len(row):
            return row[index]
    return None


def parse_excel_order_payloads(file_bytes: bytes, raw_ref: str) -> list[dict[str, Any]]:
    workbook = load_workbook(BytesIO(file_bytes), data_only=True)
    sheet = workbook.active
    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        raise ValueError("Excel file is empty")

    header_index, header_map = find_excel_header_row(rows)
    metadata = extract_excel_metadata(rows, header_index)
    grouped: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}

    for row in rows[header_index + 1 :]:
        item = {
            "code": row_value_by_header(row, header_map, "code"),
            "name": row_value_by_header(row, header_map, "name"),
            "spec": row_value_by_header(row, header_map, "spec"),
            "unit": row_value_by_header(row, header_map, "unit"),
            "qty": row_value_by_header(row, header_map, "qty"),
            "price": row_value_by_header(row, header_map, "price"),
            "category": row_value_by_header(row, header_map, "category"),
        }
        normalized_item = normalize_base_item(item)
        if not normalized_item.get("name") and normalized_item.get("qty") is None:
            continue

        store = row_value_by_header(row, header_map, "store") or metadata.get("store") or ""
        order_no = row_value_by_header(row, header_map, "order_no") or metadata.get("order_no") or ""
        orderer = row_value_by_header(row, header_map, "orderer") or metadata.get("orderer") or ""
        order_date = row_value_by_header(row, header_map, "order_date") or metadata.get("order_date") or ""
        deliver_date = row_value_by_header(row, header_map, "deliver_date") or metadata.get("deliver_date") or ""

        key = (
            clean_order_value(store),
            clean_order_value(order_no),
            clean_order_value(orderer),
            normalize_order_date_text(order_date),
            normalize_date_text(deliver_date),
        )
        payload = grouped.setdefault(
            key,
            {
                "kind": ORDER_KIND_BASE,
                "source": ORDER_SOURCE_EXCEL,
                "store": store,
                "order_no": order_no,
                "orderer": orderer,
                "order_date": order_date,
                "deliver_date": deliver_date,
                "items": [],
                "confirmed": True,
                "status": ORDER_STATUS_NEW,
                "raw_ref": raw_ref,
                "created_at": now_iso(),
            },
        )
        payload["items"].append(normalized_item)

    payloads = [normalize_order_payload(payload) for payload in grouped.values()]
    if not payloads:
        raise ValueError("Excel file contains no order item rows")
    return payloads


def image_data_uri(image_bytes: bytes, mime_type: str | None) -> str:
    mime = mime_type or "image/jpeg"
    if not mime.startswith("image/"):
        mime = "image/jpeg"
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def ensure_vision_recognition_ready() -> None:
    if vision_client is None:
        raise RuntimeError("vision model is not configured")


def call_vision_json(prompt: str, image_bytes: bytes, mime_type: str | None) -> dict[str, Any]:
    ensure_vision_recognition_ready()
    response = vision_client.chat.completions.create(
        model=VISION_MODEL,
        messages=[
            {"role": "system", "content": "你只输出可解析 JSON。"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_data_uri(image_bytes, mime_type)}},
                ],
            },
        ],
        temperature=0,
    )
    raw = response.choices[0].message.content or ""
    return extract_json_object(raw)


def llm_parse_photo_order(image_bytes: bytes, mime_type: str | None, raw_ref: str) -> dict[str, Any]:
    prompt = """
你是馄饨侯订单照片识别助手。请读取图片中的订单表格或手写订单，输出 Web 工具可直接使用的基础订单 JSON。

只输出一个 JSON 对象，不要解释，不要 Markdown。

格式：
{
  "kind":"base",
  "source":"photo",
  "store":"门店/区域",
  "order_no":"有则填，没有留空",
  "orderer":"可空",
  "order_date":"YYYY-MM-DD 或原文，可空",
  "deliver_date":"YYYY-MM-DD 或原文，可空",
  "items":[
    {"code":"商品编码或#N/A或空","name":"商品名称","spec":"规格","unit":"单位","qty":2,"price":267.32,"category":"分类"}
  ],
  "confirmed":false,
  "status":"new",
  "raw_ref":"",
  "created_at":""
}

要求：
- qty 和 price 尽量输出数字，识别不到用 null。
- code、spec、category 识别不到用空字符串。
- deliver_date 只填客户要求送达/到货日期；created_at 不要填送达日期。
- 不要编造图片里没有的信息。
- 多个商品拆成多个 items。
""".strip()

    parsed = call_vision_json(prompt, image_bytes, mime_type)
    parsed["kind"] = ORDER_KIND_BASE
    parsed["source"] = ORDER_SOURCE_PHOTO
    parsed["confirmed"] = False
    parsed["status"] = ORDER_STATUS_NEW
    parsed["raw_ref"] = raw_ref
    parsed["created_at"] = now_iso()
    return normalize_order_payload(parsed)


def llm_parse_receipt_photo(image_bytes: bytes, mime_type: str | None, raw_ref: str) -> dict[str, Any]:
    today = datetime.now().date().isoformat()
    prompt = f"""
你是产成品入库照片识别助手。请读取车间发来的产成品入库照片，只识别成品名称和数量清单。

只输出一个 JSON 对象，不要解释，不要 Markdown。

格式：
{{
  "date": "YYYY-MM-DD",
  "items": [
    {{"code": null, "name": "鸡汤虾肉馄饨", "spec": null, "unit": "箱", "qty": 50}}
  ]
}}

规则：
- 今天日期：{today}
- date 是车间入库日期。图片里有日期就按图片日期；没有日期就填今天。
- 不要输出 store，入库是车间总量，不分门店。
- items[].qty 必须是数字，不要带“箱/袋/件”等单位字。
- 单位放到 unit；识别不到单位填 null。
- code/spec 可选，识别不到填 null。
- 不要编造图片里没有的成品。
""".strip()
    parsed = call_vision_json(prompt, image_bytes, mime_type)
    parsed["raw_ref"] = raw_ref
    parsed["created_at"] = now_iso()
    return normalize_receipt_payload(parsed)


def save_confirmed_order(user_id: str, draft: dict[str, Any]) -> tuple[int, int]:
    payload = normalize_order_payload(draft)
    missing = order_draft_missing_fields(payload)
    if missing:
        raise ValueError("order draft missing fields: " + ",".join(missing))

    payload["confirmed"] = True
    payload["status"] = ORDER_STATUS_NEW
    payload["raw_ref"] = payload.get("raw_ref") or user_id
    saved = insert_order_payload(payload)
    return int(saved["id"]), len(saved.get("items") or [])


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


def normalize_interview_record(record: dict[str, Any]) -> dict[str, str]:
    normalized = {key: str(value or "").strip() for key, value in record.items()}

    for field in INTERVIEW_EXPORT_FIELDS:
        if not normalized.get(field):
            normalized[field] = INTERVIEW_FIELD_FALLBACKS[field]

    return normalized


def llm_complete_interview_record(base_record: dict[str, str], transcript: str) -> dict[str, str]:
    prompt = f"""
请把下面的微信访谈对话整理成 Excel 表格字段，只输出一个 JSON 对象，不要输出解释。

要求：
- 字段必须包含：company, name, title, responsibility, flow, frequency, time_cost, error_impact, current_tools, improvement, data_rules, out_of_scope。
- company 必须尽量使用用户原文里的公司名称；没明确说就填“未确认公司”。
- title 必须尽量使用用户原文里的职位/岗位/工种；没明确说就按对话合理推断，仍不确定填“未确认职位”。
- name 不再追问；如果对话没提到姓名或称呼，填“未询问”。
- 其他字段不要留空；信息不全时，根据对话做保守推断，并用简短中文写清楚“暂推”。
- 不要编造具体金额、具体系统名或具体时间；没有提到就写“未提及，暂推...”。

现有初步抽取：
{json.dumps({field: base_record.get(field, "") for field in INTERVIEW_EXPORT_FIELDS}, ensure_ascii=False)}

原始对话：
{transcript}
""".strip()

    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": "你是访谈记录结构化助手，只输出可解析 JSON。"},
            {"role": "user", "content": prompt},
        ],
    )
    raw = response.choices[0].message.content or ""
    data = extract_json_object(raw)

    completed = dict(base_record)
    for field in INTERVIEW_EXPORT_FIELDS:
        value = data.get(field)
        if value:
            completed[field] = clean_export_value(str(value))
    return normalize_interview_record(completed)


def interview_record_from_history(session_id: str, messages: list[dict[str, str]]) -> dict[str, str]:
    recap = latest_recap_text(messages)
    text_for_extract = recap or conversation_to_text(messages)

    record = {
        "session_id": session_id,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "company": "",
        "name": "",
        "title": "",
        "responsibility": "",
        "flow": "",
        "frequency": "",
        "time_cost": "",
        "error_impact": "",
        "current_tools": "",
        "improvement": "",
        "data_rules": "",
        "out_of_scope": "",
        "transcript": conversation_to_text(messages),
    }

    for field, patterns in RECAP_FIELD_PATTERNS.items():
        record[field] = extract_first_match(text_for_extract, patterns)

    if not record["company"]:
        record["company"] = extract_field_from_conversation(
            messages,
            [
                r"公司(?:是|叫)?[：:，, ]\s*([^，,。；;\n]+)",
                r"(?:我是|我在)([^，,。；;\n]{2,40}(?:公司|集团|店|厂|餐饮|科技|有限|有限公司))",
            ],
        )

    if not record["company"]:
        record["company"] = "未确认公司"

    return normalize_interview_record(record)


def last_user_message_ts(messages: list[dict[str, Any]]) -> float | None:
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue

        ts = message.get("ts")
        if isinstance(ts, (int, float)):
            return float(ts)
        if isinstance(ts, str):
            try:
                return float(ts)
            except ValueError:
                pass

        created_at = message.get("created_at")
        if isinstance(created_at, str):
            try:
                return datetime.fromisoformat(created_at).timestamp()
            except ValueError:
                return None

    return None


def completed_interview_record_from_history(
    session_id: str,
    messages: list[dict[str, str]],
    reason: str,
) -> dict[str, str]:
    record = interview_record_from_history(session_id, messages)
    transcript = record["transcript"]

    try:
        record = llm_complete_interview_record(record, transcript)
    except Exception as exc:
        logger.warning(
            "interview_archive_llm_failed session_id=%s reason=%s error=%s",
            session_id,
            reason,
            exc,
        )

    record["session_id"] = session_id
    record["updated_at"] = now_iso()
    record["archived_at"] = now_iso()
    record["archive_reason"] = reason
    record["status"] = "archived"
    record["transcript"] = transcript
    record["_message_count"] = str(len(messages))
    record["_last_user_ts"] = str(last_user_message_ts(messages) or "")
    return normalize_interview_record(record)


def archive_interview_session(session_id: str, reason: str) -> dict[str, str] | None:
    with MEMORY_LOCK:
        memory = load_memory()
        messages = memory.get(session_id, [])

    if not isinstance(messages, list):
        logger.warning("interview_archive_invalid_history session_id=%s", session_id)
        return None
    if not any(isinstance(message, dict) and message.get("role") == "user" for message in messages):
        logger.info("interview_archive_skipped_no_user_messages session_id=%s reason=%s", session_id, reason)
        return None

    message_count = len(messages)
    last_user_ts = str(last_user_message_ts(messages) or "")
    with INTERVIEW_ARCHIVE_LOCK:
        archive = load_interview_archive()
        existing = archive.get(session_id)
        if (
            existing
            and str(existing.get("_message_count", "")) == str(message_count)
            and str(existing.get("_last_user_ts", "")) == last_user_ts
        ):
            return normalize_interview_record(existing)

    record = completed_interview_record_from_history(session_id, messages, reason)
    with INTERVIEW_ARCHIVE_LOCK:
        archive = load_interview_archive()
        archive[session_id] = record
        save_interview_archive(archive)

    logger.info(
        "interview_archived session_id=%s reason=%s message_count=%s company=%s title=%s",
        session_id,
        reason,
        message_count,
        record.get("company"),
        record.get("title"),
    )
    return record


def archive_idle_interviews_once() -> None:
    now = time.time()
    candidates: list[tuple[str, str]] = []

    with MEMORY_LOCK:
        memory = load_memory()
        for session_id, messages in memory.items():
            if not isinstance(messages, list):
                continue
            if not any(isinstance(message, dict) and message.get("role") == "user" for message in messages):
                continue

            last_ts = last_user_message_ts(messages)
            if not last_ts:
                continue
            if now - last_ts >= INTERVIEW_IDLE_ARCHIVE_SECONDS:
                candidates.append((str(session_id), str(last_ts)))

    with INTERVIEW_ARCHIVE_LOCK:
        archive = load_interview_archive()
        filtered_candidates: list[str] = []
        for session_id, last_ts in candidates:
            existing = archive.get(session_id)
            if existing and str(existing.get("_last_user_ts", "")) == last_ts:
                continue
            filtered_candidates.append(session_id)

    for session_id in filtered_candidates:
        try:
            archive_interview_session(session_id, "idle_timeout")
        except Exception as exc:
            logger.exception("interview_idle_archive_failed session_id=%s error=%s", session_id, exc)


def archive_export_backfill_interviews_once() -> None:
    now = time.time()
    candidates: list[str] = []

    with MEMORY_LOCK:
        memory = load_memory()
        with INTERVIEW_ARCHIVE_LOCK:
            archive = load_interview_archive()

        for session_id, messages in memory.items():
            if session_id in archive:
                continue
            if not isinstance(messages, list):
                continue
            if not any(isinstance(message, dict) and message.get("role") == "user" for message in messages):
                continue

            last_ts = last_user_message_ts(messages)
            if not last_ts or now - last_ts >= INTERVIEW_IDLE_ARCHIVE_SECONDS:
                candidates.append(str(session_id))

    for session_id in candidates:
        try:
            archive_interview_session(session_id, "export_backfill")
        except Exception as exc:
            logger.exception("interview_export_backfill_failed session_id=%s error=%s", session_id, exc)


async def interview_archive_sweeper() -> None:
    while True:
        await asyncio.sleep(INTERVIEW_ARCHIVE_POLL_SECONDS)
        try:
            await asyncio.to_thread(archive_idle_interviews_once)
        except Exception as exc:
            logger.exception("interview_archive_sweeper_failed error=%s", exc)


@app.on_event("startup")
async def start_interview_archive_sweeper() -> None:
    asyncio.create_task(interview_archive_sweeper())


def safe_sheet_name(company: str, used_names: set[str]) -> str:
    base = re.sub(r"[\[\]\*:/\\?]", "_", company).strip()
    if not base:
        base = "未确认公司"
    base = base[:31]

    name = base
    suffix = 2
    while name in used_names:
        suffix_text = f"_{suffix}"
        name = f"{base[:31 - len(suffix_text)]}{suffix_text}"
        suffix += 1

    used_names.add(name)
    return name


def write_sheet_rows(sheet, rows: list[list[str]]) -> None:
    sheet.append(EXPORT_HEADERS)
    for row in rows:
        sheet.append(row)

    header_fill = PatternFill("solid", fgColor="1F4E78")
    header_font = Font(color="FFFFFF", bold=True)
    for cell in sheet[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(vertical="center", wrap_text=True)

    widths = [18, 18, 22, 12, 16, 24, 36, 18, 24, 28, 28, 28, 32, 28, 70]
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width

    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)

    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions


def collect_interview_records() -> list[dict[str, str]]:
    archive_export_backfill_interviews_once()

    with INTERVIEW_ARCHIVE_LOCK:
        archive = load_interview_archive()

    with MEMORY_LOCK:
        memory = load_memory()

    records: list[dict[str, str]] = []
    archived_session_ids = set()
    for session_id, record in archive.items():
        normalized = normalize_interview_record(record)
        normalized["session_id"] = session_id
        records.append(normalized)
        archived_session_ids.add(session_id)

    for session_id, messages in memory.items():
        if session_id in archived_session_ids:
            continue
        if not isinstance(messages, list):
            continue
        if not any(message.get("role") == "user" for message in messages if isinstance(message, dict)):
            continue
        records.append(interview_record_from_history(session_id, messages))

    return records


def record_to_export_row(record: dict[str, str]) -> list[str]:
    return [
        record["session_id"],
        record["updated_at"],
        record["company"],
        record["name"],
        record["title"],
        record["responsibility"],
        record["flow"],
        record["frequency"],
        record["time_cost"],
        record["error_impact"],
        record["current_tools"],
        record["improvement"],
        record["data_rules"],
        record["out_of_scope"],
        record["transcript"],
    ]


def build_interview_export() -> Path:
    records = collect_interview_records()

    workbook = Workbook()
    summary_sheet = workbook.active
    summary_sheet.title = "全部反馈"

    rows_by_company: dict[str, list[list[str]]] = {}
    all_rows: list[list[str]] = []

    for record in records:
        row = record_to_export_row(record)
        all_rows.append(row)
        rows_by_company.setdefault(record["company"], []).append(row)

    write_sheet_rows(summary_sheet, all_rows)

    used_sheet_names = {"全部反馈"}
    for company in sorted(rows_by_company):
        sheet = workbook.create_sheet(safe_sheet_name(company, used_sheet_names))
        write_sheet_rows(sheet, rows_by_company[company])

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = EXPORT_DIR / f"interviews-{datetime.now().strftime('%Y%m%d-%H%M%S')}.xlsx"
    workbook.save(output_path)
    return output_path


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


def order_record_to_export_row(record: dict[str, str]) -> list[str]:
    return [
        record["id"],
        record["kind"],
        record["source"],
        record["status"],
        record["confirmed"],
        record["store"],
        record["order_no"],
        record["orderer"],
        record["order_date"],
        record["deliver_date"],
        record["change_type"],
        record["line_no"],
        record["code"],
        record["name"],
        record["spec"],
        record["unit"],
        record["qty"],
        record["price"],
        record["category"],
        record["raw_text"],
        record["raw_ref"],
        record["created_at"],
    ]


def parse_quantity_number(value: str) -> float | None:
    match = re.search(r"-?\d+(?:\.\d+)?", value)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def format_quantity_total(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def build_order_summary_rows(records: list[dict[str, str]]) -> list[list[str]]:
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for record in records:
        key = (record["store"], record["name"], record["unit"])
        group = groups.setdefault(
            key,
            {
                "total": 0.0,
                "numeric_count": 0,
                "raw_quantities": [],
                "count": 0,
                "latest_created_at": "",
            },
        )
        group["count"] += 1
        quantity = record["qty"]
        number = parse_quantity_number(quantity)
        if number is None:
            if quantity:
                group["raw_quantities"].append(quantity)
        else:
            group["total"] += number
            group["numeric_count"] += 1
        if record["created_at"] > group["latest_created_at"]:
            group["latest_created_at"] = record["created_at"]

    rows: list[list[str]] = []
    for (store, product, unit), group in sorted(groups.items()):
        quantity_parts: list[str] = []
        if group["numeric_count"]:
            quantity_parts.append(format_quantity_total(float(group["total"])))
        if group["raw_quantities"]:
            quantity_parts.append("、".join(group["raw_quantities"]))
        rows.append(
            [
                store,
                product,
                unit,
                "；".join(quantity_parts),
                str(group["count"]),
                str(group["latest_created_at"]),
            ]
        )
    return rows


def write_order_table_sheet(sheet, headers: list[str], rows: list[list[str]], widths: list[int]) -> None:
    sheet.append(headers)
    for row in rows:
        sheet.append(row)

    header_fill = PatternFill("solid", fgColor="2F5597")
    header_font = Font(color="FFFFFF", bold=True)
    for cell in sheet[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(vertical="center", wrap_text=True)

    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width

    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)

    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions


def build_order_export() -> Path:
    records = collect_order_records()

    workbook = Workbook()
    detail_sheet = workbook.active
    detail_sheet.title = "全部订单"
    detail_rows = [order_record_to_export_row(record) for record in records]
    write_order_table_sheet(
        detail_sheet,
        ORDER_CONTRACT_EXPORT_HEADERS,
        detail_rows,
        [10, 10, 10, 10, 10, 22, 18, 14, 14, 14, 12, 8, 16, 28, 20, 10, 12, 12, 14, 36, 44, 20],
    )

    summary_sheet = workbook.create_sheet("按门店商品汇总")
    write_order_table_sheet(
        summary_sheet,
        ORDER_SUMMARY_HEADERS,
        build_order_summary_rows(records),
        [24, 26, 10, 16, 12, 20],
    )

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = EXPORT_DIR / f"orders-{datetime.now().strftime('%Y%m%d-%H%M%S')}.xlsx"
    workbook.save(output_path)
    return output_path


def trim_history(history: list[dict[str, str]]) -> list[dict[str, str]]:
    return history[-MAX_HISTORY_MESSAGES:]


def build_llm_messages(history: list[dict[str, str]]) -> list[dict[str, str]]:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    if PROMPT_TURN_CONTEXT:
        user_turns = sum(1 for message in history if message.get("role") == "user")
        messages.append(
            {
                "role": "system",
                "content": (
                    f"运行时信息：当前会话已收到 {user_turns} 个用户回合。"
                    "如果系统提示包含阶段流程，请用这个数字判断访谈节奏。"
                    "如果回合数只是建议，不要把它当作硬性拒答或卡死条件。"
                ),
            }
        )

    return [*messages, *history]


def call_llm(user_id: str, history: list[dict[str, str]]) -> str:
    messages = build_llm_messages(history)

    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
        )
        answer = response.choices[0].message.content
    except Exception as exc:
        print("========== LLM ERROR ==========")
        print(str(exc))
        traceback.print_exc()
        print("================================")
        logger.warning(
            "llm_failed user_id=%s history_length=%s model=%s error_type=%s",
            user_id,
            len(history),
            MODEL_NAME,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error": "LLM request failed",
                "real_error": str(exc),
            },
        ) from exc

    logger.info(
        "llm_success user_id=%s history_length=%s model=%s",
        user_id,
        len(history),
        MODEL_NAME,
    )
    return answer or ""


def handle_order_user_message(user_id: str, message: str, raw_ref: str | None = None) -> ChatResponse:
    command = normalize_command(message)
    history_length = user_order_count(user_id)

    if command in ORDER_CANCEL_COMMANDS:
        clear_order_draft(user_id)
        return ChatResponse(
            user_id=user_id,
            answer="已清空当前订单草稿。你可以直接发送下一张订单。",
            history_length=history_length,
        )

    if command in ORDER_CONFIRM_COMMANDS:
        draft = get_order_draft(user_id)
        missing = order_draft_missing_fields(draft)
        if missing:
            return ChatResponse(
                user_id=user_id,
                answer=(
                    "当前订单还不能保存，缺少："
                    + "、".join(missing)
                    + "\n"
                    + format_order_draft_summary(draft)
                    + "\n请直接补充缺失信息，或发“取消”清空。"
                ),
                history_length=history_length,
            )

        try:
            order_id, line_count = save_confirmed_order(user_id, draft)
        except Exception as exc:
            logger.warning("order_save_failed user_id=%s error=%s", user_id, exc)
            return ChatResponse(
                user_id=user_id,
                answer="订单保存失败了，请稍后再试，或联系管理员查看后台日志。",
                history_length=history_length,
            )

        clear_order_draft(user_id)
        return ChatResponse(
            user_id=user_id,
            answer=f"已保存订单入库，ID {order_id}，共 {line_count} 行商品。继续发下一张即可。",
            history_length=history_length + line_count,
        )

    existing_draft = get_order_draft(user_id)
    try:
        draft = llm_parse_order_draft(existing_draft, message)
    except Exception as exc:
        logger.warning("order_parse_failed user_id=%s error=%s", user_id, exc)
        return ChatResponse(
            user_id=user_id,
            answer="这条订单我没有解析成功。请按“门店 + 商品 + 数量”的格式重发，例如：老三家 鸡腿 20件。",
            history_length=history_length,
        )

    draft["raw_ref"] = draft.get("raw_ref") or raw_ref or user_id
    if draft.get("kind") == ORDER_KIND_PATCH:
        draft["raw_text"] = draft.get("raw_text") or message
    draft["confirmed"] = False
    draft["status"] = ORDER_STATUS_NEW
    draft["created_at"] = draft.get("created_at") or now_iso()
    draft = normalize_order_draft(draft)
    save_order_draft(user_id, draft)

    missing = order_draft_missing_fields(draft)
    summary = format_order_draft_summary(draft)
    if missing:
        answer = (
            "我先整理成订单草稿，还缺："
            + "、".join(missing)
            + "\n"
            + summary
            + "\n请直接补充缺失信息，或发“取消”清空。"
        )
    else:
        answer = (
            "我整理成待确认订单：\n"
            + summary
            + "\n确认无误请回复“确认”；要修改就直接发修改内容。"
        )

    return ChatResponse(
        user_id=user_id,
        answer=answer,
        history_length=history_length,
    )


def format_receipt_draft_summary(draft: dict[str, Any]) -> str:
    if not draft:
        return "暂无入库草稿"
    lines = [
        f"入库日期：{draft.get('date') or '未填写'}",
    ]
    items = draft.get("items") if isinstance(draft.get("items"), list) else []
    if not items:
        lines.append("成品：未填写")
    else:
        lines.append("成品：")
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            parts = [
                str(item.get("code") or "").strip(),
                str(item.get("name") or "未填写成品").strip(),
                str(item.get("spec") or "").strip(),
                f"{item.get('qty') if item.get('qty') is not None else '未填写数量'}{item.get('unit') or ''}",
            ]
            lines.append(f"{index}. {' / '.join(part for part in parts if part)}")
    return "\n".join(lines)


def handle_receipt_user_message(user_id: str, message: str) -> ChatResponse:
    command = normalize_command(message)
    if command in ORDER_CANCEL_COMMANDS:
        clear_receipt_draft(user_id)
        return ChatResponse(
            user_id=user_id,
            answer="已清空当前入库草稿。请重新发送产成品入库照片。",
            history_length=0,
        )

    if command in ORDER_CONFIRM_COMMANDS:
        draft = get_receipt_draft(user_id)
        missing = receipt_missing_fields(draft)
        if missing:
            return ChatResponse(
                user_id=user_id,
                answer=(
                    "当前入库记录还不能保存，缺少："
                    + "、".join(missing)
                    + "\n"
                    + format_receipt_draft_summary(draft)
                    + "\n请重新发送清晰照片，或发“取消”清空。"
                ),
                history_length=0,
            )

        try:
            receipt_id, line_count = save_confirmed_receipt(draft)
        except Exception as exc:
            logger.warning("receipt_save_failed user_id=%s error=%s", user_id, exc)
            return ChatResponse(
                user_id=user_id,
                answer="入库记录保存失败了，请稍后再试，或联系管理员查看后台日志。",
                history_length=0,
            )

        clear_receipt_draft(user_id)
        return ChatResponse(
            user_id=user_id,
            answer=f"已保存产成品入库记录 {receipt_id}，共 {line_count} 行成品。",
            history_length=0,
        )

    return ChatResponse(
        user_id=user_id,
        answer="当前是入库模式。请发送产成品入库照片；识别后我会发清单给你确认。发“订单”可切到订单模式。",
        history_length=0,
    )


def handle_receipt_photo_input(user_id: str, image_bytes: bytes, mime_type: str | None, raw_ref: str) -> ChatResponse:
    try:
        draft = llm_parse_receipt_photo(image_bytes, mime_type, raw_ref)
    except Exception as exc:
        logger.warning("receipt_photo_parse_failed user_id=%s raw_ref=%s error=%s", user_id, raw_ref, exc)
        if "vision model is not configured" in str(exc):
            answer = "入库照片已收到，但当前视觉模型还没配置好。请稍后再试，或先人工记录。"
        else:
            answer = "这张入库照片我没有识别成功。请重新拍清楚成品名称和数量后再发。"
        return ChatResponse(user_id=user_id, answer=answer, history_length=0)

    save_receipt_draft(user_id, draft)
    missing = receipt_missing_fields(draft)
    summary = format_receipt_draft_summary(draft)
    if missing:
        answer = (
            "我先把照片识别成入库草稿，还缺："
            + "、".join(missing)
            + "\n"
            + summary
            + "\n请重新发送清晰照片，或发“取消”清空。"
        )
    else:
        answer = (
            "我把照片识别成待确认入库记录：\n"
            + summary
            + "\n确认无误请回复“确认”；不对请重新发送照片。"
        )
    return ChatResponse(user_id=user_id, answer=answer, history_length=0)


def handle_user_message(user_id: str, message: str, raw_ref: str | None = None) -> ChatResponse:
    user_id = user_id.strip()
    message = message.strip()

    if not user_id:
        raise HTTPException(status_code=400, detail="user_id cannot be empty")
    if not message:
        raise HTTPException(status_code=400, detail="message cannot be empty")

    command = normalize_command(message)
    inline_order_message = strip_order_inline_prefix(message)

    if command in ORDER_EXPORT_COMMANDS:
        return ChatResponse(
            user_id=user_id,
            answer=build_order_export_message(),
            history_length=user_order_count(user_id),
        )

    if command in ORDER_MODE_COMMANDS:
        set_session_mode(user_id, SESSION_MODE_ORDER)
        return ChatResponse(
            user_id=user_id,
            answer="已切换到订单模式。请发送订单内容；确认前我会先整理成草稿。发“问诊”可切回问诊模式。",
            history_length=user_order_count(user_id),
        )

    if command in RECEIPT_MODE_COMMANDS:
        set_session_mode(user_id, SESSION_MODE_RECEIPT)
        return ChatResponse(
            user_id=user_id,
            answer="已切换到入库模式。请发送产成品入库照片；识别后我会先整理成草稿让你确认。发“订单”可切回订单模式。",
            history_length=0,
        )

    if inline_order_message is not None:
        set_session_mode(user_id, SESSION_MODE_ORDER)
        return handle_order_user_message(user_id, inline_order_message, raw_ref=raw_ref)

    if command in INTERVIEW_MODE_COMMANDS:
        set_session_mode(user_id, SESSION_MODE_INTERVIEW)
        return ChatResponse(
            user_id=user_id,
            answer="已切换到问诊模式。可以继续按原来的访谈流程沟通。",
            history_length=0,
        )

    if get_session_mode(user_id) == SESSION_MODE_ORDER:
        return handle_order_user_message(user_id, message, raw_ref=raw_ref)

    if get_session_mode(user_id) == SESSION_MODE_RECEIPT:
        return handle_receipt_user_message(user_id, message)

    with MEMORY_LOCK:
        memory = load_memory()
        history = memory.setdefault(user_id, [])

        if not isinstance(history, list):
            raise HTTPException(status_code=500, detail=f"Invalid history for user_id: {user_id}")

        user_message_ts = time.time()
        history.append(
            {
                "role": "user",
                "content": message,
                "created_at": now_iso(),
                "ts": user_message_ts,
            }
        )
        history = trim_history(history)
        memory[user_id] = history

        logger.info("chat_request user_id=%s history_length=%s", user_id, len(history))

        answer = call_llm(user_id, history)
        history.append(
            {
                "role": "assistant",
                "content": answer,
                "created_at": now_iso(),
                "ts": time.time(),
            }
        )
        history = trim_history(history)
        memory[user_id] = history
        save_memory(memory)

    return ChatResponse(
        user_id=user_id,
        answer=answer,
        history_length=len(history),
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


def xml_text(parent: ET.Element, path: str, default: str = "") -> str:
    node = parent.find(path)
    if node is None or node.text is None:
        return default
    return node.text


def parse_wecom_plain_message(plain_xml: bytes | str) -> WecomMessage:
    if isinstance(plain_xml, bytes):
        plain_xml = plain_xml.decode("utf-8")

    root = ET.fromstring(plain_xml)
    msg_type = xml_text(root, "MsgType")
    chat_type = xml_text(root, "ChatType")
    chat_id = xml_text(root, "ChatId")
    msg_id = xml_text(root, "MsgId")
    sender_user_id = xml_text(root, "From/UserId")
    sender_name = xml_text(root, "From/Name") or xml_text(root, "From/Alias") or sender_user_id

    if msg_type == "text":
        content = xml_text(root, "Text/Content")
    elif msg_type == "mixed":
        parts: list[str] = []
        mixed = root.find("MixedMessage")
        if mixed is not None:
            for item in mixed:
                if xml_text(item, "MsgType") == "text":
                    parts.append(xml_text(item, "Text/Content"))
        content = "\n".join(part for part in parts if part).strip()
    elif msg_type == "event":
        event_type = xml_text(root, "Event/EventType")
        content = f"[event:{event_type}]"
    else:
        content = ""

    return WecomMessage(
        msg_type=msg_type,
        chat_type=chat_type,
        chat_id=chat_id,
        msg_id=msg_id,
        sender_user_id=sender_user_id,
        sender_name=sender_name,
        content=content,
    )


def strip_bot_mention(content: str) -> str:
    content = content.strip()
    if WECOM_BOT_NAME:
        content = content.replace(f"@{WECOM_BOT_NAME}", "").strip()
    return content


def build_wecom_text_response_xml(content: str) -> bytes:
    root = ET.Element("xml")
    ET.SubElement(root, "MsgType").text = "text"
    text = ET.SubElement(root, "Text")
    ET.SubElement(text, "Content").text = content
    return ET.tostring(root, encoding="utf-8", method="xml")


def encrypt_wecom_response(content: str, nonce: str, timestamp: str) -> str:
    ret, encrypted_response = get_wecom_crypto().EncryptMsg(
        build_wecom_text_response_xml(content),
        nonce,
        timestamp,
    )
    if ret != 0:
        logger.warning("wecom_encrypt_failed ret=%s", ret)
        raise HTTPException(status_code=500, detail=f"WeCom encrypt failed: {ret}")

    if isinstance(encrypted_response, bytes):
        return encrypted_response.decode("utf-8")
    return encrypted_response


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


def compute_wecom_signature(token: str, timestamp: str, nonce: str, encrypted: str) -> str:
    raw = "".join(sorted([token, timestamp, nonce, encrypted]))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def decrypt_wecom_kf_payload(encrypted: str) -> tuple[bytes, str]:
    if not WECOM_KF_ENCODING_AES_KEY:
        raise HTTPException(status_code=500, detail="WECOM_KF_ENCODING_AES_KEY must be configured")

    try:
        key = base64.b64decode(WECOM_KF_ENCODING_AES_KEY + "=")
        if len(key) != 32:
            raise ValueError("invalid key length")
        cryptor = AES.new(key, AES.MODE_CBC, key[:16])
        plain_text = cryptor.decrypt(base64.b64decode(encrypted))
        pad = plain_text[-1]
        if pad < 1 or pad > 32:
            raise ValueError("invalid padding")

        content = plain_text[16:-pad]
        if len(content) < 4:
            raise ValueError("missing message length")
        xml_len = socket.ntohl(struct.unpack("I", content[:4])[0])
        xml_content = content[4 : xml_len + 4]
        receive_id = content[xml_len + 4 :].decode("utf-8", errors="replace")
        return xml_content, receive_id
    except Exception as exc:
        logger.warning("wecom_kf_decrypt_payload_failed error=%s", exc)
        raise HTTPException(status_code=403, detail="WeCom KF decrypt failed") from exc


def verify_wecom_kf_signature(msg_signature: str, timestamp: str, nonce: str, encrypted: str) -> None:
    if not WECOM_KF_CALLBACK_TOKEN:
        raise HTTPException(status_code=500, detail="WECOM_KF_CALLBACK_TOKEN must be configured")

    expected_signature = compute_wecom_signature(
        WECOM_KF_CALLBACK_TOKEN,
        timestamp,
        nonce,
        encrypted,
    )
    if not hmac.compare_digest(expected_signature, msg_signature):
        logger.warning(
            "wecom_kf_signature_failed expected=%s actual=%s",
            expected_signature,
            msg_signature,
        )
        raise HTTPException(status_code=403, detail="WeCom KF signature failed")


def decrypt_wecom_kf_verify_url(
    msg_signature: str,
    timestamp: str,
    nonce: str,
    echostr: str,
) -> str:
    verify_wecom_kf_signature(msg_signature, timestamp, nonce, echostr)
    decrypted_echo, receive_id = decrypt_wecom_kf_payload(echostr)
    if WECOM_KF_CORP_ID and receive_id != WECOM_KF_CORP_ID:
        logger.warning(
            "wecom_kf_receive_id_mismatch expected=%s actual=%s",
            WECOM_KF_CORP_ID,
            receive_id,
        )
    return decrypted_echo.decode("utf-8")


def decrypt_wecom_kf_message(
    encrypted_body: bytes,
    msg_signature: str,
    timestamp: str,
    nonce: str,
) -> bytes:
    try:
        encrypted = xml_text(ET.fromstring(encrypted_body), "Encrypt")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid WeCom KF encrypted XML") from exc

    if not encrypted:
        raise HTTPException(status_code=400, detail="Missing WeCom KF Encrypt field")

    verify_wecom_kf_signature(msg_signature, timestamp, nonce, encrypted)
    plain_xml, receive_id = decrypt_wecom_kf_payload(encrypted)
    if WECOM_KF_CORP_ID and receive_id != WECOM_KF_CORP_ID:
        logger.warning(
            "wecom_kf_receive_id_mismatch expected=%s actual=%s",
            WECOM_KF_CORP_ID,
            receive_id,
        )
    return plain_xml


def parse_wecom_kf_event(plain_xml: bytes | str) -> WecomKfEvent:
    if isinstance(plain_xml, bytes):
        plain_xml = plain_xml.decode("utf-8")

    root = ET.fromstring(plain_xml)
    msg_type = xml_text(root, "MsgType")
    event = xml_text(root, "Event")

    if msg_type != "event" or event != "kf_msg_or_event":
        raise ValueError(f"Unsupported WeCom KF callback: MsgType={msg_type}, Event={event}")

    return WecomKfEvent(
        token=xml_text(root, "Token"),
        open_kfid=xml_text(root, "OpenKfId"),
        event=event,
        create_time=xml_text(root, "CreateTime"),
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

    filename = media_id
    disposition = response.headers.get("content-disposition", "")
    match = re.search(r'filename="?([^";]+)"?', disposition)
    if match:
        filename = match.group(1)
    return response.content, content_type, filename


def save_excel_order_payloads(file_bytes: bytes, raw_ref: str) -> list[dict[str, Any]]:
    payloads = parse_excel_order_payloads(file_bytes, raw_ref)
    saved: list[dict[str, Any]] = []
    for payload in payloads:
        payload["confirmed"] = True
        payload["status"] = ORDER_STATUS_NEW
        saved.append(insert_order_payload(payload))
    return saved


def format_saved_order_ids(saved_orders: list[dict[str, Any]]) -> str:
    ids = [str(order.get("id")) for order in saved_orders if order.get("id")]
    return "、".join(ids)


def handle_excel_order_input(user_id: str, file_bytes: bytes, raw_ref: str) -> ChatResponse:
    try:
        saved = save_excel_order_payloads(file_bytes, raw_ref)
    except Exception as exc:
        logger.warning("excel_order_import_failed user_id=%s raw_ref=%s error=%s", user_id, raw_ref, exc)
        return ChatResponse(
            user_id=user_id,
            answer="Excel订单解析失败了。请确认文件是标准订单表，并包含商品名称和数量列。",
            history_length=user_order_count(user_id),
        )

    line_count = sum(len(order.get("items") or []) for order in saved)
    return ChatResponse(
        user_id=user_id,
        answer=f"Excel订单已入库，ID {format_saved_order_ids(saved)}，共 {line_count} 行商品。",
        history_length=user_order_count(user_id),
    )


def handle_photo_order_input(user_id: str, image_bytes: bytes, mime_type: str | None, raw_ref: str) -> ChatResponse:
    try:
        draft = llm_parse_photo_order(image_bytes, mime_type, raw_ref)
    except Exception as exc:
        logger.warning("photo_order_parse_failed user_id=%s raw_ref=%s error=%s", user_id, raw_ref, exc)
        if "vision model is not configured" in str(exc):
            return ChatResponse(
                user_id=user_id,
                answer="照片订单识别已收到，但当前视觉模型还没配置好。请先用文字发送门店、商品和数量。",
                history_length=user_order_count(user_id),
            )
        return ChatResponse(
            user_id=user_id,
            answer="这张照片我没有识别成功。请重新拍清楚订单表，或直接用文字发送门店、商品和数量。",
            history_length=user_order_count(user_id),
        )

    draft["confirmed"] = False
    draft["status"] = ORDER_STATUS_NEW
    save_order_draft(user_id, draft)

    missing = order_draft_missing_fields(draft)
    summary = format_order_draft_summary(draft)
    if missing:
        answer = (
            "我先把照片识别成订单草稿，还缺："
            + "、".join(missing)
            + "\n"
            + summary
            + "\n请直接补充缺失信息，或发“取消”清空。"
        )
    else:
        answer = (
            "我把照片识别成待确认订单：\n"
            + summary
            + "\n确认无误请回复“确认”；要修改就直接发修改内容。"
        )

    return ChatResponse(
        user_id=user_id,
        answer=answer,
        history_length=user_order_count(user_id),
    )


def maybe_archive_wecom_kf_event(event_info: dict[str, Any]) -> None:
    event_type = str(event_info.get("event_type") or "")
    open_kfid = str(event_info.get("open_kfid") or "")
    external_userid = str(event_info.get("external_userid") or "")
    if not open_kfid or not external_userid:
        return

    should_archive = event_type in ARCHIVE_EVENT_TYPES
    if event_type == "session_status_change":
        try:
            should_archive = int(event_info.get("change_type", 0)) == 3
        except (TypeError, ValueError):
            should_archive = False

    if not should_archive:
        return

    session_id = f"kf:{open_kfid}:{external_userid}"
    archive_interview_session(session_id, f"kf_event:{event_type}")


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
        event_info = item.get("event", {})
        logger.info("wecom_kf_event msg_id=%s event=%s", msg_id, event_info)
        if isinstance(event_info, dict):
            maybe_archive_wecom_kf_event(event_info)
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
            logger.warning("wecom_kf_image_order_failed msg_id=%s media_id=%s error=%s", msg_id, media_id, exc)
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
            if extension not in {".xlsx", ".xlsm"} and "spreadsheet" not in content_type:
                answer = "这个文件我暂时只支持标准 Excel 订单表。"
            else:
                answer = handle_excel_order_input(session_id, media_bytes, raw_ref=f"{raw_ref}:{filename}").answer
        except Exception as exc:
            logger.warning("wecom_kf_file_order_failed msg_id=%s media_id=%s error=%s", msg_id, media_id, exc)
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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/wecom/callback")
async def wecom_verify(request: Request):
    msg_signature = require_query_param(request, "msg_signature")
    timestamp = require_query_param(request, "timestamp")
    nonce = require_query_param(request, "nonce")
    echostr = require_query_param(request, "echostr")

    ret, decrypted_echo = get_wecom_crypto().VerifyURL(
        msg_signature,
        timestamp,
        nonce,
        echostr,
    )
    if ret != 0:
        logger.warning("wecom_verify_failed ret=%s", ret)
        raise HTTPException(status_code=403, detail=f"WeCom verify failed: {ret}")

    if isinstance(decrypted_echo, bytes):
        decrypted_echo = decrypted_echo.decode("utf-8")

    return PlainTextResponse(decrypted_echo)


@app.post("/wecom/callback")
async def wecom_callback(request: Request):
    msg_signature = require_query_param(request, "msg_signature")
    timestamp = require_query_param(request, "timestamp")
    nonce = require_query_param(request, "nonce")
    encrypted_body = await request.body()

    ret, plain_xml = get_wecom_crypto().DecryptMsg(
        encrypted_body,
        msg_signature,
        timestamp,
        nonce,
    )
    if ret != 0:
        logger.warning("wecom_decrypt_failed ret=%s", ret)
        raise HTTPException(status_code=403, detail=f"WeCom decrypt failed: {ret}")

    message = parse_wecom_plain_message(plain_xml)
    logger.info(
        "wecom_message msg_id=%s msg_type=%s chat_type=%s chat_id=%s sender=%s",
        message.msg_id,
        message.msg_type,
        message.chat_type,
        message.chat_id,
        message.sender_user_id,
    )

    if is_duplicate_wecom_message(message.msg_id):
        logger.info("wecom_duplicate_message msg_id=%s", message.msg_id)
        answer = ""
    else:
        try:
            answer = answer_wecom_message(message)
        except Exception as exc:
            logger.warning("wecom_answer_failed msg_id=%s error=%s", message.msg_id, exc)
            answer = "这条消息处理失败了，请稍后再试。"

    encrypted_response = encrypt_wecom_response(answer, nonce, timestamp)
    return PlainTextResponse(encrypted_response, media_type="application/xml")


@app.get("/wecom/kf/callback")
async def wecom_kf_verify(request: Request):
    msg_signature = require_query_param(request, "msg_signature")
    timestamp = require_query_param(request, "timestamp")
    nonce = require_query_param(request, "nonce")
    echostr = require_query_param(request, "echostr")

    decrypted_echo = decrypt_wecom_kf_verify_url(
        msg_signature,
        timestamp,
        nonce,
        echostr,
    )

    return PlainTextResponse(decrypted_echo)


@app.post("/wecom/kf/callback")
async def wecom_kf_callback(request: Request, background_tasks: BackgroundTasks):
    msg_signature = require_query_param(request, "msg_signature")
    timestamp = require_query_param(request, "timestamp")
    nonce = require_query_param(request, "nonce")
    encrypted_body = await request.body()

    plain_xml = decrypt_wecom_kf_message(
        encrypted_body,
        msg_signature,
        timestamp,
        nonce,
    )

    try:
        event = parse_wecom_kf_event(plain_xml)
    except Exception as exc:
        logger.warning("wecom_kf_parse_failed error=%s", exc)
        return PlainTextResponse("success")

    logger.info(
        "wecom_kf_callback event=%s open_kfid=%s create_time=%s",
        event.event,
        event.open_kfid,
        event.create_time,
    )
    background_tasks.add_task(process_wecom_kf_event, event)
    return PlainTextResponse("success")


@app.post("/chat", response_model=ChatResponse)
def chat(payload: ChatRequest) -> ChatResponse:
    return handle_user_message(payload.user_id, payload.message)


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


@app.get("/api/orders")
def api_orders(
    request: Request,
    status: str = Query(default=ORDER_STATUS_NEW),
    order_date: str | None = Query(default=None),
    ids: str | None = Query(default=None),
) -> dict[str, list[dict[str, Any]]]:
    require_robot_api_token(request)
    parsed_ids = parse_ids_param(ids)
    if parsed_ids:
        return {"orders": query_order_payloads(ids=parsed_ids)}

    if status not in ORDER_STATUSES:
        raise HTTPException(status_code=400, detail="status must be new, fetched, or all")
    normalized_order_date = validate_iso_date_param(order_date or "", "order_date")
    query_status = None if status == ORDER_STATUS_ALL else status
    return {"orders": query_order_payloads(status=query_status, order_date=normalized_order_date)}


@app.post("/api/orders/mark_fetched")
def api_mark_fetched(request: Request, payload: MarkFetchedRequest) -> dict[str, Any]:
    require_robot_api_token(request)
    return mark_order_payloads_fetched(payload.ids)


@app.get("/api/receipts")
def api_receipts(
    request: Request,
    date: str = Query(...),
) -> dict[str, list[dict[str, Any]]]:
    require_robot_api_token(request)
    normalized_date = validate_iso_date_param(date, "date")
    return {"receipts": query_receipt_payloads(normalized_date)}


@app.post("/api/orders/import/excel")
async def api_import_excel(
    request: Request,
    file: UploadFile = File(...),
    raw_ref: str | None = Query(default=None),
) -> dict[str, Any]:
    require_robot_api_token(request)
    file_bytes = await file.read()
    reference = raw_ref or file.filename or "api:excel"
    try:
        saved = save_excel_order_payloads(file_bytes, reference)
    except Exception as exc:
        logger.warning("api_excel_import_failed raw_ref=%s error=%s", reference, exc)
        raise HTTPException(status_code=400, detail="Excel order import failed") from exc
    return {"orders": saved}


@app.post("/api/orders/import/photo")
async def api_import_photo(
    request: Request,
    file: UploadFile = File(...),
    user_id: str = Query(default="api"),
    confirm: bool = Query(default=False),
    raw_ref: str | None = Query(default=None),
) -> dict[str, Any]:
    require_robot_api_token(request)
    image_bytes = await file.read()
    reference = raw_ref or file.filename or "api:photo"
    mime_type = file.content_type or mimetypes.guess_type(file.filename or "")[0]
    try:
        draft = llm_parse_photo_order(image_bytes, mime_type, reference)
    except Exception as exc:
        logger.warning("api_photo_import_failed raw_ref=%s error=%s", reference, exc)
        if "vision model is not configured" in str(exc):
            raise HTTPException(status_code=503, detail="Photo recognition vision model is not configured") from exc
        raise HTTPException(status_code=400, detail="Photo order parse failed") from exc

    if confirm:
        missing = order_draft_missing_fields(draft)
        if missing:
            raise HTTPException(status_code=400, detail="missing fields: " + "、".join(missing))
        draft["confirmed"] = True
        saved = insert_order_payload(draft)
        return {"order": saved}

    save_order_draft(user_id, draft)
    return {"draft": draft, "message": "photo parsed; confirm before it is visible from /api/orders"}


@app.post("/api/orders/import/text")
def api_import_text(request: Request, payload: TextOrderImportRequest) -> dict[str, Any]:
    require_robot_api_token(request)
    raw_ref = payload.raw_ref or f"api:text:{payload.user_id}"
    try:
        draft = llm_parse_order_draft({}, payload.message)
    except Exception as exc:
        logger.warning("api_text_import_failed user_id=%s error=%s", payload.user_id, exc)
        raise HTTPException(status_code=400, detail="Text order parse failed") from exc

    draft["raw_ref"] = raw_ref
    draft["raw_text"] = draft.get("raw_text") or payload.message
    draft["confirmed"] = bool(payload.confirm)
    if payload.confirm:
        missing = order_draft_missing_fields(draft)
        if missing:
            raise HTTPException(status_code=400, detail="missing fields: " + "、".join(missing))
        saved = insert_order_payload(draft)
        return {"order": saved}

    save_order_draft(payload.user_id, draft)
    return {"draft": normalize_order_payload(draft), "message": "text parsed; confirm before it is visible from /api/orders"}


@app.get("/memory/{user_id}", response_model=MemoryLengthResponse)
def get_memory_length(user_id: str) -> MemoryLengthResponse:
    user_id = user_id.strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id cannot be empty")

    with MEMORY_LOCK:
        memory = load_memory()
        history = memory.get(user_id, [])

        if not isinstance(history, list):
            raise HTTPException(status_code=500, detail=f"Invalid history for user_id: {user_id}")

    return MemoryLengthResponse(user_id=user_id, history_length=len(history))


def require_export_token(request: Request) -> str:
    token = request.query_params.get("token", "")
    if EXPORT_TOKEN and token != EXPORT_TOKEN:
        raise HTTPException(status_code=403, detail="invalid export token")
    return token


@app.get("/exports", response_class=HTMLResponse)
def export_page(request: Request):
    token = request.query_params.get("token", "")

    if EXPORT_TOKEN and token != EXPORT_TOKEN:
        return HTMLResponse(
            """
            <!doctype html>
            <html lang="zh-CN">
              <head>
                <meta charset="utf-8">
                <meta name="viewport" content="width=device-width, initial-scale=1">
                <title>访谈导出</title>
                <style>
                  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 48px; color: #172033; }
                  main { max-width: 520px; }
                  label { display: block; margin: 18px 0 8px; font-weight: 600; }
                  input { width: 100%; box-sizing: border-box; padding: 12px 14px; border: 1px solid #ccd3df; border-radius: 8px; font-size: 16px; }
                  button { margin-top: 18px; padding: 12px 18px; border: 0; border-radius: 8px; background: #1f6feb; color: white; font-size: 16px; cursor: pointer; }
                  p { color: #667085; line-height: 1.6; }
                </style>
              </head>
              <body>
                <main>
                  <h1>访谈导出</h1>
                  <p>请输入导出口令后进入导出页。</p>
                  <form method="get" action="/exports">
                    <label for="token">导出口令</label>
                    <input id="token" name="token" type="password" autocomplete="current-password">
                    <button type="submit">进入</button>
                  </form>
                </main>
              </body>
            </html>
            """
        )

    records = collect_interview_records()
    companies = sorted({record["company"] for record in records})
    download_url = "/exports/interviews.xlsx"
    if token:
        download_url = f"{download_url}?token={token}"

    company_items = "".join(f"<li>{company}</li>" for company in companies[:30])
    if len(companies) > 30:
        company_items += f"<li>还有 {len(companies) - 30} 个公司...</li>"
    if not company_items:
        company_items = "<li>暂无公司数据</li>"

    return HTMLResponse(
        f"""
        <!doctype html>
        <html lang="zh-CN">
          <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>访谈导出</title>
            <style>
              body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 48px; color: #172033; background: #f6f8fb; }}
              main {{ max-width: 880px; }}
              .panel {{ background: white; border: 1px solid #e3e8f0; border-radius: 10px; padding: 28px; box-shadow: 0 8px 24px rgba(15, 23, 42, 0.06); }}
              .stats {{ display: flex; gap: 16px; margin: 22px 0; }}
              .stat {{ background: #f1f5fb; border-radius: 8px; padding: 16px 18px; min-width: 140px; }}
              .stat strong {{ display: block; font-size: 28px; color: #0f4c81; }}
              a.button {{ display: inline-block; margin-top: 10px; padding: 13px 18px; border-radius: 8px; background: #1f6feb; color: white; text-decoration: none; font-weight: 700; }}
              p {{ color: #667085; line-height: 1.6; }}
              ul {{ columns: 2; color: #344054; line-height: 1.8; }}
            </style>
          </head>
          <body>
            <main class="panel">
              <h1>访谈导出</h1>
              <p>点击按钮生成并下载 Excel。文件包含“全部反馈”和每个公司的独立 sheet。</p>
              <div class="stats">
                <div class="stat"><strong>{len(records)}</strong>访谈记录</div>
                <div class="stat"><strong>{len(companies)}</strong>公司分表</div>
              </div>
              <a class="button" href="{download_url}">下载 Excel</a>
              <h2>当前公司</h2>
              <ul>{company_items}</ul>
            </main>
          </body>
        </html>
        """
    )


@app.get("/exports/interviews.xlsx")
def export_interviews(request: Request):
    require_export_token(request)

    output_path = build_interview_export()
    return FileResponse(
        output_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=output_path.name,
    )


@app.get("/exports/orders.xlsx")
def export_orders(request: Request):
    require_export_token(request)

    output_path = build_order_export()
    return FileResponse(
        output_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=output_path.name,
    )


@app.delete("/memory/{user_id}", response_model=DeleteMemoryResponse)
def delete_memory(user_id: str) -> DeleteMemoryResponse:
    user_id = user_id.strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id cannot be empty")

    with MEMORY_LOCK:
        memory = load_memory()
        memory.pop(user_id, None)
        save_memory(memory)

    logger.info("memory_deleted user_id=%s", user_id)
    return DeleteMemoryResponse(deleted=True, user_id=user_id)
